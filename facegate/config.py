"""Configuration storage for FaceGate.

Everything lives under /etc/facegate, root-owned, mode 0700/0600, since it
holds face model files and a PIN hash.
"""
import json
import os

CONFIG_DIR = "/etc/facegate"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
MODEL_DIR = os.path.join(CONFIG_DIR, "models")

DEFAULTS = {
    "enabled": False,
    "camera": {
        "rgb_device": None,
        "ir_device": None,
        "auto_detected": False,
    },
    # Seconds to wait/retry for the camera devices to enumerate before
    # giving up. USB webcams (especially ones behind a hub) don't always
    # show up in v4l2-ctl the instant the kernel finishes booting, so a
    # PAM check that runs right at the SDDM login screen after a cold
    # boot can otherwise race the device and silently fail closed. New in
    # v0.2.0.
    "camera_wait_seconds": 5,
    "recognition": {
        # LBPH: LOWER confidence value = better match. These thresholds are
        # conservative starting points; `facegate test` will show you real
        # numbers for your face/lighting so you can tune them.
        "confidence_threshold_rgb": 60,
        "confidence_threshold_ir": 65,
        "require_both": True,
        # How many distinct face-match attempts to make (each attempt gets
        # its own short time slice) before giving up and letting PAM fall
        # through to the normal password prompt. Configurable via
        # `facegate set-attempts N`.
        "max_attempts": 2,
        "timeout_seconds": 8,
        # Shorter budget used specifically for greeter/lock-screen PAM
        # services (sddm, kde, kscreenlocker-greet). Those UIs read as
        # "broken" if the screen just sits there for 16s (max_attempts x
        # timeout_seconds) before a face check gives up, so lock-screen
        # contexts get a tighter timeout than an interactive sudo prompt
        # where you're already sitting there anyway. New in v0.2.0.
        "timeout_seconds_greeter": 6,
        # Minimum face bounding-box size (pixels) the Haar cascade will
        # accept. Lower this if you're sitting far from the camera and
        # enrollment/recognition can't find a face at all.
        "min_face_size": 80,
    },
    # Brute-force / repeated-spoofing-attempt protection. After
    # max_failed_attempts consecutive failures (across ALL PAM contexts,
    # counted process-wide via /run/facegate/lockout.json), face auth is
    # skipped for cooldown_seconds and PAM falls straight to password --
    # not just for the current login attempt, but until the cooldown
    # expires. State lives under /run (tmpfs) so a lockout doesn't
    # survive a reboot. New in v0.2.0.
    "lockout": {
        "max_failed_attempts": 5,
        "cooldown_seconds": 300,
    },
    "pin_hash": None,
    "pin_salt": None,
}


def ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(MODEL_DIR, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
        os.chmod(MODEL_DIR, 0o700)
    except PermissionError:
        pass


def load():
    ensure_dirs()
    if not os.path.exists(CONFIG_FILE):
        save(dict(DEFAULTS))
        return dict(DEFAULTS)
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    merged = json.loads(json.dumps(DEFAULTS))  # deep copy
    _deep_update(merged, cfg)
    return merged


def save(cfg):
    ensure_dirs()
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except PermissionError:
        pass


def _deep_update(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
