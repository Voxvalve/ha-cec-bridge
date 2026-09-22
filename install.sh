#!/usr/bin/env bash
# Installs the CEC bridge on a Raspberry Pi (Raspberry Pi OS Bookworm or newer).
# Usage:  sudo ./install.sh
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo ./install.sh"
  exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET=/opt/cec-bridge

need_pkg() {  # $1 = package, $2 = a file that proves it is installed
  [ -e "$2" ] && return 1
  command -v "$(basename "$2")" >/dev/null 2>&1 && return 1
  return 0
}

MISSING=""
command -v cec-ctl >/dev/null 2>&1 || MISSING="$MISSING v4l-utils"
python3 -c "import paho.mqtt" >/dev/null 2>&1 || MISSING="$MISSING python3-paho-mqtt"

if [ -n "$MISSING" ]; then
  echo "==> Installing packages:$MISSING"
  # A broken or unreachable third-party repo must not stop the install, so
  # the update is advisory and only the install itself has to succeed.
  apt-get update -qq 2>/dev/null || \
    echo "    (apt-get update reported problems; carrying on with the cached lists)"
  # shellcheck disable=SC2086
  if ! apt-get install -y $MISSING; then
    echo
    echo "Could not install:$MISSING"
    echo "Install them by hand and re-run:  sudo apt install$MISSING"
    exit 1
  fi
else
  echo "==> Packages already present (v4l-utils, python3-paho-mqtt)"
fi

echo "==> Copying files to $TARGET"
install -d "$TARGET"
install -m 755 "$DIR/cec_bridge.py" "$TARGET/cec_bridge.py"
install -m 644 "$DIR/cec-bridge.service" /etc/systemd/system/cec-bridge.service

echo "==> Starting service"
systemctl daemon-reload
systemctl enable cec-bridge >/dev/null 2>&1 || true
systemctl restart cec-bridge

sleep 1
if ! systemctl is-active --quiet cec-bridge; then
  echo
  echo "The service did not stay running. Recent log:"
  journalctl -u cec-bridge -n 15 --no-pager 2>/dev/null || true
  exit 1
fi

if [ ! -e /dev/cec0 ]; then
  echo
  echo "WARNING: /dev/cec0 does not exist. Check that 'dtoverlay=vc4-kms-v3d' is"
  echo "enabled in /boot/firmware/config.txt (the default on Raspberry Pi OS) and reboot."
fi

# deploy.sh prints its own summary once everything is configured and verified.
if [ "${CEC_BRIDGE_QUIET:-0}" != "1" ]; then
  PORT="$(python3 - <<'EOF' 2>/dev/null || echo 8080
import json
try:
    print(json.load(open('/opt/cec-bridge/config.json')).get('web_port', 8080))
except Exception:
    print(8080)
EOF
)"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  echo "Done. Open the settings page:  http://${IP:-<pi-ip>}:$PORT/"
  echo "Logs:  journalctl -u cec-bridge -f"
fi
