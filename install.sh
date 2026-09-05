#!/usr/bin/env bash
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Run as root: sudo ./install.sh"
  exit 1
fi

echo "== Installing system dependencies via pacman =="
# python-pam via pacman, not pip -- it's an official Arch package (matches
# the AUR PKGBUILD's depends=), and installing it via pip instead creates
# untracked files under site-packages that later collide with pacman's own
# copy the moment anyone (including the AUR package itself) tries to
# install python-pam properly. Learned this the hard way.
pacman -S --needed --noconfirm python python-pip python-numpy opencv python-pam v4l-utils base-devel

echo "== Checking for cv2.face (opencv-contrib) =="
if ! python -c "import cv2; cv2.face" >/dev/null 2>&1; then
  echo "cv2.face not found in the pacman opencv package."
  echo "Installing opencv-contrib-python via pip as a fallback..."
  pip install --break-system-packages opencv-contrib-python
fi

echo "== Installing Visagate =="
# pip install . (not a manual copy to /usr/lib + hand-written wrapper
# scripts) so this produces the EXACT same file layout as the AUR
# package: proper site-packages install + auto-generated /usr/bin
# entry-point scripts. Two different layouts for the same software is
# exactly what caused a real /usr/bin/visagate* file conflict the first
# time this was tested against the AUR build -- --no-deps because numpy
# and python-pam are already handled via pacman above, matching how the
# AUR package resolves them through pacman's depends= rather than pip.
pip install --break-system-packages --force-reinstall --no-deps .

echo "== Creating log directory =="
mkdir -p /var/log/visagate
chmod 1777 /var/log/visagate

echo "== Checking camera device group access =="
# /dev/video* are typically root:video 0660. visagate-auth runs as root
# under sudo (fine either way), but as your actual logged-in user under
# kscreenlocker/some greeters -- if that user isn't in the "video" group,
# it can wire up PAM perfectly and still never be able to open the camera
# at all. Fixing this needs a fresh login session to take effect for any
# already-running desktop session (group membership is set at login time
# and doesn't refresh for a running session), so it's flagged clearly
# rather than silently assumed to have worked immediately.
TARGET_USER="${SUDO_USER:-}"
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ]; then
  if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx video; then
    usermod -aG video "$TARGET_USER"
    echo "Added $TARGET_USER to the 'video' group (needed for camera access from"
    echo "non-root PAM contexts like the KDE lock screen). This only takes effect"
    echo "after you log out and back in (or reboot) -- it will NOT apply to your"
    echo "current session."
  else
    echo "$TARGET_USER is already in the 'video' group."
  fi
else
  echo "Could not determine the invoking user (\$SUDO_USER unset) -- skipping."
  echo "If lock-screen face checks fail to even open the camera, run:"
  echo "  sudo usermod -aG video <your-username>   # then log out and back in"
fi

echo ""
echo "Install complete (visagate $(python3 -c "from visagate import __version__; print(__version__)"))."
echo "Run:  sudo visagate autosetup"
echo "Then: sudo visagate doctor    (sanity-check camera + PAM wiring)"
