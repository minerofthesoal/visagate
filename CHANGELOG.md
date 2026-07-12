# Changelog

## v0.2.0

**Broader webcam support**
- Detection no longer assumes "Brio" specifically -- any Logitech device
  is probed, same as before, but classification is now backed by a known
  IR-capable/non-IR-capable model table (Brio 4K/500/505/MX Brio vs.
  Brio 300/100, C920/C922/C930/C925, StreamCam, BCC950, etc.) instead of
  relying purely on the saturation heuristic, which could misclassify a
  dim RGB feed as IR on hardware that never had an IR sensor. Unknown
  models still fall back to the saturation probe as before.

**Lock screen / login screen**
- Added `/etc/pam.d/sddm` as a PAM target -- this is the login screen you
  hit right after a restart, previously not wired at all.
- Added `/etc/pam.d/kscreenlocker-greet` alongside the existing `kde`
  target, since Plasma's lock screen uses different PAM service names
  across distros/versions -- both are wired (safe no-op if a given file
  doesn't apply to your system).
- New `recognition.timeout_seconds_greeter` (default 6s), used instead of
  the sudo-context `timeout_seconds` for greeter/lock-screen PAM
  services, so the lock screen doesn't sit unresponsive for the full
  sudo-context budget.
- Added a camera-busy guard (flock) so a lock screen and a sudo prompt
  triggering face checks at the same moment don't collide on the video
  device.
- Added a boot-readiness retry (`camera_wait_seconds`, default 5s) so a
  USB webcam that hasn't enumerated yet right after a cold boot gets a
  few seconds' grace instead of failing closed on the first try.

**Security**
- New lockout/cooldown: after `lockout.max_failed_attempts` (default 5)
  consecutive failures, face auth is skipped for `lockout.cooldown_seconds`
  (default 300s) and PAM falls straight to password. State lives on
  tmpfs (`/run/facegate`) and resets on reboot. `sudo facegate enable`
  clears any active cooldown.

**Logging**
- New `facegate/logging_setup.py`: auth attempts are now written to
  `/var/log/facegate/facegate.log` (rotating, 5x2MB) in addition to
  syslog, and `facegate log` shows recent entries directly without
  needing `journalctl` syntax.

**New commands**
- `facegate doctor` -- one-shot health check: camera detected, RGB/IR
  configured, models enrolled, PAM wiring, lockout state, log dir.
- `facegate log [-n N]` -- show recent auth attempts.
- `facegate --version`.
- `autosetup`/`enable` now print the detected display manager.

**Known limitation, stated honestly:** the lock-screen/login-screen PAM
wiring above targets the PAM service names these greeters *should* use;
whether `pam_exec` actually fires cleanly from a given greeter still
depends on your specific Plasma/SDDM version and distro packaging, and
isn't something that can be verified without testing on the target
machine. Run `sudo facegate doctor` after `enable`, then test an actual
lock/restart cycle and check `sudo facegate log` / `sudo journalctl -t
facegate -e` if it doesn't fire.
