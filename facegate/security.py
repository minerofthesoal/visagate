"""Guards for privileged FaceGate actions (disable / uninstall / change PIN).

Two independent unlock paths are supported, matching the request: the
account's real password, or a separate FaceGate-only PIN. Either is
accepted so a PIN can be handed out for quick toggling without sharing
the real system password.

IMPORTANT: password verification here deliberately does NOT go through
the system `sudo`/`login`/`kde` PAM stacks, because FaceGate itself adds
a `pam_exec.so ... sufficient` face-auth line to those. If we verified
"do you know the password" via regular `sudo`, a successfully spoofed (or
just successfully recognized) face would satisfy that check too, letting
face recognition alone disable face recognition. Instead we maintain our
own tiny PAM service, `facegate-verify`, containing nothing but
`pam_unix.so` -- no face auth is ever added to it -- so this check can
only ever be satisfied by the real account password.
"""
import fcntl
import getpass
import hashlib
import json
import os
import subprocess
import time

from . import config

VERIFY_SERVICE_NAME = "facegate-verify"
VERIFY_SERVICE_FILE = f"/etc/pam.d/{VERIFY_SERVICE_NAME}"

# Lockout state lives on tmpfs, not under /etc/facegate: it's meant to
# reset on reboot (a lockout surviving a reboot just because someone was
# fumbling with lighting last night is a worse user experience than the
# security gain is worth), and it needs to be safely shared/locked across
# concurrent PAM invocations (sudo + lock screen firing at once). New in
# v0.2.0.
LOCKOUT_DIR = "/run/facegate"
LOCKOUT_STATE_FILE = os.path.join(LOCKOUT_DIR, "lockout.json")
LOCKOUT_LOCK_FILE = os.path.join(LOCKOUT_DIR, "lockout.lock")


def ensure_verify_service():
    """Create the dedicated password-only PAM service if it doesn't exist yet."""
    if os.path.exists(VERIFY_SERVICE_FILE):
        return
    content = (
        "# Managed by FaceGate.\n"
        "# Do NOT add pam_exec/face-auth lines to this file. It exists\n"
        "# specifically so FaceGate's internal 'confirm your real password'\n"
        "# check can never be satisfied by face recognition.\n"
        "auth     required   pam_unix.so\n"
        "account  required   pam_unix.so\n"
    )
    with open(VERIFY_SERVICE_FILE, "w") as f:
        f.write(content)
    os.chmod(VERIFY_SERVICE_FILE, 0o644)


def verify_sudo_password():
    """Verify the real account password via the isolated facegate-verify
    PAM service (falls back to `sudo -S` only if python-pam isn't
    installed -- that fallback path IS subject to the face-auth caveat
    above, so python-pam is installed by default in install.sh)."""
    ensure_verify_service()
    username = os.environ.get("SUDO_USER") or getpass.getuser()
    pw = getpass.getpass("Enter your account password to confirm: ")
    try:
        import pam as pam_module
    except ImportError:
        print("(python-pam not installed -- falling back to `sudo -S`, which")
        print(" is satisfied by face recognition too. Run install.sh again")
        print(" to install python-pam and close this gap.)")
        proc = subprocess.run(
            ["sudo", "-S", "-k", "true"], input=pw + "\n", text=True, capture_output=True
        )
        return proc.returncode == 0
    p = pam_module.pam()
    return bool(p.authenticate(username, pw, service=VERIFY_SERVICE_NAME))


def hash_pin(pin, salt_hex=None):
    if salt_hex is None:
        salt_hex = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", pin.encode(), bytes.fromhex(salt_hex), 200_000
    ).hex()
    return digest, salt_hex


def set_pin(pin):
    digest, salt = hash_pin(pin)
    cfg = config.load()
    cfg["pin_hash"] = digest
    cfg["pin_salt"] = salt
    config.save(cfg)


def verify_pin(pin):
    cfg = config.load()
    if not cfg.get("pin_hash") or not cfg.get("pin_salt"):
        return False
    digest, _ = hash_pin(pin, cfg["pin_salt"])
    return digest == cfg["pin_hash"]


def confirm_privileged_action(prompt="This action requires confirmation."):
    print(prompt)
    print("  1) Verify with sudo password")
    print("  2) Verify with custom PIN")
    choice = input("Choose [1/2]: ").strip()
    if choice == "1":
        return verify_sudo_password()
    if choice == "2":
        pin = getpass.getpass("Enter PIN: ")
        return verify_pin(pin)
    return False


# ---------------------------------------------------------------------------
# Lockout / cooldown (v0.2.0)
#
# Protects against repeated face-auth attempts (accidental or a deliberate
# spoofing attempt) by tripping a cooldown after N consecutive failures,
# during which PAM checks are skipped entirely and fall straight to
# password. State is a tiny JSON file on tmpfs, guarded by a real flock so
# concurrent PAM invocations (e.g. sudo in one terminal and the lock screen
# both attempting a face check at once) can't race each other into an
# inconsistent count.
# ---------------------------------------------------------------------------


def _with_lockout_lock(fn):
    os.makedirs(LOCKOUT_DIR, exist_ok=True)
    fd = os.open(LOCKOUT_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _load_lockout_state():
    try:
        with open(LOCKOUT_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"failures": 0, "locked_until": 0}


def _save_lockout_state(state):
    os.makedirs(LOCKOUT_DIR, exist_ok=True)
    tmp = LOCKOUT_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, LOCKOUT_STATE_FILE)
    try:
        os.chmod(LOCKOUT_STATE_FILE, 0o600)
    except PermissionError:
        pass


def record_failure():
    """Bump the consecutive-failure counter; trip the cooldown if the
    configured threshold is reached."""
    cfg = config.load()
    max_attempts = cfg.get("lockout", {}).get("max_failed_attempts", 5)
    cooldown = cfg.get("lockout", {}).get("cooldown_seconds", 300)

    def _do():
        state = _load_lockout_state()
        state["failures"] = state.get("failures", 0) + 1
        if state["failures"] >= max_attempts:
            state["locked_until"] = time.time() + cooldown
            state["failures"] = 0
        _save_lockout_state(state)

    _with_lockout_lock(_do)


def record_success():
    """Reset the failure counter and clear any active cooldown."""
    _with_lockout_lock(lambda: _save_lockout_state({"failures": 0, "locked_until": 0}))


def is_locked_out():
    """Return (bool locked, int seconds_remaining)."""
    state = _load_lockout_state()
    remaining = int(state.get("locked_until", 0) - time.time())
    if remaining > 0:
        return True, remaining
    return False, 0


def clear_lockout():
    """Manually clear a cooldown (used by `sudo facegate enable`, so
    re-enabling after tuning thresholds doesn't leave you locked out from
    the tuning attempts themselves)."""
    _with_lockout_lock(lambda: _save_lockout_state({"failures": 0, "locked_until": 0}))
