#!/usr/bin/env bash
#
# One-command installer for the CEC bridge, run from your own Linux or macOS
# machine. It copies the bridge to a Raspberry Pi over SSH, installs it,
# optionally configures MQTT, and checks that it came up.
#
#   ./deploy.sh                          ask for everything
#   ./deploy.sh cec@192.168.1.11         install, then configure in the browser
#   ./deploy.sh cec@192.168.1.11 --mqtt-host 192.168.1.10 --mqtt-user ha
#   ./deploy.sh cec@192.168.1.11 --uninstall
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET=""; SSH_PORT=22; IDENTITY=""
MQTT_HOST=""; MQTT_PORT=""; MQTT_USER=""; MQTT_PASS=""; BASE_TOPIC=""; DEVICE_NAME=""
WEB_PORT=""; DETECT=0; UNINSTALL=0; ASSUME_YES=0; NO_KEY=0; UPDATE=0
# Where the last target is remembered, so `./deploy.sh --update` needs no address.
STATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/cec-bridge"
STATE_FILE="$STATE_DIR/last-target"

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; DIM=$'\e[2m'; BLD=$'\e[1m'; RST=$'\e[0m'
[ -t 1 ] || { RED=""; GRN=""; YLW=""; DIM=""; BLD=""; RST=""; }

say()  { printf '%s==>%s %s\n' "$BLD" "$RST" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '  %s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '%sError:%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Installs the CEC bridge on a Raspberry Pi over SSH.

Usage: ./deploy.sh [user@host] [options]

Options:
  --mqtt-host HOST     MQTT broker, i.e. your Home Assistant machine
  --mqtt-port PORT     MQTT port (default 1883)
  --mqtt-user USER     MQTT username
  --mqtt-pass PASS     MQTT password (omit to be prompted without echo)
  --base-topic TOPIC   MQTT base topic (default on the Pi: cec_bridge)
  --name NAME          Device name shown in Home Assistant
  --web-port PORT      Port for the settings page (default 8080)
  --detect-inputs      After installing, scan the HDMI bus and save what it finds
  --update             Update the software only. Asks nothing, changes no
                       settings, and reuses the last address if you omit it.
  --port PORT          SSH port (default 22)
  -i, --identity FILE  SSH private key
  --no-key-setup       Do not offer to install an SSH key
  -y, --yes            Do not ask for confirmation
  --uninstall          Remove the bridge from the Pi
  -h, --help           This text

Anything not given here is left to the settings page at http://<pi>:8080/
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mqtt-host) MQTT_HOST="${2:-}"; shift 2 ;;
    --mqtt-port) MQTT_PORT="${2:-}"; shift 2 ;;
    --mqtt-user) MQTT_USER="${2:-}"; shift 2 ;;
    --mqtt-pass) MQTT_PASS="${2:-}"; shift 2 ;;
    --base-topic) BASE_TOPIC="${2:-}"; shift 2 ;;
    --name) DEVICE_NAME="${2:-}"; shift 2 ;;
    --web-port) WEB_PORT="${2:-}"; shift 2 ;;
    --detect-inputs) DETECT=1; shift ;;
    --update|--upgrade) UPDATE=1; shift ;;
    --port) SSH_PORT="${2:-}"; shift 2 ;;
    -i|--identity) IDENTITY="${2:-}"; shift 2 ;;
    --no-key-setup) NO_KEY=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "Unknown option '$1'. Try --help." ;;
    *) [ -z "$TARGET" ] || die "More than one target given."; TARGET="$1"; shift ;;
  esac
done

if [ "$UPDATE" -eq 1 ]; then
  # The whole point of --update is that it cannot touch your configuration,
  # so a flag that would change settings is a contradiction, not a preference.
  CONFLICT=""
  [ -n "$MQTT_HOST" ]   && CONFLICT="$CONFLICT --mqtt-host"
  [ -n "$MQTT_PORT" ]   && CONFLICT="$CONFLICT --mqtt-port"
  [ -n "$MQTT_USER" ]   && CONFLICT="$CONFLICT --mqtt-user"
  [ -n "$MQTT_PASS" ]   && CONFLICT="$CONFLICT --mqtt-pass"
  [ -n "$BASE_TOPIC" ]  && CONFLICT="$CONFLICT --base-topic"
  [ -n "$DEVICE_NAME" ] && CONFLICT="$CONFLICT --name"
  [ -n "$WEB_PORT" ]    && CONFLICT="$CONFLICT --web-port"
  [ -n "$CONFLICT" ] && die "--update never changes settings, so it cannot be combined with:$CONFLICT
       Drop --update to change them, or drop those flags to update the software only."
  [ "$UNINSTALL" -eq 1 ] && die "--update and --uninstall do opposite things."
  ASSUME_YES=1; NO_KEY=1          # an update asks nothing
fi

command -v ssh >/dev/null || die "ssh is not installed."
command -v scp >/dev/null || die "scp is not installed."
for f in cec_bridge.py install.sh cec-bridge.service; do
  [ -f "$SRC_DIR/$f" ] || die "$f is missing from $SRC_DIR."
done

# ---------------------------------------------------------------- the target
if [ -z "$TARGET" ] && [ -f "$STATE_FILE" ]; then
  # Reuse the Pi from last time: always for --update, and as the suggested
  # default otherwise.
  LAST_TARGET=""; LAST_PORT=""; LAST_IDENTITY=""
  # shellcheck disable=SC1090
  . "$STATE_FILE" 2>/dev/null || true
  if [ -n "$LAST_TARGET" ]; then
    if [ "$UPDATE" -eq 1 ]; then
      TARGET="$LAST_TARGET"
      [ "$SSH_PORT" = 22 ] && [ -n "$LAST_PORT" ] && SSH_PORT="$LAST_PORT"
      [ -z "$IDENTITY" ] && [ -n "$LAST_IDENTITY" ] && IDENTITY="$LAST_IDENTITY"
      say "Updating ${BLD}$TARGET${RST} (remembered from last time)"
    elif [ -t 0 ]; then
      printf 'Raspberry Pi to install on %s[Enter for %s]%s: ' "$DIM" "$LAST_TARGET" "$RST"
      read -r TARGET
      [ -n "$TARGET" ] || { TARGET="$LAST_TARGET"
        [ "$SSH_PORT" = 22 ] && [ -n "$LAST_PORT" ] && SSH_PORT="$LAST_PORT"
        [ -z "$IDENTITY" ] && [ -n "$LAST_IDENTITY" ] && IDENTITY="$LAST_IDENTITY"; }
    fi
  fi
fi
if [ -z "$TARGET" ]; then
  [ "$UPDATE" -eq 1 ] && die "No Pi remembered yet, so --update has nothing to update.
       Give the address once:  ./deploy.sh cec@192.168.1.11 --update"
  printf 'Raspberry Pi to install on, as user@address %s[e.g. pi@192.168.1.11]%s: ' "$DIM" "$RST"
  read -r TARGET
  [ -n "$TARGET" ] || die "No target given."
fi
case "$TARGET" in
  *@*) ;;
  *) TARGET="pi@$TARGET"; say "No username given, assuming ${BLD}$TARGET${RST}" ;;
esac
PI_USER="${TARGET%@*}"; PI_HOST="${TARGET#*@}"

# One SSH connection reused for every step, so a password is typed once.
CTRL="${TMPDIR:-/tmp}/cec-deploy-$$-%C"
SSH_BASE=(-p "$SSH_PORT" -o ControlMaster=auto -o "ControlPath=$CTRL" -o ControlPersist=300
          -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
[ -n "$IDENTITY" ] && SSH_BASE+=(-i "$IDENTITY")
cleanup() { ssh "${SSH_BASE[@]}" -O exit "$TARGET" 2>/dev/null || true; }
trap cleanup EXIT

# -n keeps ssh from swallowing this script's own stdin, which would eat the
# answers to the prompts below. shdata_ is for the two calls that pipe data in.
sh_()     { ssh -n "${SSH_BASE[@]}" "$TARGET" "$@"; }
shdata_() { ssh "${SSH_BASE[@]}" "$TARGET" "$@"; }
shi_() {                                               # may need to ask for sudo
  if [ -t 0 ]; then ssh "${SSH_BASE[@]}" -t "$TARGET" "$@"
  else ssh -n "${SSH_BASE[@]}" "$TARGET" "$@"; fi
}

say "Connecting to $BLD$TARGET$RST"
sh_ true 2>/dev/null || {
  # First connection may need a password; let SSH prompt on the terminal.
  shi_ true \
    || die "Cannot reach $TARGET over SSH on port $SSH_PORT.
       Check the address, that SSH is enabled on the Pi, and that both are on the same network."
}
ok "connected"

# Offer key-based login so later runs and reboots need no password.
if [ "$NO_KEY" -eq 0 ] && [ "$UNINSTALL" -eq 0 ] \
   && ! sh_ -o PasswordAuthentication=no -o BatchMode=yes true 2>/dev/null; then
  if command -v ssh-copy-id >/dev/null && [ -t 0 ]; then
    key=""
    for k in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_rsa.pub"; do
      [ -f "$k" ] && { key="$k"; break; }
    done
    if [ -z "$key" ] && [ "$ASSUME_YES" -eq 0 ]; then
      printf 'No SSH key found. Create one so you are not asked for a password again? [Y/n] '
      read -r a
      case "$a" in [Nn]*) ;; *) ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519" \
            && key="$HOME/.ssh/id_ed25519.pub" ;; esac
    fi
    if [ -n "$key" ]; then
      say "Installing your SSH key on the Pi (one last password)"
      ssh-copy-id -p "$SSH_PORT" -i "$key" "$TARGET" >/dev/null 2>&1 \
        && ok "key installed" || warn "could not install the key; carrying on with passwords"
    fi
  fi
fi

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  say "Removing the bridge from $PI_HOST"
  shi_ 'sudo systemctl disable --now cec-bridge 2>/dev/null; \
        sudo rm -f /etc/systemd/system/cec-bridge.service; \
        sudo systemctl daemon-reload; \
        sudo rm -rf /opt/cec-bridge; echo removed'
  ok "the bridge is gone (v4l-utils and python3-paho-mqtt were left installed)"
  exit 0
fi

# ------------------------------------------------------------------ check it
say "Checking the Pi"
INFO="$(sh_ 'set -e
  . /etc/os-release 2>/dev/null || true
  echo "os=${PRETTY_NAME:-unknown}"
  echo "arch=$(uname -m)"
  echo "model=$(cat /proc/device-tree/model 2>/dev/null | tr -d "\0" || true)"
  echo "python=$(command -v python3 >/dev/null && python3 -V 2>&1 || echo none)"
  echo "cec=$(ls /dev/cec* 2>/dev/null | tr "\n" " " || true)"
  echo "sudo=$(sudo -n true 2>/dev/null && echo nopasswd || echo password)"
  echo "existing=$([ -d /opt/cec-bridge ] && echo yes || echo no)"
  echo "ip=$(hostname -I 2>/dev/null | awk "{print \$1}")"
')"
get() { printf '%s\n' "$INFO" | sed -n "s/^$1=//p"; }
[ -n "$(get model)" ] && ok "$(get model)"
ok "$(get os) ($(get arch))"
[ "$(get python)" = "none" ] && die "python3 is not installed on the Pi." || ok "$(get python)"
if [ -n "$(get cec)" ]; then ok "CEC device: $(get cec)"
else warn "no /dev/cec* yet. If this is a Pi, check that vc4-kms-v3d is enabled in
    /boot/firmware/config.txt and reboot. Installing anyway."; fi
if [ "$(get existing)" = "yes" ]; then
  say "An existing install was found; it will be updated (settings kept)."
elif [ "$UPDATE" -eq 1 ]; then
  die "There is no CEC bridge on $PI_HOST to update.
       Install it first:  ./deploy.sh $TARGET"
fi

PI_IP="$(get ip)"; [ -n "$PI_IP" ] || PI_IP="$PI_HOST"

# Fingerprint the settings before touching anything, so the end of an update
# can prove they came through unchanged.
CFG_BEFORE=""
[ "$UPDATE" -eq 1 ] && CFG_BEFORE="$(sh_ 'sudo md5sum /opt/cec-bridge/config.json 2>/dev/null | cut -d" " -f1' || true)"

if [ "$ASSUME_YES" -eq 0 ]; then
  printf '\nInstall the CEC bridge on %s%s%s? [Y/n] ' "$BLD" "$PI_HOST" "$RST"
  read -r a; case "$a" in [Nn]*) echo "Cancelled."; exit 0 ;; esac
fi

# ---------------------------------------------------------------- MQTT config
if [ -z "$MQTT_HOST" ] && [ "$UPDATE" -eq 0 ] && [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
  printf '\nConfigure MQTT now? Leave blank to do it later in the browser.\n'
  printf '  Home Assistant address: '; read -r MQTT_HOST
  if [ -n "$MQTT_HOST" ]; then
    printf '  MQTT username: '; read -r MQTT_USER
    printf '  MQTT password: '; read -rs MQTT_PASS; printf '\n'
  fi
fi
if [ -n "$MQTT_USER" ] && [ -z "$MQTT_PASS" ] && [ -t 0 ]; then
  printf '  MQTT password for %s: ' "$MQTT_USER"; read -rs MQTT_PASS; printf '\n'
fi

# ------------------------------------------------------------------- copy it
say "Copying files"
STAGE="$(sh_ 'mktemp -d /tmp/cec-bridge.XXXXXX')"
scp -p -P "$SSH_PORT" -o "ControlPath=$CTRL" -q \
    "$SRC_DIR/cec_bridge.py" "$SRC_DIR/install.sh" "$SRC_DIR/cec-bridge.service" \
    "$TARGET:$STAGE/" || die "Could not copy the files to $STAGE on the Pi."
ok "sent to $STAGE"

say "Installing (this needs sudo on the Pi)"
shi_ "cd '$STAGE' && chmod +x install.sh && sudo CEC_BRIDGE_QUIET=1 ./install.sh" \
  || die "The installer failed on the Pi. The output above says why."

# ------------------------------------------------------------- configure it
if [ -n "$MQTT_HOST$MQTT_PORT$MQTT_USER$MQTT_PASS$BASE_TOPIC$DEVICE_NAME$WEB_PORT" ]; then
  say "Writing settings"
  # The values travel as tab-separated lines on stdin, so no quoting or JSON
  # escaping happens in the shell — a password with quotes or spaces is safe.
  shdata_ "cat > '$STAGE/configure.py'" <<'PYEOF'
import json, os, sys
path = '/opt/cec-bridge/config.json'
cfg = {}
if os.path.exists(path):          # merge, so an update keeps inputs and keys
    try:
        with open(path) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
for line in sys.stdin.read().splitlines():
    if not line:
        continue
    key, _, value = line.partition('\t')
    cfg[key] = int(value) if key.endswith('_port') else value
tmp = path + '.tmp'
with open(tmp, 'w') as f:
    json.dump(cfg, f, indent=2)
os.replace(tmp, path)
os.chmod(path, 0o600)             # it holds the MQTT password
print('ok')
PYEOF
  {
    [ -n "$MQTT_HOST" ]   && printf 'mqtt_host\t%s\n'   "$MQTT_HOST"
    [ -n "$MQTT_PORT" ]   && printf 'mqtt_port\t%s\n'   "$MQTT_PORT"
    [ -n "$MQTT_USER" ]   && printf 'mqtt_user\t%s\n'   "$MQTT_USER"
    [ -n "$MQTT_PASS" ]   && printf 'mqtt_pass\t%s\n'   "$MQTT_PASS"
    [ -n "$BASE_TOPIC" ]  && printf 'base_topic\t%s\n'  "$BASE_TOPIC"
    [ -n "$DEVICE_NAME" ] && printf 'device_name\t%s\n' "$DEVICE_NAME"
    [ -n "$WEB_PORT" ]    && printf 'web_port\t%s\n'    "$WEB_PORT"
    true
  } | shdata_ "sudo python3 '$STAGE/configure.py'" >/dev/null \
    || die "Could not write the settings to /opt/cec-bridge/config.json."
  sh_ "sudo systemctl restart cec-bridge" || die "Could not restart the bridge."
  ok "settings saved"
fi

# --------------------------------------------------------------- verify it
PORT="${WEB_PORT:-8080}"
URL="http://$PI_IP:$PORT/"
say "Waiting for the bridge to come up"
STATUS=""
for _ in $(seq 1 20); do
  STATUS="$(sh_ "curl -fsS --max-time 3 http://127.0.0.1:$PORT/api/status 2>/dev/null" || true)"
  [ -n "$STATUS" ] && break
  sleep 1
done
[ -n "$STATUS" ] || die "The bridge did not answer on port $PORT.
       Look at the log with:  ssh $TARGET 'journalctl -u cec-bridge -n 40'"

field() { printf '%s' "$STATUS" | sed -n "s/.*\"$1\": *\"\{0,1\}\([^,\"}]*\).*/\1/p"; }
ok "the bridge is running"

# The broker handshake takes a moment after a restart, so give it a few tries
# before calling it a problem.
for _ in $(seq 1 8); do
  [ "$(field mqtt_connected)" = "true" ] && break
  sleep 1
  STATUS="$(sh_ "curl -fsS --max-time 3 http://127.0.0.1:$PORT/api/status 2>/dev/null" || printf '%s' "$STATUS")"
done
if [ "$(field mqtt_connected)" = "true" ]; then
  ok "MQTT connected"
elif [ -z "$MQTT_HOST" ]; then
  warn "MQTT not configured yet — set it on the settings page."
else
  warn "MQTT not connected: $(field mqtt_error)
    Check the address, username and password, and that the broker is running."
fi
case "$(field phys_addr)" in
  unknown) warn "the CEC adapter could not be read (is /dev/cec0 there?)" ;;
  f.f.f.f) warn "HDMI address is f.f.f.f — the Pi cannot see the TV. Turn the TV on
    and re-run, or force the HDMI output on at boot (see the guide)." ;;
  *) ok "HDMI address $(field phys_addr), the TV can be seen" ;;
esac

if [ "$DETECT" -eq 1 ]; then
  say "Scanning the HDMI bus for devices"
  shdata_ "cat > '$STAGE/detect.py'" <<'PYEOF'
"""Ask the running bridge what is on the bus, then add anything new as inputs."""
import json, sys, urllib.request

port = sys.argv[1]
base = f"http://127.0.0.1:{port}"


def call(path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


with urllib.request.urlopen(base + "/api/config", timeout=15) as r:
    inputs = json.load(r)["inputs"]

found = call("/api/detect", {"existing_ids": [i["id"] for i in inputs]})["found"]
known = {i["address"] for i in inputs}
fresh = [{k: d[k] for k in ("id", "name", "address")}
         for d in found if d["address"] not in known]

if fresh:                       # added to what is there, never replacing it
    call("/api/config", {"inputs": inputs + fresh})
    print(f"ADDED {len(fresh)}")
    for d in fresh:
        print(f"  {d['name']}  ({d['address']}, id: {d['id']})")
elif found:
    print("KNOWN")
else:
    print("NONE")
PYEOF
  OUT="$(sh_ "python3 '$STAGE/detect.py' $PORT" 2>/dev/null || true)"
  case "$OUT" in
    ADDED*) ok "$(printf '%s' "$OUT" | head -1 | sed 's/ADDED /added /') device(s) as inputs"
            printf '%s\n' "$OUT" | tail -n +2 | sed 's/^/    /' ;;
    KNOWN)  ok "everything on the bus is already configured" ;;
    NONE)   warn "nothing answered on the bus. Switch the devices on and press
    Detect inputs on the settings page." ;;
    *)      warn "the scan did not complete; use Detect inputs on the settings page." ;;
  esac
fi

if [ "$UPDATE" -eq 1 ]; then
  CFG_AFTER="$(sh_ 'sudo md5sum /opt/cec-bridge/config.json 2>/dev/null | cut -d" " -f1' || true)"
  if [ -n "$CFG_BEFORE" ] && [ "$CFG_BEFORE" = "$CFG_AFTER" ]; then
    shdata_ "cat > '$STAGE/summary.py'" <<'PYEOF'
import json
with open('/opt/cec-bridge/config.json') as f:
    c = json.load(f)
bits = [f"{len(c.get('inputs', []))} input(s)"]
if c.get('key_buttons'):
    bits.append(f"{len(c['key_buttons'])} key button(s)")
bits.append(f"MQTT {c.get('mqtt_host', '?')}:{c.get('mqtt_port', '?')}")
print(", ".join(bits))
PYEOF
    SUMMARY="$(sh_ "sudo python3 '$STAGE/summary.py'" 2>/dev/null || true)"
    ok "settings untouched${SUMMARY:+ — $SUMMARY}"
  elif [ -n "$CFG_BEFORE" ]; then
    warn "the settings file changed during the update, which it should not have."
  fi
fi

# Remember this Pi so a later `./deploy.sh --update` needs no address.
mkdir -p "$STATE_DIR" 2>/dev/null && {
  printf 'LAST_TARGET=%s\nLAST_PORT=%s\nLAST_IDENTITY=%s\n' \
    "$TARGET" "$SSH_PORT" "$IDENTITY" > "$STATE_FILE"
} 2>/dev/null || true

sh_ 'rm -rf /tmp/cec-bridge.*' 2>/dev/null || true

if [ "$UPDATE" -eq 1 ]; then
  cat <<EOF

${GRN}${BLD}Updated.${RST}  Your configuration was left alone.

  Settings page   ${BLD}$URL${RST}
  Logs            ssh $TARGET 'journalctl -u cec-bridge -f'
EOF
else
  cat <<EOF

${GRN}${BLD}Done.${RST}

  Settings page   ${BLD}$URL${RST}
  Logs            ssh $TARGET 'journalctl -u cec-bridge -f'
  Restart         ssh $TARGET 'sudo systemctl restart cec-bridge'
  Update later    ./deploy.sh --update
  Remove          ./deploy.sh $TARGET --uninstall

Open the settings page to add your inputs and pick the remote keys you want
in Home Assistant.
EOF
fi

if [ "$UPDATE" -eq 0 ] && [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ] && command -v xdg-open >/dev/null 2>&1; then
  printf '\nOpen the settings page now? [Y/n] '
  read -r a; case "$a" in [Nn]*) ;; *) xdg-open "$URL" >/dev/null 2>&1 & ;; esac
fi
