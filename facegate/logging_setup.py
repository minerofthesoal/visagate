"""Rotating file logger for FaceGate, in addition to syslog.

syslog is the source of truth (works even if /var/log is unwritable, e.g.
read-only root), but journalctl -t facegate isn't discoverable for
everyone, and it doesn't give you a simple `facegate log` view. This
writes the same events to /var/log/facegate/facegate.log so `facegate log`
has something to read directly, with rotation so it can't grow unbounded.

New in v0.2.0.
"""
import logging
import logging.handlers
import os

LOG_DIR = "/var/log/facegate"
LOG_FILE = os.path.join(LOG_DIR, "facegate.log")

_logger = None


def get_logger(name="facegate"):
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        os.chmod(LOG_DIR, 0o750)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=5
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        if os.path.exists(LOG_FILE):
            os.chmod(LOG_FILE, 0o640)
    except (PermissionError, OSError):
        # Not root, or /var/log/facegate isn't writable yet. Syslog (in
        # pam_helper._log) remains the record of truth in that case -- this
        # file log is a convenience, not the only copy.
        logger.addHandler(logging.NullHandler())

    _logger = logger
    return logger
