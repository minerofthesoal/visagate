"""Face enrollment / recognition.

Uses OpenCV's LBPH face recognizer (opencv-contrib's cv2.face module) on
both the RGB and IR streams separately, storing one model file per stream
per user under /etc/facegate/models. At auth time both streams are
checked and (by default) both must agree, which is a meaningfully higher
bar than a single RGB camera check since a printed photo or phone screen
generally will not read correctly on the IR stream.

This is NOT structured-light depth sensing. It will not stop every
spoofing attempt a $30 Windows Hello depth camera would. See README.md
for the honest threat model.
"""
import os
import time

import cv2
import numpy as np

from . import config

# Prefer the copy we ship (works regardless of how the local OpenCV package
# lays out its data dir -- on Arch the pacman `opencv` package and the pip
# `opencv-contrib-python` wheel don't agree on where haarcascades live, and
# sometimes cv2.data.haarcascades points at a path with nothing in it).
_BUNDLED_CASCADE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "haarcascade_frontalface_default.xml"
)


def _cascade_path():
    candidates = [_BUNDLED_CASCADE]
    try:
        candidates.append(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    except Exception:
        pass
    candidates += [
        "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
        "/usr/share/OpenCV/haarcascades/haarcascade_frontalface_default.xml",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise RuntimeError(
        "Could not locate haarcascade_frontalface_default.xml anywhere "
        "(checked bundled copy + cv2.data + common system paths). "
        "Reinstall FaceGate or place the file at "
        f"{_BUNDLED_CASCADE}"
    )


def _detector():
    path = _cascade_path()
    clf = cv2.CascadeClassifier(path)
    if clf.empty():
        raise RuntimeError(f"OpenCV failed to load cascade file at {path} (file may be corrupt).")
    return clf


def _grab_faces(device_path, num_samples, timeout, min_face_size=80, verbose=True):
    cap = cv2.VideoCapture(device_path, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera device {device_path}")
    detector = _detector()
    faces = []
    frames_read = 0
    detections_seen = 0
    start = time.time()
    last_report = start
    try:
        while len(faces) < num_samples and time.time() - start < timeout:
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            frames_read += 1
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            detected = detector.detectMultiScale(
                gray, 1.2, 5, minSize=(min_face_size, min_face_size)
            )
            if len(detected):
                detections_seen += 1
            for (x, y, w, h) in detected:
                crop = cv2.resize(gray[y : y + h, x : x + w], (200, 200))
                faces.append(crop)
                break
            if verbose and time.time() - last_report > 2:
                print(
                    f"    ...{frames_read} frames read, face seen in {detections_seen} of them, "
                    f"{len(faces)}/{num_samples} samples collected"
                )
                last_report = time.time()
    finally:
        cap.release()
    if verbose and frames_read == 0:
        print(f"    WARNING: 0 frames were successfully read from {device_path}.")
    elif verbose and detections_seen == 0:
        print(
            f"    WARNING: {frames_read} frames read but no face was ever detected. "
            f"Move closer to the camera, improve lighting, or lower "
            f"recognition.min_face_size in /etc/facegate/config.json (currently {min_face_size})."
        )
    return faces


MIN_SAMPLES = 5


def _train_or_update(path, faces, append=False):
    """Write an LBPH model to `path`. If `append` is True and a model
    already exists there, load it and add `faces` via update() (which
    keeps everything the model already learned); otherwise train a fresh
    model from scratch with just `faces`."""
    recognizer = cv2.face.LBPHFaceRecognizer_create()
    labels = np.array([0] * len(faces))
    if append and os.path.exists(path):
        recognizer.read(path)
        recognizer.update(faces, labels)
    else:
        recognizer.train(faces, labels)
    recognizer.write(path)


def enroll_user(username, rgb_device, ir_device, samples=25, timeout=25, append=False):
    """Capture face samples from whichever device(s) are configured and
    train + save an LBPH model per stream. Returns a summary dict.

    If the RGB stream can't come up with enough samples (bad lighting, the
    RGB sensor just not picking up a face, etc.) but the IR stream did, the
    IR-detected face crops are reused to train the RGB model too rather than
    failing enrollment outright -- both crops are already normalized to
    grayscale 200x200 for LBPH, so an IR crop is a legitimate substitute.
    This does weaken the "two independent streams" guarantee for that user
    until they re-enroll with working RGB capture; we surface that in the
    result dict so callers can warn about it.

    If `append` is True and a model already exists for this user, the new
    samples are added to it via LBPH's update() instead of replacing it via
    train(). This is the right tool for "my face looks different sometimes"
    cases -- most commonly glasses vs. no glasses, since LBPH's local
    texture features around the eyes are sensitive to lens glare/frames,
    so a model that's only ever seen you one way can under-match the
    other. Run enroll normally once, then again with --append while
    wearing (or not wearing) glasses to teach the model both looks,
    rather than one appearance overwriting the other.
    """
    if not rgb_device and not ir_device:
        raise RuntimeError("No camera devices configured. Run 'facegate autosetup' first.")

    cfg = config.load()
    min_face_size = cfg["recognition"].get("min_face_size", 80)
    result = {}

    rgb_faces = []
    if rgb_device:
        print(f"  Capturing RGB samples from {rgb_device}...")
        rgb_faces = _grab_faces(rgb_device, samples, timeout, min_face_size=min_face_size)

    ir_faces = []
    if ir_device:
        print(f"  Capturing IR samples from {ir_device}...")
        ir_faces = _grab_faces(ir_device, samples, timeout, min_face_size=min_face_size)
        if len(ir_faces) < MIN_SAMPLES:
            raise RuntimeError(
                "Not enough IR face samples captured. Make sure nothing is "
                "covering the IR sensor and try again."
            )

    if rgb_device:
        rgb_source = rgb_faces
        used_ir_fallback = False
        if len(rgb_faces) < MIN_SAMPLES:
            if len(ir_faces) >= MIN_SAMPLES:
                print(
                    f"  WARNING: only {len(rgb_faces)}/{MIN_SAMPLES} RGB samples captured; "
                    "falling back to IR-detected face crops for the RGB model."
                )
                rgb_source = ir_faces
                used_ir_fallback = True
            else:
                raise RuntimeError(
                    "Not enough RGB face samples captured. Face the camera directly "
                    "in good, even lighting and try again."
                )
        path = os.path.join(config.MODEL_DIR, f"{username}_rgb.yml")
        _train_or_update(path, rgb_source, append=append)
        _lock_down(path)
        result["rgb_samples"] = len(rgb_source)
        result["rgb_used_ir_fallback"] = used_ir_fallback
        result["rgb_appended"] = append and os.path.exists(path)

    if ir_device:
        path = os.path.join(config.MODEL_DIR, f"{username}_ir.yml")
        _train_or_update(path, ir_faces, append=append)
        _lock_down(path)
        result["ir_samples"] = len(ir_faces)
        result["ir_appended"] = append and os.path.exists(path)

    return result


def _lock_down(path):
    try:
        os.chmod(path, 0o600)
    except PermissionError:
        pass


def _authenticate_stream(device_path, model_path, threshold, max_attempts, timeout, min_face_size=80):
    """Try up to `max_attempts` distinct passes at recognizing a face,
    each given an even slice of `timeout` seconds. Returns as soon as one
    pass matches; gives up (returns False) once attempts are exhausted so
    the caller/PAM can fall back to password auth quickly and predictably.

    Returns (matched, best_conf, detected_any). `detected_any` is True if
    the detector ever found a face-shaped region in a frame, regardless of
    whether it matched the enrolled model -- this lets the caller tell
    "this stream never saw a face" (a camera/detection problem) apart from
    "this stream saw a face but it didn't match" (a real non-match)."""
    if not device_path or not os.path.exists(model_path):
        return False, None, False
    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.read(model_path)
    cap = cv2.VideoCapture(device_path, cv2.CAP_V4L2)
    if not cap.isOpened():
        return False, None, False
    detector = _detector()
    best_conf = None
    detected_any = False
    max_attempts = max(1, max_attempts)
    slice_seconds = max(1.0, timeout / max_attempts)
    try:
        for _attempt in range(max_attempts):
            deadline = time.time() + slice_seconds
            matched = False
            while time.time() < deadline:
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                detected = detector.detectMultiScale(
                    gray, 1.2, 5, minSize=(min_face_size, min_face_size)
                )
                for (x, y, w, h) in detected:
                    detected_any = True
                    crop = cv2.resize(gray[y : y + h, x : x + w], (200, 200))
                    _label, conf = recognizer.predict(crop)
                    if best_conf is None or conf < best_conf:
                        best_conf = conf
                    if conf <= threshold:
                        matched = True
                        break
                if matched:
                    break
            if matched:
                return True, best_conf, detected_any
    finally:
        cap.release()
    return False, best_conf, detected_any


def authenticate(username, timeout_override=None):
    """Run recognition against whichever streams are configured for this
    user. Returns (bool success, info dict with raw confidences).

    `timeout_override`, new in v0.2.0: lets callers (specifically
    pam_helper, for greeter/lock-screen PAM services) use a shorter time
    budget than the configured recognition.timeout_seconds, since a
    lock-screen sitting unresponsive for the full sudo-context timeout
    reads as broken rather than "still checking."

    If both streams are required to match but one of them (either one)
    never detected a face at all during the whole attempt window -- as
    opposed to detecting a face and failing to match it -- the other
    stream's result is used alone instead of failing outright. A stream
    that never sees a face is a camera/detection problem, not evidence of
    a spoofing attempt, so it shouldn't veto a stream that did positively
    identify the person. A stream that DID detect a face but didn't match
    still fails the whole check, since that's a real non-match.
    """
    cfg = config.load()
    cam = cfg["camera"]
    rec = cfg["recognition"]
    min_face_size = rec.get("min_face_size", 80)
    timeout_seconds = timeout_override or rec["timeout_seconds"]

    rgb_ok, rgb_conf, rgb_detected = _authenticate_stream(
        cam.get("rgb_device"),
        os.path.join(config.MODEL_DIR, f"{username}_rgb.yml"),
        rec["confidence_threshold_rgb"],
        rec["max_attempts"],
        timeout_seconds,
        min_face_size=min_face_size,
    )
    ir_ok, ir_conf, ir_detected = _authenticate_stream(
        cam.get("ir_device"),
        os.path.join(config.MODEL_DIR, f"{username}_ir.yml"),
        rec["confidence_threshold_ir"],
        rec["max_attempts"],
        timeout_seconds,
        min_face_size=min_face_size,
    )

    have_rgb = bool(cam.get("rgb_device"))
    have_ir = bool(cam.get("ir_device"))
    used_single_stream_fallback = None

    if have_rgb and have_ir and rec.get("require_both", True):
        if not rgb_detected and ir_detected:
            success = ir_ok
            used_single_stream_fallback = "ir"
        elif not ir_detected and rgb_detected:
            success = rgb_ok
            used_single_stream_fallback = "rgb"
        else:
            success = rgb_ok and ir_ok
    elif have_rgb and have_ir:
        success = rgb_ok or ir_ok
    elif have_rgb:
        success = rgb_ok
    elif have_ir:
        success = ir_ok
    else:
        success = False

    return success, {
        "rgb_conf": rgb_conf,
        "ir_conf": ir_conf,
        "used_single_stream_fallback": used_single_stream_fallback,
    }
