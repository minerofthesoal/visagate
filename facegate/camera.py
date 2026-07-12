"""Camera discovery for Logitech webcams.

We enumerate /dev/videoN nodes via `v4l2-ctl --list-devices`, filter to
ones whose description mentions Logitech, then probe each by grabbing a
few live frames and looking at color saturation -- IR streams read back
as near-grayscale even though the device may still report a color pixel
format.

v0.2.0: broadened beyond the Brio specifically. Not every Logitech webcam
has an IR sensor at all (most don't -- only the Windows-Hello-capable
ones do), and running the saturation heuristic against a plain RGB-only
webcam in dim lighting can misclassify its normal feed as "IR". A small
known-device table below short-circuits that for common models we can
name with confidence; anything not in the table still falls back to the
saturation probe, same as before, so unlisted/future models keep working.
"""
import subprocess
import time

import cv2

# Substring (lowercase) -> whether this Logitech model is known to expose
# a second IR stream. Checked against the v4l2-ctl description. Order
# matters: more specific entries are checked before the generic "brio"
# catch-all so e.g. "Brio 300" (no IR) isn't swept up by "brio" (usually
# IR-capable). This list is necessarily incomplete -- anything not
# matched here falls through to the saturation probe as before.
KNOWN_NOT_IR_CAPABLE = (
    "brio 300",
    "brio 100",
    "c920",
    "c922",
    "c930",
    "c925",
    "streamcam",
    "bcc950",
    "b910",
    "b525",
    "c615",
    "c270",
    "rally",
)
KNOWN_IR_CAPABLE = (
    "mx brio",
    "brio 500",
    "brio 505",
    "brio 4k",
    "brio ultra hd",
)


def classify_known_device(description):
    """Return True/False if `description` matches a known model, else None
    (unknown -- caller should fall back to probing)."""
    if not description:
        return None
    desc = description.lower()
    for sub in KNOWN_NOT_IR_CAPABLE:
        if sub in desc:
            return False
    for sub in KNOWN_IR_CAPABLE:
        if sub in desc:
            return True
    if "brio" in desc:
        # Bare "Brio" with no model suffix is almost always the original
        # 4K Ultra HD Pro Webcam, which has IR.
        return True
    return None


def list_video_devices():
    """Return {device_path: description} for all v4l2 devices on the system."""
    devices = {}
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--list-devices"], text=True, stderr=subprocess.DEVNULL
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return devices
    current = None
    for line in out.splitlines():
        if line and not line.startswith(("\t", " ")):
            current = line.split("(")[0].strip()
        elif line.strip().startswith("/dev/video"):
            devices[line.strip()] = current
    return devices


def find_brio_devices():
    """Return sorted list of /dev/videoN paths belonging to any Logitech device.

    Name kept for compatibility with older configs/scripts that import it;
    despite the name this has always matched any Logitech description, not
    just Brio specifically.
    """
    devs = list_video_devices()
    matches = [
        d
        for d, desc in devs.items()
        if desc and ("brio" in desc.lower() or "logitech" in desc.lower())
    ]
    return sorted(matches)


def wait_for_devices(timeout=5, interval=0.5):
    """Poll for Logitech devices to appear, up to `timeout` seconds.

    USB webcams -- especially behind a hub or on a cold boot -- don't
    always enumerate the instant a PAM check runs, most visibly right at
    the SDDM login screen right after startup. Returns the device list
    (possibly empty if none showed up in time). New in v0.2.0.
    """
    deadline = time.time() + timeout
    devices = find_brio_devices()
    while not devices and time.time() < deadline:
        time.sleep(interval)
        devices = find_brio_devices()
    return devices


def probe_device(path, samples=5):
    """Open a device, grab a few frames, return resolution + an IR guess."""
    cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        return None
    sat_values = []
    frame = None
    for _ in range(samples):
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        sat_values.append(float(hsv[:, :, 1].mean()))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if frame is None:
        return None
    avg_sat = sum(sat_values) / len(sat_values) if sat_values else 255.0
    is_ir = avg_sat < 12.0  # near-grayscale => almost certainly the IR stream
    return {
        "path": path,
        "width": w,
        "height": h,
        "avg_saturation": round(avg_sat, 2),
        "is_ir": is_ir,
    }


def auto_detect():
    """Return (rgb_info_or_None, ir_info_or_None, all_probed_devices).

    v0.2.0: if every candidate device belongs to a model we know for a
    fact has no IR sensor (KNOWN_NOT_IR_CAPABLE), we skip the saturation
    guesswork entirely and just report RGB -- avoids misclassifying a
    dim-lit RGB feed as an IR stream on hardware that was never going to
    have one.
    """
    devs = list_video_devices()
    candidates = find_brio_devices()
    results = []
    for path in candidates:
        info = probe_device(path)
        if not info:
            continue
        known = classify_known_device(devs.get(path))
        if known is not None:
            info["is_ir"] = known
            info["classified_by"] = "known_device_table"
        else:
            info["classified_by"] = "saturation_probe"
        results.append(info)
    rgb = next((r for r in results if not r["is_ir"]), None)
    ir = next((r for r in results if r["is_ir"]), None)
    return rgb, ir, results

