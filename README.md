# FaceGate

A Howdy-style face-unlock utility for Arch Linux, built around the Logitech
Brio's RGB **and** IR camera streams.



## How it's different from Howdy

Howdy ships a compiled PAM module (`pam_python`-based). FaceGate instead
uses **`pam_exec.so`**, which already ships with `pam` on every Arch
install -- no compiling a PAM module against your kernel/libc, no DKMS-like
breakage on updates. `pam_exec` just runs an external program and reads its
exit code: `0` = authenticated, anything else = "fall through to the next
line in the stack" (i.e. your normal password prompt).

## What it does

- Detects the Brio's `/dev/videoN` nodes via `v4l2-ctl` and classifies them
  as RGB or IR by checking live color saturation (IR streams read back as
  near-grayscale).
- Trains a separate OpenCV LBPH face model per stream at enrollment.
- At auth time, checks both streams and (by default) **requires both to
  match** before allowing the login through.
- Wires into `/etc/pam.d/sudo` (so plain `sudo <anything>`, not just
  `facegate` commands, tries face recognition first), `/etc/pam.d/login`,
  and KDE's `kscreenlocker` service (`/etc/pam.d/kde`).
- Gives up and falls back to your normal password after a configurable
  number of failed recognition attempts (`facegate set-attempts N`,
  default 2) -- it won't sit there indefinitely retrying.
- CLI (`facegate`) for setup, enrollment, testing, enabling/disabling.
- Disabling or uninstalling requires your real account password **or** a
  separate FaceGate PIN you set during setup. This check runs through its
  own isolated PAM service (`facegate-verify`, containing nothing but
  `pam_unix.so`) specifically so a spoofed or successfully-recognized face
  can never be used to satisfy "prove you know the password" and disable
  FaceGate itself -- see the security note below.



## Install
```curl -fsSL https://raw.githubusercontent.com/minerofthesoal/facegate/main/get.sh | bash```

or

```bash
git clone <this repo, or just unzip the files>
cd facegate
sudo ./install.sh
sudo facegate autosetup
```

`autosetup` will:
1. Find and classify your Brio's camera streams.
2. Ask you to type `yes` before capturing your face.
3. Ask you to set a PIN for disabling later.
4. Show you the exact PAM line it wants to add to `/etc/pam.d/sudo` (and
   `/etc/pam.d/login` if present), take a timestamped backup of that file,
   and ask for confirmation before touching it.

## CLI reference

```
sudo facegate autosetup        # full guided first-time setup
sudo facegate enroll [--user X]  # (re)register a face
facegate test [--user X]       # dry-run recognition, changes nothing
sudo facegate enable           # turn face unlock back on
sudo facegate disable          # turn it off (needs sudo password or PIN)
sudo facegate set-pin          # change the disable/uninstall PIN
sudo facegate set-attempts N   # attempts before falling back to password (default 2)
facegate status                # show current config
sudo facegate uninstall        # strip PAM integration (needs sudo pw or PIN)
```

## Tuning

`facegate test` prints raw LBPH confidence numbers (**lower = better
match**). If you're getting false rejects or accepts, edit the thresholds
in `/etc/facegate/config.json` under `recognition.confidence_threshold_rgb`
/ `confidence_threshold_ir`, or flip `require_both` to `false` if your unit
turned out to have no usable IR stream.

If enrollment fails with "not enough face samples," run:

```bash
facegate diag
```

This probes every detected Brio device for a few seconds and reports
resolution, actual frame format, how many frames were read, and how many
of those frames had a detectable face -- so you can tell whether the
problem is "camera isn't delivering frames" vs. "frames are fine but
you're too far/dark for the detector." In the latter case, lower
`recognition.min_face_size` in `/etc/facegate/config.json` (default `80`)
and re-run enrollment.

## Using it beyond `sudo`

`facegate enable`/`autosetup` also wire into `/etc/pam.d/login` (if present)
and, for KDE Plasma, `/etc/pam.d/kde` -- KDE's `kscreenlocker` authenticates
against a PAM service literally called `kde`, whose vendor default on Arch
ships at `/usr/lib/pam.d/kde` rather than `/etc/pam.d/kde`. If `/etc/pam.d/kde`
doesn't exist yet, FaceGate will offer to create it by copying that vendor
default first (so you keep normal password auth as a fallback line) before
adding its own line on top.

For other lockers (GNOME's `gdm-password`, `swaylock`, `hyprlock`, `i3lock`,
etc.), FaceGate doesn't touch them automatically -- add the same
`pam_exec.so` line manually to the right file under `/etc/pam.d/` for that
service. Check `journalctl` after a failed unlock attempt if you're not
sure of the exact service name in use; PAM logs which service name it
looked up.

## Files

```
/etc/facegate/config.json       # settings (root-only)
/etc/facegate/models/*.yml      # per-user, per-stream LBPH models (root-only)
/usr/lib/facegate/facegate/     # the package
/usr/bin/facegate               # CLI
/usr/bin/facegate-auth          # PAM-exec entry point (do not run manually as auth)
```
