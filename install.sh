#!/usr/bin/env bash
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Run as root: sudo ./install.sh"
  exit 1
fi

echo "== Installing system dependencies via pacman =="
pacman -S --needed --noconfirm python python-pip python-numpy opencv v4l-utils base-devel

echo "== Checking for cv2.face (opencv-contrib) =="
if ! python -c "import cv2; cv2.face" >/dev/null 2>&1; then
  echo "cv2.face not found in the pacman opencv package."
  echo "Installing opencv-contrib-python via pip as a fallback..."
  pip install --break-system-packages opencv-contrib-python
fi

echo "== Installing python-pam (isolated password verification) =="
pip install --break-system-packages python-pam

INSTALL_DIR="/usr/lib/facegate"
echo "== Installing FaceGate to $INSTALL_DIR =="
# Wipe any previous install first. cp -r into an *existing* directory nests
# the copy inside it instead of replacing it (facegate/facegate/facegate/...),
# which silently leaves stale/old code in place on re-installs or updates.
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -r facegate "$INSTALL_DIR/"

cat > /usr/bin/facegate <<'EOF'
#!/usr/bin/env python3
import sys
sys.path.insert(0, "/usr/lib/facegate")
from facegate.cli import main
main()
EOF
chmod 755 /usr/bin/facegate

cat > /usr/bin/facegate-auth <<'EOF'
#!/usr/bin/env python3
import sys
sys.path.insert(0, "/usr/lib/facegate")
from facegate.pam_helper import main
main()
EOF
chmod 755 /usr/bin/facegate-auth

echo "== Creating log directory =="
mkdir -p /var/log/facegate
chmod 750 /var/log/facegate

echo ""
echo "Install complete (facegate $(python3 -c "import sys; sys.path.insert(0,'/usr/lib/facegate'); from facegate import __version__; print(__version__)"))."
echo "Run:  sudo facegate autosetup"
echo "Then: sudo facegate doctor    (sanity-check camera + PAM wiring)"
