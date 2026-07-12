"""Entry point invoked by pam_exec.so at auth time.

Exit code 0  -> tell PAM the user authenticated.
Exit code != 0 -> tell PAM to fall through to the next stack entry
                  (normally the real password prompt).

pam_exec sets PAM_USER (account being authenticated) and PAM_SERVICE
(which PAM service triggered this -- "sudo", "login", "kde",
"kscreenlocker-greet", "sddm", etc.) in the environment; we use both.

Every attempt is logged to syslog (facility LOG_AUTH, ident "facegate")
AND to /var/log/facegate/facegate.log (see logging_setup.py, new in
v0.2.0) so a silent fall-through to password can actually be diagnosed
after the fact instead of just being a mystery exit code 1:
    sudo journalctl -t facegate -e
    sudo facegate log
"""
import fcntl
import os
import sys
import syslog
import time

from . import camera, config, recognizer, security
from .logging_setup import get_logger

# PAM services where the person is looking at a lock/login screen right
# now, as opposed to a sudo prompt in a terminal they're already sitting
# at. Used to pick the shorter recognition.timeout_seconds_greeter budget
# instead of the sudo-context timeout_seconds. New in v0.2.0.
GREETER_SERVICES = {"sddm", "sddm-greeter", "kde", "kde-np", "kscreenlocker-greet"}

CAMERA_LOCK_FILE = "/run/facegate/camera.lock"


def _log(message, level=syslog.LOG_INFO):
    try:
        syslog.openlog(ident="facegate", facility=syslog.LOG_AUTH)
        syslog.syslog(level, message)
    except Exception:
        pass  # logging must never be the reason auth fails closed
    try:
        get_logger().info(message)
    except Exception:
        pass


def _acquire_camera_lock(timeout=3.0):
    """Serialize camera access across concurrent PAM invocations (e.g. a
    sudo prompt and the lock screen both triggering a face check within
    the same second). V4L2 devices generally only allow one exclusive
    open at a time -- without this, the loser gets a confusing
    "cannot open camera device" failure instead of a clean, explained
    fallback to password. New in v0.2.0."""
    try:
        os.makedirs("/run/facegate", exist_ok=True)
        fd = os.open(CAMERA_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            time.sleep(0.2)
    os.close(fd)
    return None


def main():
    cfg = config.load()
    if not cfg.get("enabled"):
        sys.exit(1)  # face unlock turned off -> always fall back to password

    username = os.environ.get("PAM_USER")
    if not username:
        _log("no PAM_USER in environment; falling back to password")
        sys.exit(1)

    service = os.environ.get("PAM_SERVICE", "unknown")

    locked, remaining = security.is_locked_out()
    if locked:
        _log(f"user={username} service={service} SKIPPED: locked out, {remaining}s remaining")
        sys.exit(1)

    # Right after a cold boot (most relevant at the SDDM login screen) a
    # USB webcam can take a moment to enumerate. Give it a few seconds
    # before giving up, rather than failing closed on the very first try
    # every time the machine restarts. New in v0.2.0.
    if not camera.find_brio_devices():
        camera.wait_for_devices(timeout=cfg.get("camera_wait_seconds", 5))

    lock_fd = _acquire_camera_lock()
    if lock_fd is None:
        _log(f"user={username} service={service} SKIPPED: camera busy (concurrent check in progress)")
        sys.exit(1)

    timeout_override = None
    if service in GREETER_SERVICES:
        timeout_override = cfg["recognition"].get("timeout_seconds_greeter")

    try:
        ok, info = recognizer.authenticate(username, timeout_override=timeout_override)
    except Exception as e:
        # Any camera/model error must fail closed, not crash PAM.
        _log(f"user={username} service={service} EXCEPTION during authenticate(): {type(e).__name__}: {e}")
        security.record_failure()
        sys.exit(1)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    _log(f"user={username} service={service} result={'MATCH' if ok else 'NO MATCH'} info={info}")
    if ok:
        security.record_success()
        sys.exit(0)
    security.record_failure()
    sys.exit(1)


if __name__ == "__main__":
    main()
