#!/usr/bin/env python3
import argparse
import datetime
import getpass
import os
import shutil
import sys
import time

from . import __version__, camera, config, recognizer, security

PAM_MARKER = "facegate-auth"
PAM_LINE = f"auth    sufficient   pam_exec.so quiet /usr/bin/{PAM_MARKER}\n"

# target PAM file -> vendor-default fallback to seed it from, if the target
# doesn't exist yet. Arch (and other systemd-based distros) ship many
# service defaults under /usr/lib/pam.d/ rather than /etc/pam.d/; PAM reads
# /etc/pam.d/<service> if present, otherwise falls back to the vendor copy.
# To add our line we need a real file under /etc/pam.d/, so if only the
# vendor copy exists we seed /etc/pam.d/<service> from it first.
#
# v0.2.0: added sddm (the login-manager screen you hit right after a
# restart) and kscreenlocker-greet (the PAM service name Plasma's lock
# screen uses on some distros/versions, alongside the older "kde" name --
# both are wired since which one is actually in play varies, and wiring
# a service whose file doesn't exist on your system is a safe no-op).
PAM_TARGETS = {
    "/etc/pam.d/sudo": None,
    "/etc/pam.d/login": None,
    "/etc/pam.d/kde": "/usr/lib/pam.d/kde",  # KDE Plasma's kscreenlocker (kcheckpass), older naming
    "/etc/pam.d/kscreenlocker-greet": "/usr/lib/pam.d/kscreenlocker-greet",  # same, newer naming
    "/etc/pam.d/sddm": "/usr/lib/pam.d/sddm",  # SDDM login screen, i.e. right after a restart
}

GREETER_SERVICES = {"sddm", "sddm-greeter", "kde", "kde-np", "kscreenlocker-greet"}


def detect_display_manager():
    """Best-effort name of the active display manager (sddm/gdm/lightdm/...),
    via the systemd display-manager.service symlink. Informational only --
    used to tell the user which lock-screen PAM file is actually relevant
    to them, not to gate anything. New in v0.2.0."""
    try:
        target = os.path.realpath("/etc/systemd/system/display-manager.service")
        name = os.path.basename(target).replace(".service", "")
        return name or None
    except OSError:
        return None


def require_root():
    if os.geteuid() != 0:
        print("This command must be run as root (use sudo).")
        sys.exit(1)


def cmd_autosetup(args):
    require_root()
    print("== FaceGate autosetup ==")
    dm = detect_display_manager()
    print(f"Detected display manager: {dm or 'unknown'}")
    print("Scanning for Logitech camera devices (v4l2-ctl)...")
    rgb, ir, all_devs = camera.auto_detect()

    if not all_devs:
        print("No Brio/Logitech devices found.")
        print("Check that:")
        print("  - v4l-utils is installed (pacman -S v4l-utils)")
        print("  - the webcam is plugged in")
        print("  - `v4l2-ctl --list-devices` shows it at all")
        sys.exit(1)

    print("\nDevices found:")
    for d in all_devs:
        kind = "IR (guess)" if d["is_ir"] else "RGB (guess)"
        print(f"  {d['path']}  {d['width']}x{d['height']}  avg_sat={d['avg_saturation']}  -> {kind}")

    if not rgb and not ir:
        print("\nCould not confidently classify any stream as RGB or IR.")
        print("Edit /etc/facegate/config.json manually to set camera.rgb_device / camera.ir_device.")
        sys.exit(1)

    cfg = config.load()
    cfg["camera"]["rgb_device"] = rgb["path"] if rgb else None
    cfg["camera"]["ir_device"] = ir["path"] if ir else None
    cfg["camera"]["auto_detected"] = True
    config.save(cfg)

    security.ensure_verify_service()

    print(f"\nSelected RGB device: {cfg['camera']['rgb_device']}")
    print(f"Selected IR device:  {cfg['camera']['ir_device']}")
    if not ir:
        print("Note: no IR stream detected. Your Brio unit may not have IR, or it")
        print("needs a different capture mode. FaceGate will run RGB-only.")

    answer = input("\nType 'yes' to begin face enrollment now: ").strip().lower()
    if answer != "yes":
        print("Setup paused. Run 'sudo facegate enroll' when you're ready.")
        return

    username = os.environ.get("SUDO_USER") or getpass.getuser()
    _do_enroll(username, cfg)

    attempts_input = input(
        "\nHow many face-recognition attempts before falling back to your "
        "password? [default 2]: "
    ).strip()
    try:
        attempts = int(attempts_input) if attempts_input else 2
    except ValueError:
        attempts = 2
    cfg = config.load()
    cfg["recognition"]["max_attempts"] = max(1, attempts)
    config.save(cfg)
    print(f"Will try face recognition {cfg['recognition']['max_attempts']} time(s) before asking for your password.")

    print("\nSet a PIN you can use later to disable face unlock without your full")
    print("sudo password (sudo password will also always work).")
    pin = getpass.getpass("New PIN: ")
    confirm = getpass.getpass("Confirm PIN: ")
    if pin != confirm:
        print("PINs did not match. Run 'sudo facegate set-pin' to try again.")
    else:
        security.set_pin(pin)
        print("PIN saved.")

    _install_pam(interactive=True)

    cfg = config.load()
    cfg["enabled"] = True
    config.save(cfg)
    print("\nFaceGate is enabled. Test it with: sudo -k && sudo true")
    print("(If it doesn't recognize you, it silently falls back to your password.)")


def _do_enroll(username, cfg, append=False):
    print(f"Enrolling face for user '{username}'.")
    if append:
        print("Appending new samples to the existing model (previous samples are kept).")
    print("Look directly at the camera and move your head slightly during capture.")
    try:
        result = recognizer.enroll_user(
            username, cfg["camera"]["rgb_device"], cfg["camera"]["ir_device"], append=append
        )
    except RuntimeError as e:
        print(f"Enrollment failed: {e}")
        sys.exit(1)
    print(f"Enrollment complete: {result}")
    if result.get("rgb_used_ir_fallback"):
        print(
            "NOTE: the RGB model was trained from IR-detected face crops because "
            "not enough RGB samples were captured. Two-stream verification is "
            "weaker for this user until you re-run 'sudo facegate enroll' with "
            "working RGB capture (check lighting / camera angle)."
        )


def cmd_enroll(args):
    require_root()
    cfg = config.load()
    username = args.user or os.environ.get("SUDO_USER") or getpass.getuser()
    _do_enroll(username, cfg, append=args.append)


def cmd_test(args):
    username = args.user or os.environ.get("SUDO_USER") or getpass.getuser()
    print(f"Testing recognition for '{username}' (this will NOT unlock or change anything)...")
    ok, info = recognizer.authenticate(username)
    print(f"Result: {'MATCH' if ok else 'NO MATCH'}")
    print(f"Raw LBPH confidences (lower = better match): {info}")
    if info.get("used_single_stream_fallback"):
        stream = info["used_single_stream_fallback"]
        other = "IR" if stream == "rgb" else "RGB"
        print(
            f"NOTE: the {other} stream never detected a face at all this attempt, "
            f"so the result above was decided by {stream.upper()} alone."
        )


def _install_pam(interactive=True):
    installed_any = False
    for pam_file, vendor_fallback in PAM_TARGETS.items():
        if not os.path.exists(pam_file):
            if vendor_fallback and os.path.exists(vendor_fallback):
                if interactive:
                    print(f"\n{pam_file} doesn't exist yet; found vendor default at {vendor_fallback}.")
                    confirm = input(f"Create {pam_file} from that vendor default? [y/N]: ").strip().lower()
                    if confirm != "y":
                        print(f"Skipped {pam_file}.")
                        continue
                shutil.copy2(vendor_fallback, pam_file)
                print(f"Created {pam_file} from {vendor_fallback}.")
            else:
                continue  # no such service on this system, nothing to do

        with open(pam_file) as f:
            content = f.read()
        lines = content.splitlines(keepends=True)
        marker_idxs = [i for i, l in enumerate(lines) if PAM_MARKER in l]

        if marker_idxs:
            if all(lines[i] == PAM_LINE for i in marker_idxs):
                print(f"{pam_file}: already configured, skipping.")
                installed_any = True
                continue
            # A FaceGate line exists but doesn't match the current PAM_LINE --
            # most likely an older version (e.g. the expose_authtok flag that
            # used to force a password prompt before face auth even ran).
            # Repair it in place rather than leaving the stale behavior.
            if interactive:
                print(f"\n{pam_file} has an outdated FaceGate line:")
                for i in marker_idxs:
                    print("  - " + lines[i].strip())
                print("Replacing with:")
                print("  + " + PAM_LINE.strip())
                confirm = input("Proceed? [y/N]: ").strip().lower()
                if confirm != "y":
                    print(f"Skipped {pam_file}.")
                    continue
            backup = pam_file + f".facegate.bak.{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
            shutil.copy2(pam_file, backup)
            for i in marker_idxs:
                lines[i] = PAM_LINE
            with open(pam_file, "w") as f:
                f.writelines(lines)
            print(f"{pam_file} repaired. Backup: {backup}")
            installed_any = True
            continue

        if interactive:
            print(f"\nAbout to insert a FaceGate auth line into {pam_file}:")
            print("  " + PAM_LINE.strip())
            confirm = input("Proceed? [y/N]: ").strip().lower()
            if confirm != "y":
                print(f"Skipped {pam_file}.")
                continue
        backup = pam_file + f".facegate.bak.{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(pam_file, backup)
        lines.insert(0, PAM_LINE)
        with open(pam_file, "w") as f:
            f.writelines(lines)
        print(f"{pam_file} updated. Backup: {backup}")
        installed_any = True
    if not installed_any:
        print("No PAM files were modified.")


def cmd_enable(args):
    require_root()
    security.ensure_verify_service()
    _install_pam(interactive=True)
    security.clear_lockout()
    cfg = config.load()
    cfg["enabled"] = True
    config.save(cfg)
    print("Face unlock enabled.")


def cmd_disable(args):
    require_root()
    if not security.confirm_privileged_action("Disabling face unlock requires confirmation."):
        print("Confirmation failed. Face unlock remains enabled.")
        sys.exit(1)
    cfg = config.load()
    cfg["enabled"] = False
    config.save(cfg)
    print("Face unlock disabled. Only your normal password will be accepted.")


def cmd_set_pin(args):
    require_root()
    if config.load().get("pin_hash"):
        if not security.confirm_privileged_action("Changing your PIN requires confirmation."):
            print("Confirmation failed.")
            sys.exit(1)
    pin = getpass.getpass("New PIN: ")
    confirm = getpass.getpass("Confirm PIN: ")
    if pin != confirm:
        print("PINs did not match.")
        sys.exit(1)
    security.set_pin(pin)
    print("PIN updated.")


def cmd_set_attempts(args):
    require_root()
    if args.count < 1:
        print("Attempts must be at least 1.")
        sys.exit(1)
    cfg = config.load()
    cfg["recognition"]["max_attempts"] = args.count
    config.save(cfg)
    print(f"Will try face recognition {args.count} time(s) before falling back to your password.")


def cmd_relax(args):
    """Make matching more permissive: raise LBPH confidence thresholds
    (LBPH confidence is a distance -- lower is a better match -- so a
    HIGHER threshold accepts weaker/less-perfect matches) and/or lower
    min_face_size so faces are detected from farther away or at an angle.
    Trades some false-reject reduction for a slightly higher false-accept
    risk; this is a meaningful security/convenience tradeoff, not just a
    UX tweak, so it prints the before/after values rather than doing it
    silently.
    """
    require_root()
    cfg = config.load()
    rec = cfg["recognition"]
    before = dict(rec)

    if args.rgb_threshold is not None:
        rec["confidence_threshold_rgb"] = args.rgb_threshold
    if args.ir_threshold is not None:
        rec["confidence_threshold_ir"] = args.ir_threshold
    if args.min_face_size is not None:
        rec["min_face_size"] = args.min_face_size

    if not any([args.rgb_threshold, args.ir_threshold, args.min_face_size]):
        # No explicit values given -> apply a sensible one-step loosening.
        rec["confidence_threshold_rgb"] = min(100, before["confidence_threshold_rgb"] + 15)
        rec["confidence_threshold_ir"] = min(100, before["confidence_threshold_ir"] + 15)
        rec["min_face_size"] = max(40, before["min_face_size"] - 20)

    config.save(cfg)
    print("Recognition is now more permissive:")
    print(
        f"  confidence_threshold_rgb: {before['confidence_threshold_rgb']} -> {rec['confidence_threshold_rgb']}"
    )
    print(
        f"  confidence_threshold_ir:  {before['confidence_threshold_ir']} -> {rec['confidence_threshold_ir']}"
    )
    print(f"  min_face_size:            {before['min_face_size']} -> {rec['min_face_size']}")
    print(
        "\nNote: higher thresholds and smaller min_face_size make it easier for YOU to "
        "pass, but also easier for a false match. Run 'sudo facegate test' to sanity-check."
    )


def cmd_diag(args):
    import cv2

    print("Probing all detected Brio/Logitech devices for ~3 seconds each...\n")
    candidates = camera.find_brio_devices()
    if not candidates:
        print("No Brio/Logitech devices found via v4l2-ctl.")
        return
    cfg = config.load()
    min_face_size = cfg["recognition"].get("min_face_size", 80)
    detector = recognizer._detector()
    for path in candidates:
        print(f"=== {path} ===")
        cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
        if not cap.isOpened():
            print("  Could not open device.\n")
            continue
        fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC))
        fourcc = "".join(chr((fourcc_int >> 8 * i) & 0xFF) for i in range(4)) if fourcc_int else "?"
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frames_read = 0
        detections = 0
        brightness_sum = 0.0
        start = time.time()
        while time.time() - start < 3:
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            frames_read += 1
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            brightness_sum += float(gray.mean())
            found = detector.detectMultiScale(gray, 1.2, 5, minSize=(min_face_size, min_face_size))
            if len(found):
                detections += 1
        cap.release()
        avg_brightness = brightness_sum / frames_read if frames_read else float("nan")
        print(f"  resolution: {w}x{h}   fourcc: {fourcc}")
        print(f"  frames read in 3s: {frames_read}")
        print(f"  avg brightness (0-255): {avg_brightness:.1f}")
        print(f"  frames with a detected face (min_face_size={min_face_size}): {detections}/{frames_read}")
        if frames_read == 0:
            print("  -> device isn't delivering frames at all.")
        elif detections == 0:
            print("  -> frames are fine but no face was ever detected here. Move closer / "
                  "improve lighting / lower recognition.min_face_size.")
        print()


def cmd_status(args):
    cfg = config.load()
    print(f"Enabled:      {cfg['enabled']}")
    print(f"RGB device:   {cfg['camera']['rgb_device']}")
    print(f"IR device:    {cfg['camera']['ir_device']}")
    print(f"Require both streams to match: {cfg['recognition']['require_both']}")
    print(f"Max attempts before password fallback: {cfg['recognition']['max_attempts']}")
    print(f"PIN set:      {bool(cfg.get('pin_hash'))}")
    enrolled = []
    if os.path.isdir(config.MODEL_DIR):
        for fn in sorted(os.listdir(config.MODEL_DIR)):
            enrolled.append(fn)
    print(f"Model files:  {enrolled or '(none)'}")


def cmd_uninstall(args):
    require_root()
    if not security.confirm_privileged_action("Uninstalling FaceGate requires confirmation."):
        sys.exit(1)
    for pam_file in PAM_TARGETS:
        if not os.path.exists(pam_file):
            continue
        with open(pam_file) as f:
            lines = f.readlines()
        new_lines = [l for l in lines if PAM_MARKER not in l]
        if new_lines != lines:
            with open(pam_file, "w") as f:
                f.writelines(new_lines)
            print(f"Removed FaceGate line from {pam_file}")
    print("PAM integration removed.")
    print(f"Face model files are still in {config.MODEL_DIR} -- delete manually if you want them gone too.")


def cmd_doctor(args):
    """Health check: camera, PAM wiring, enrolled models, lockout state,
    logging. New in v0.2.0."""
    require_root()
    from . import logging_setup

    print("== FaceGate doctor ==")
    ok_all = True

    def check(label, ok, detail=""):
        nonlocal ok_all
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))
        if not ok:
            ok_all = False

    cfg = config.load()
    check("face unlock enabled", cfg.get("enabled"), "" if cfg.get("enabled") else "run 'sudo facegate enable'")

    dm = detect_display_manager()
    print(f"     display manager: {dm or 'unknown'}")

    devs = camera.list_video_devices()
    logi = sorted({d for d, desc in devs.items() if desc and "logitech" in desc.lower()})
    check("Logitech camera(s) detected", bool(logi), ", ".join(logi) if logi else "none found via v4l2-ctl")

    rgb = cfg["camera"].get("rgb_device")
    ir = cfg["camera"].get("ir_device")
    check("RGB device configured", bool(rgb), rgb or "none -- run 'sudo facegate autosetup'")
    check("IR device configured", bool(ir), ir or "none (RGB-only mode -- fine if your webcam has no IR sensor)")

    username = os.environ.get("SUDO_USER") or getpass.getuser()
    rgb_model = os.path.join(config.MODEL_DIR, f"{username}_rgb.yml")
    ir_model = os.path.join(config.MODEL_DIR, f"{username}_ir.yml")
    if rgb:
        check(f"RGB model enrolled ({username})", os.path.exists(rgb_model))
    if ir:
        check(f"IR model enrolled ({username})", os.path.exists(ir_model))

    for pam_file in PAM_TARGETS:
        if not os.path.exists(pam_file):
            print(f"[skip] PAM file not present: {pam_file} (not applicable on this system)")
            continue
        with open(pam_file) as f:
            wired = PAM_MARKER in f.read()
        check(f"PAM wired: {pam_file}", wired)

    locked, remaining = security.is_locked_out()
    check("not currently locked out", not locked, f"{remaining}s remaining" if locked else "")

    try:
        os.makedirs(logging_setup.LOG_DIR, exist_ok=True)
        check("log directory writable", os.access(logging_setup.LOG_DIR, os.W_OK))
    except PermissionError:
        check("log directory writable", False)

    print("\nAll checks passed." if ok_all else "\nSome checks failed -- see above.")


def cmd_log(args):
    """Show recent auth attempts from the FaceGate log file. New in v0.2.0."""
    from . import logging_setup

    try:
        with open(logging_setup.LOG_FILE) as f:
            lines = f.readlines()[-args.n :]
    except FileNotFoundError:
        print(f"No log file yet at {logging_setup.LOG_FILE}.")
        print("Either nothing has run through PAM yet, or you're on syslog-only:")
        print("  sudo journalctl -t facegate -e")
        return
    except PermissionError:
        print(f"Permission denied reading {logging_setup.LOG_FILE} -- try with sudo.")
        return
    for line in lines:
        print(line.rstrip())



def main():
    parser = argparse.ArgumentParser(
        prog="facegate",
        description="Face unlock for Logitech webcams on Arch Linux (Howdy-style, RGB+IR where available).",
    )
    parser.add_argument("--version", action="version", version=f"facegate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("autosetup", help="Detect camera, enroll your face, wire up PAM").set_defaults(
        func=cmd_autosetup
    )

    p = sub.add_parser("enroll", help="(Re)register a face")
    p.add_argument("--user", help="username to enroll (default: current user)")
    p.add_argument(
        "--append",
        action="store_true",
        help="Add new samples to the existing model instead of replacing it "
        "(e.g. re-run wearing glasses to teach both looks)",
    )
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser("test", help="Test recognition without touching auth/config")
    p.add_argument("--user")
    p.set_defaults(func=cmd_test)

    sub.add_parser(
        "diag", help="Probe all detected camera devices and report frame/face-detection stats"
    ).set_defaults(func=cmd_diag)

    sub.add_parser("enable", help="(Re)enable face unlock and PAM hook").set_defaults(func=cmd_enable)
    sub.add_parser(
        "disable", help="Disable face unlock (requires sudo password or PIN)"
    ).set_defaults(func=cmd_disable)
    sub.add_parser("set-pin", help="Set or change the disable/uninstall PIN").set_defaults(
        func=cmd_set_pin
    )
    sub.add_parser("status", help="Show current configuration").set_defaults(func=cmd_status)

    p = sub.add_parser(
        "set-attempts", help="Set how many face-match attempts before falling back to password"
    )
    p.add_argument("count", type=int)
    p.set_defaults(func=cmd_set_attempts)

    p = sub.add_parser(
        "relax",
        help="Make matching more permissive (raise confidence thresholds, lower min_face_size)",
    )
    p.add_argument("--rgb-threshold", type=int, default=None, help="New confidence_threshold_rgb (higher = looser)")
    p.add_argument("--ir-threshold", type=int, default=None, help="New confidence_threshold_ir (higher = looser)")
    p.add_argument("--min-face-size", type=int, default=None, help="New min_face_size in pixels (lower = looser)")
    p.set_defaults(func=cmd_relax)

    sub.add_parser("uninstall", help="Remove PAM integration").set_defaults(func=cmd_uninstall)

    sub.add_parser(
        "doctor", help="Run health checks: camera, PAM wiring, enrolled models, lockout, logs"
    ).set_defaults(func=cmd_doctor)

    p = sub.add_parser("log", help="Show recent auth attempts from the FaceGate log")
    p.add_argument("-n", type=int, default=20, help="number of lines to show (default 20)")
    p.set_defaults(func=cmd_log)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
