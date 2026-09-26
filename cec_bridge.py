#!/usr/bin/env python3
"""
CEC <-> MQTT bridge for Home Assistant, with a built-in settings web page.

Runs on a Raspberry Pi plugged into a spare HDMI port on the TV. Switches the
TV's input by broadcasting CEC "Active Source" messages with the physical
address of the wanted HDMI port, and exposes everything to Home Assistant via
MQTT discovery.

Dependencies: v4l-utils (cec-ctl) and python3-paho-mqtt. Everything else is
Python standard library.
"""
import base64
import collections
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

VERSION = "1.1"
CONFIG_PATH = os.environ.get(
    "CEC_BRIDGE_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
)

DEFAULT_CONFIG = {
    "mqtt_host": "homeassistant.local",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "discovery_prefix": "homeassistant",
    "base_topic": "cec_bridge",
    "device_name": "TV (CEC)",
    "cec_device": "/dev/cec0",
    "osd_name": "CEC Bridge",
    "power_poll_seconds": 30,
    # How long a remote key is held down before release. Some TVs ignore a key
    # that is released too quickly.
    "key_hold_ms": 120,
    # How many times to resend a frame the other end did not acknowledge.
    "retries": 2,
    "web_port": 8080,
    "web_password": "",
    "allow_raw": True,
    # Announce entities via MQTT discovery. Off = plain MQTT only.
    "discovery_enabled": True,
    # Empty on purpose: use "Detect inputs" on the settings page, or add rows
    # by hand. Nothing about this bridge is tied to particular devices.
    "inputs": [],
    # Remote keys to expose as buttons in Home Assistant. Every key still works
    # over MQTT whether or not it is listed here.
    "key_buttons": [],
    # The fixed entities. Turning one off removes it from Home Assistant; the
    # MQTT topic behind it keeps working either way.
    "entities": {
        "tv_on": True, "tv_standby": True, "standby_all": True,
        "input_select": True, "tv_power": True,
    },
    # Results of key tests, so the page can mark what is known to work on
    # your TV instead of guessing from the spec. {"volume-up@5": [acked, sent]}
    "key_status": {},
}

# Fixed entities, in the order the settings page lists them.
FIXED_ENTITIES = [
    ("tv_on", "TV on", "button",
     "Wakes the TV with the dedicated Image View On message."),
    ("tv_standby", "TV standby", "button",
     "Sends the dedicated Standby message to the TV."),
    ("standby_all", "All devices standby", "button",
     "Broadcasts Standby to every device on the bus at once."),
    ("input_select", "Input", "select",
     "Dropdown of your inputs, for dashboards and voice."),
    ("tv_power", "TV power", "sensor",
     "Reports on / standby, polled at the interval in Settings."),
]

PHYS_RE = re.compile(r"^[0-9a-fA-F]\.[0-9a-fA-F]\.[0-9a-fA-F]\.[0-9a-fA-F]$")
ID_RE = re.compile(r"^[a-z0-9_]+$")
KEY_RE = re.compile(r"^[a-z0-9-]+$")

# Every remote key CEC defines, exactly as cec-ctl names them (its ui-cmd
# enum), grouped for the settings page. Taken from `cec-ctl --help-all`, so
# these names are the ones the tool actually accepts -- several differ from
# the labels in the CEC spec (Exit is "back", Root Menu is "device-root-menu").
KEY_GROUPS = [
    ("Volume and sound", [
        "volume-up", "volume-down", "mute", "mute-function",
        "restore-volume-function", "sound-select", "select-sound-presentation",
        "audio-description", "select-audio-input-function"]),
    ("Navigation", [
        "up", "down", "left", "right", "select", "back", "enter", "clear",
        "page-up", "page-down", "right-up", "right-down", "left-up",
        "left-down"]),
    ("Menus and info", [
        "device-root-menu", "device-setup-menu", "contents-menu",
        "favorite-menu", "media-top-menu", "media-context-sensitive-menu",
        "display-information", "help", "electronic-program-guide",
        "timer-programming", "initial-configuration", "internet", "3d-mode"]),
    ("Playback", [
        "play", "pause", "stop", "record", "rewind", "fast-forward", "eject",
        "skip-forward", "skip-backward", "stop-record", "pause-record",
        "play-function", "pause-play-function", "record-function",
        "pause-record-function", "stop-function", "video-on-demand", "angle",
        "sub-picture"]),
    ("Power", [
        "power", "power-toggle-function", "power-on-function",
        "power-off-function"]),
    ("Channels and tuning", [
        "channel-up", "channel-down", "previous-channel", "next-favorite",
        "tune-function", "select-broadcast-type"]),
    ("Number keys", [
        "number-0-or-number-10", "number-1", "number-2", "number-3",
        "number-4", "number-5", "number-6", "number-7", "number-8",
        "number-9", "number-11", "number-12", "dot", "number-entry-mode"]),
    ("Input and source", [
        "input-select", "select-av-input-function", "select-media-function"]),
    ("Coloured keys", [
        "f1-blue", "f2-red", "f3-green", "f4-yellow", "f5", "data"]),
]

KEY_LABELS = {
    "select": "Select (OK)", "back": "Back / Exit",
    "device-root-menu": "Root menu", "device-setup-menu": "Setup menu",
    "media-context-sensitive-menu": "Context menu",
    "electronic-program-guide": "Guide (EPG)",
    "number-0-or-number-10": "Number 0 / 10",
    "f1-blue": "Blue (F1)", "f2-red": "Red (F2)",
    "f3-green": "Green (F3)", "f4-yellow": "Yellow (F4)", "f5": "F5",
    "3d-mode": "3D mode", "video-on-demand": "Video on demand",
}
ALL_KEYS = [k for _, keys in KEY_GROUPS for k in keys]

KEY_ICONS = {
    "volume-up": "mdi:volume-plus", "volume-down": "mdi:volume-minus",
    "mute": "mdi:volume-off", "play": "mdi:play", "pause": "mdi:pause",
    "stop": "mdi:stop", "channel-up": "mdi:chevron-up",
    "channel-down": "mdi:chevron-down", "power": "mdi:power",
    "up": "mdi:arrow-up", "down": "mdi:arrow-down",
    "left": "mdi:arrow-left", "right": "mdi:arrow-right",
    "select": "mdi:checkbox-marked-circle-outline", "back": "mdi:arrow-left-circle",
    "device-root-menu": "mdi:menu", "display-information": "mdi:information-outline",
}


def key_label(key):
    return KEY_LABELS.get(key, key.replace("-", " ").capitalize())

LOG = collections.deque(maxlen=300)


def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    LOG.append(line)
    print(line, flush=True)


# CEC is a shared bus: every device has a fixed logical address and sees every
# message. Nothing is "routed through" an AVR — you address a device directly.
LOGICAL_NAMES = {
    0: "TV", 1: "Recording 1", 2: "Recording 2", 3: "Tuner 1",
    4: "Playback 1", 5: "Audio system", 6: "Tuner 2", 7: "Tuner 3",
    8: "Playback 2", 9: "Recording 3", 10: "Tuner 4", 11: "Playback 3",
    12: "Reserved", 13: "Reserved", 14: "Specific use", 15: "Broadcast",
}


def dest_label(dest):
    try:
        return LOGICAL_NAMES.get(int(dest), f"device {dest}")
    except (TypeError, ValueError):
        return f"device {dest}"


def parse_target(payload, default_dest="0"):
    """Split 'volume-up@5' into ('volume-up', '5'). The @suffix is how every
    command in this bridge picks which device on the bus it talks to."""
    name, _, dest = str(payload).partition("@")
    dest = dest.strip() or default_dest
    if not (dest.isdigit() and 0 <= int(dest) <= 15):
        return name.strip(), None
    return name.strip(), dest


# When a device does not answer a query during --show-topology, cec-ctl puts
# the transaction status where the value would be: "Tx, OK, Rx, Timeout",
# "Tx, Not Acknowledged", "Tx, OK, Rx, Feature Abort" and so on. That is "no
# answer", never a name — a TV once showed up as "Tx, OK, Rx, Timeout".
STATUS_RE = re.compile(r"^\s*(Tx|Rx)\s*,", re.I)


def answered(match):
    """The captured value, or '' when it is missing or a cec-ctl status line."""
    if not match:
        return ""
    value = match.group(1).strip()
    return "" if STATUS_RE.match(value) else value


def input_name_applies(dev, all_devices):
    """Should your name for an input label this bus device?

    An input is "what the TV shows at this HDMI address". That describes the
    device sitting there only when it is the one source at that address. It
    does not for the TV, and it does not for an AVR: the input pointing at an
    AVR's port shows whatever the AVR is passing through, and an AVR usually
    holds two logical addresses at once (5 audio system, 3 tuner for its
    radio), both at the same physical address."""
    addr = dev.get("address")
    if not addr or dev.get("logical") in (0, 5):
        return False
    return sum(1 for d in all_devices if d.get("address") == addr) == 1


def nacked(out):
    """True when cec-ctl says the frame was not acknowledged by the other end."""
    low = out.lower()
    return ("nack" in low or "transmit failed" in low
            or "tx error" in low or "timed out" in low)


def slug(text):
    """'PlayStation 5' -> 'playstation_5', for use as an MQTT payload id."""
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "input"


# ------------------------------------------------------------------ config
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        save_config(cfg)
    except Exception as exc:
        log(f"Could not read config, using defaults: {exc}")
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)  # contains the MQTT password
    except OSError:
        pass


def validate_config(new):
    errors = []
    try:
        new["mqtt_port"] = int(new["mqtt_port"])
        new["web_port"] = int(new["web_port"])
        new["power_poll_seconds"] = max(0, int(new["power_poll_seconds"]))
        new["key_hold_ms"] = max(0, min(int(new.get("key_hold_ms", 120)), 2000))
        new["retries"] = max(0, min(int(new.get("retries", 2)), 10))
    except (ValueError, KeyError, TypeError):
        errors.append("Ports, poll interval, key hold and retries must be numbers.")
    for field in ("mqtt_host", "base_topic", "discovery_prefix", "cec_device"):
        if not str(new.get(field, "")).strip():
            errors.append(f"'{field}' cannot be empty.")
    osd = str(new.get("osd_name", ""))
    if not 1 <= len(osd) <= 14:
        errors.append("OSD name must be 1-14 characters (CEC limit).")
    seen = set()
    for inp in new.get("inputs", []):
        if not ID_RE.match(inp.get("id", "")):
            errors.append(f"Input id '{inp.get('id')}' must be lowercase letters, numbers or _.")
        if inp.get("id") in seen:
            errors.append(f"Input id '{inp.get('id')}' is used twice.")
        seen.add(inp.get("id"))
        if not PHYS_RE.match(inp.get("address", "")):
            errors.append(f"Address '{inp.get('address')}' must look like 3.0.0.0.")
        if not str(inp.get("name", "")).strip():
            errors.append("Every input needs a name.")
    ents = new.get("entities")
    if not isinstance(ents, dict):
        new["entities"] = dict(DEFAULT_CONFIG["entities"])
    else:
        new["entities"] = {k: bool(ents.get(k, True))
                           for k, _, _, _ in FIXED_ENTITIES}
    unknown = []
    for entry in new.get("key_buttons", []):
        name, dest = parse_target(entry, "0")
        if name not in ALL_KEYS or dest is None:
            unknown.append(entry)
    if unknown:
        errors.append("Not CEC key names: " + ", ".join(unknown[:5]) + ".")
    return errors


# --------------------------------------------------------------------- CEC
class Cec:
    def __init__(self, bridge):
        self.bridge = bridge
        self.lock = threading.Lock()
        self.phys_addr = "unknown"
        self.logical_addr = "unknown"
        self.last_error = ""

    @property
    def cfg(self):
        return self.bridge.cfg

    def run(self, *args, timeout=15, quiet=False, retry=None):
        """Run cec-ctl. A frame the TV did not acknowledge is retried, because
        a busy CEC bus drops messages and one lost frame is a missed button."""
        tries = (self.cfg.get("retries", 2) if retry is None else retry) + 1
        ok, out = False, ""
        for attempt in range(tries):
            ok, out = self._run_once(*args, timeout=timeout,
                                     quiet=quiet or attempt < tries - 1)
            if ok and not nacked(out):
                if attempt:
                    log(f"cec-ctl: {' '.join(args)} went through on try {attempt + 1}")
                return True, out
            if attempt < tries - 1:
                time.sleep(0.15 * (attempt + 1))
        if ok and nacked(out):
            self.note_error(f"cec-ctl: the TV did not acknowledge "
                            f"{' '.join(args)} after {tries} tries", quiet)
            return False, out
        return ok, out

    def _run_once(self, *args, timeout=15, quiet=False):
        cmd = ["cec-ctl", "-d", self.cfg["cec_device"], *args]
        with self.lock:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except FileNotFoundError:
                self.note_error("cec-ctl not found. Install it with: "
                                "sudo apt install v4l-utils", quiet)
                return False, "cec-ctl not found (install v4l-utils)"
            except subprocess.TimeoutExpired:
                self.note_error("cec-ctl timed out: " + " ".join(args), quiet)
                return False, "timed out"
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            last = out.splitlines()[-1] if out else f"exit code {r.returncode}"
            self.note_error(f"cec-ctl failed ({' '.join(args)}): {last}", quiet)
        else:
            self.last_error = ""
        return r.returncode == 0, out

    def note_error(self, msg, quiet=False):
        """Log an error, but do not repeat the same one over and over."""
        if quiet and msg == self.last_error:
            return
        self.last_error = msg
        log(msg)

    def setup(self):
        ok, out = self.run("--playback", "--osd-name", self.cfg["osd_name"])
        self.refresh_info()
        log(f"CEC registered: physical {self.phys_addr}, logical {self.logical_addr}")
        if self.phys_addr == "unknown":
            log("WARNING: could not read the CEC adapter. Check that /dev/cec0 exists "
                "and that cec-ctl is installed (sudo apt install v4l-utils).")
        elif self.phys_addr == "f.f.f.f":
            log("WARNING: physical address is f.f.f.f - the Pi is not seeing the TV over "
                "HDMI. See 'Troubleshooting' in the guide (force HDMI output at boot).")
        return ok, out

    def refresh_info(self):
        ok, out = self.run()
        m = re.search(r"^[ \t]*Physical Address\s*:\s*([0-9a-fA-F.]+)", out, re.M)
        self.phys_addr = m.group(1) if m else "unknown"
        # The real logical address is the indented line "  Logical Address : 4 (...)".
        # The unindented "Logical Addresses : 1" is only a count, so anchor on indentation.
        m = re.search(r"^[ \t]+Logical Address\s*:\s*(\d+)", out, re.M)
        self.logical_addr = m.group(1) if m else "unknown"

    def active_source(self, addr):
        return self.run("--to", "15", "--active-source", f"phys-addr={addr}")

    def tv_on(self, dest="0"):
        return self.run("--to", dest, "--image-view-on")

    def tv_standby(self, dest="0"):
        return self.run("--to", dest, "--standby")

    def standby_all(self):
        return self.run("--to", "15", "--standby")

    def key(self, name, dest="0", hold_ms=None):
        """Press and release a remote key. A real remote holds a key down for
        a moment; some TVs ignore a press released too quickly, so the gap is
        configurable and defaults to roughly one remote-control tap."""
        if hold_ms is None:
            hold_ms = self.cfg.get("key_hold_ms", 120)
        ok, out = self.run("--to", dest, "--user-control-pressed", f"ui-cmd={name}")
        time.sleep(max(0, int(hold_ms)) / 1000.0)
        ok2, out2 = self.run("--to", dest, "--user-control-released")
        return ok and ok2, out + "\n" + out2

    def features(self, dest="0"):
        """Ask a device what it declares it supports (CEC 2.0 Give Features).
        This is the only thing the standard lets a device advertise about its
        remote-key handling, so it beats guessing from the spec."""
        ok, out = self.run("--to", dest, "--give-features", timeout=10)
        got = {}
        for field, pattern in (
                ("cec_version", r"cec-version\s*:\s*(\S+)"),
                ("device_types", r"all-device-types\s*:\s*(.+)"),
                ("rc_profile", r"rc-profile\s*:\s*(.+)"),
                ("features", r"dev-features\s*:\s*(.+)")):
            m = re.search(pattern, out, re.I)
            if m:
                got[field] = m.group(1).strip()
        return ok and bool(got), got, out

    def key_test(self, name, dest="0", count=10, hold_ms=None):
        """Send one key repeatedly and report how many frames the TV accepted.
        This separates 'the Pi never sent it' from 'the TV ignored it'."""
        sent = acked = 0
        first_failure = ""
        for _ in range(max(1, min(int(count), 50))):
            # retry=0: each attempt is counted honestly, no hidden retries.
            ok, out = self.run("--to", dest, "--user-control-pressed",
                               f"ui-cmd={name}", quiet=True, retry=0)
            time.sleep(max(0, int(hold_ms if hold_ms is not None
                                  else self.cfg.get("key_hold_ms", 120))) / 1000.0)
            self.run("--to", dest, "--user-control-released", quiet=True, retry=0)
            sent += 1
            if ok and not nacked(out):
                acked += 1
            elif not first_failure:
                first_failure = out.strip().splitlines()[-1] if out.strip() else "no output"
            time.sleep(0.25)
        return sent, acked, first_failure

    def power_status(self, dest="0", quiet=False):
        ok, out = self.run("--to", dest, "--give-device-power-status",
                           timeout=10, quiet=quiet)
        m = re.search(r"pwr-state:\s*([a-z-]+)", out)
        return (m.group(1) if (ok and m) else "unknown"), out

    def topology(self):
        return self.run("--show-topology", timeout=30)

    # cec-ctl prints each topology block tab-indented, so anchor on the header
    # text wherever it appears rather than at the start of a line.
    TOPO_RE = re.compile(
        r"System Information for device (\d+)\s*\(([^)]*)\)(.*?)"
        r"(?=System Information for device|\Z)", re.S)

    def devices(self):
        """Every device on the CEC bus, with the logical address you address
        it by. The AVR, the TV and a Shield all sit on the same wire."""
        ok, out = self.topology()
        found = [self._device_from(int(m.group(1)), m.group(2).strip(), m.group(3))
                 for m in self.TOPO_RE.finditer(out)]
        if len(found) < 2:
            # Topology said little or nothing. Ask every logical address in
            # turn instead: slower, but it does not depend on the TV choosing
            # to report its neighbours.
            log("Topology returned little; polling each address instead")
            probed, probe_out = self.probe_bus()
            if len(probed) > len(found):
                found, out = probed, (out + "\n\n" + probe_out)
        found.sort(key=lambda d: d["logical"])
        return ok, found, out

    def _device_from(self, la, kind, block):
        name = answered(re.search(r"OSD Name\s*:\s*'?([^'\n]+?)'?\s*$", block, re.M))
        addr = re.search(r"Physical Address\s*:\s*([0-9a-fA-F]\.[0-9a-fA-F.]+)", block)
        power = answered(re.search(r"Power Status\s*:\s*([^\n]+?)\s*$", block, re.M))
        vendor = re.search(r"Vendor ID\s*:\s*\S+\s*\(([^)]*)\)", block)
        address = addr.group(1) if addr else ""
        return {
            "logical": la,
            "role": LOGICAL_NAMES.get(la, kind),
            "type": kind,
            "name": name or LOGICAL_NAMES.get(la, kind),
            "address": address,
            "power": (power.split()[0].lower() if power else ""),
            "vendor": vendor.group(1) if vendor else "",
            "is_self": bool(address and address == self.phys_addr),
        }

    def probe_bus(self):
        """Poll each logical address and ask whoever answers who they are."""
        found, log_out = [], []
        for la in range(15):
            ok, out = self.run("--to", str(la), "--poll", timeout=5,
                               quiet=True, retry=0)
            log_out.append(f"--- poll {la} ---\n{out}")
            if not ok or nacked(out):
                continue
            _, info = self.run("--to", str(la), "--give-osd-name",
                               "--give-physical-addr", "--give-device-power-status",
                               "--give-vendor-id", timeout=10, quiet=True, retry=0)
            log_out.append(info)
            name = re.search(r"(?:osd-)?name\s*:\s*'?([^'\n]+?)'?\s*$", info, re.M | re.I)
            addr = re.search(r"phys-addr\s*:\s*([0-9a-fA-F.]+)", info, re.I) or \
                re.search(r"Physical Address\s*:\s*([0-9a-fA-F.]+)", info)
            power = re.search(r"pwr-state\s*:\s*([a-z-]+)", info, re.I)
            vendor = re.search(r"vendor-id\s*:\s*\S+\s*\(([^)]*)\)", info, re.I)
            address = addr.group(1) if addr else ""
            found.append({
                "logical": la,
                "role": LOGICAL_NAMES.get(la, f"device {la}"),
                "type": LOGICAL_NAMES.get(la, f"device {la}"),
                "name": answered(name)
                        or LOGICAL_NAMES.get(la, f"device {la}"),
                "address": address,
                "power": (power.group(1).lower() if power else ""),
                "vendor": vendor.group(1) if vendor else "",
                "is_self": bool(address and address == self.phys_addr)
                           or str(la) == str(self.logical_addr),
            })
        return found, "\n".join(log_out)

    def discover(self):
        """Scan the bus and turn what is plugged in into ready-made input rows."""
        ok, devs, out = self.devices()
        found, seen = [], set()
        for dev in devs:
            addr = dev["address"]
            # 0.0.0.0 is the TV itself; f.f.f.f is unknown; skip our own port.
            if (not addr or addr in ("0.0.0.0", "f.f.f.f", self.phys_addr)
                    or dev["is_self"] or addr in seen):
                continue
            seen.add(addr)
            label = dev["name"] or f"HDMI {addr[0]}"
            found.append({"id": slug(label), "name": label, "address": addr,
                          "port": addr[0]})
        found.sort(key=lambda d: d["address"])
        return ok, found, out

    def raw(self, args):
        return self.run(*shlex.split(args), timeout=30)


# ------------------------------------------------------------------ bridge
class Bridge:
    def __init__(self):
        self.cfg = load_config()
        self.cec = Cec(self)
        self.client = None
        self.mqtt_connected = False
        self.mqtt_error = ""
        self.tv_power = "unknown"
        self.published = set()     # discovery topics we have published
        self.seen_configs = set()  # retained ones found on the broker at connect
        self.cfg_lock = threading.Lock()
        # Last bus scan, kept so the settings page can name devices straight
        # away instead of showing bare logical addresses until you press Scan.
        self.devices_cache = []
        self.devices_scanned = 0

    def device_label(self, dest):
        """What to call the device at a logical address. Kept in step with
        deviceName() in the page; see input_name_applies for the rule."""
        for dev in self.devices_cache:
            if str(dev.get("logical")) != str(dest):
                continue
            if input_name_applies(dev, self.devices_cache):
                for inp in self.cfg["inputs"]:
                    if inp.get("address") == dev.get("address") and inp.get("name"):
                        return inp["name"]
            name = dev.get("name", "")
            if name and name != dev.get("type") and not STATUS_RE.match(name):
                return name
            break
        return dest_label(dest)

    def scan_devices(self):
        ok, found, out = self.cec.devices()
        if found or ok:
            self.devices_cache = found
            self.devices_scanned = int(time.time())
        return ok, found, out

    # ---- commands (shared by MQTT and the web page)
    def find_input(self, value):
        value = value.strip()
        for inp in self.cfg["inputs"]:
            if value in (inp["id"], inp["name"], inp["address"]):
                return inp
        return None

    def command(self, cmd, payload=""):
        payload = (payload or "").strip()
        log(f"Command: {cmd} {payload}".rstrip())
        if cmd == "input":
            inp = self.find_input(payload)
            if not inp:
                return False, f"Unknown input '{payload}'"
            ok, out = self.cec.active_source(inp["address"])
            if ok:
                self.publish(f"{self.cfg['base_topic']}/state/input", inp["name"], retain=True)
            return ok, out
        if cmd == "active_source":
            if not PHYS_RE.match(payload):
                return False, "Payload must be a physical address like 3.0.0.0"
            return self.cec.active_source(payload)
        # Every device command takes an optional "@<logical address>" to aim it
        # at something other than the TV: "@5" is the audio system, "@8" a
        # second playback device such as a Shield.
        if cmd in ("tv_on", "tv_standby", "power_status"):
            _, dest = parse_target(payload, "0")
            if dest is None:
                return False, "Destination after @ must be a logical address, 0-15"
            if cmd == "tv_on":
                return self.cec.tv_on(dest)
            if cmd == "tv_standby":
                return self.cec.tv_standby(dest)
            state, out = self.cec.power_status(dest)
            if dest == "0":            # only the TV drives the power sensor
                self.set_power(state)
            return True, f"{dest_label(dest)} power: {state}\n\n{out}"
        if cmd == "standby_all":
            return self.cec.standby_all()
        if cmd == "key":
            name, dest = parse_target(payload, "0")
            if dest is None:
                return False, "Destination after @ must be a logical address, 0-15"
            if not KEY_RE.match(name):
                return False, "Payload must be a key name like volume-up"
            if name not in ALL_KEYS:
                return False, (f"Unknown key '{name}'. The settings page lists "
                               "every key CEC defines.")
            return self.cec.key(name, dest)
        if cmd == "topology":
            ok, out = self.cec.topology()
            self.publish(f"{self.cfg['base_topic']}/topology", out[-60000:])
            return ok, out
        if cmd == "reconfigure":
            return self.cec.setup()
        if cmd == "raw":
            if not self.cfg.get("allow_raw"):
                return False, "Raw commands are disabled in settings"
            if not payload:
                return False, "Payload must be cec-ctl arguments"
            return self.cec.raw(payload)
        return False, f"Unknown command '{cmd}'"

    def set_power(self, state):
        if state != self.tv_power:
            log(f"TV power: {state}")
        self.tv_power = state
        self.publish(f"{self.cfg['base_topic']}/state/tv_power", state, retain=True)

    # ---- MQTT
    def publish(self, topic, payload, retain=False):
        if self.client and self.mqtt_connected:
            self.client.publish(topic, payload, retain=retain)

    def start_mqtt(self):
        cfg = self.cfg
        cid = f"cec_bridge_{cfg['base_topic']}"
        try:  # paho-mqtt 2.x
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        except AttributeError:  # paho-mqtt 1.x
            client = mqtt.Client(client_id=cid)
        if cfg["mqtt_user"]:
            client.username_pw_set(cfg["mqtt_user"], cfg["mqtt_pass"])
        client.will_set(f"{cfg['base_topic']}/status", "offline", retain=True)
        client.on_connect = self.on_connect
        client.on_disconnect = self.on_disconnect
        client.on_message = self.on_message
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.client = client
        self.mqtt_error = "connecting..."
        try:
            client.connect_async(cfg["mqtt_host"], cfg["mqtt_port"], keepalive=60)
            client.loop_start()
            log(f"MQTT: connecting to {cfg['mqtt_host']}:{cfg['mqtt_port']}")
        except Exception as exc:
            self.mqtt_error = str(exc)
            log(f"MQTT error: {exc}")

    def stop_mqtt(self):
        if not self.client:
            return
        try:
            if self.mqtt_connected:
                self.client.publish(f"{self.cfg['base_topic']}/status", "offline", retain=True)
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:
            pass
        self.client = None
        self.mqtt_connected = False

    def on_connect(self, client, userdata, flags, rc, properties=None):
        code = getattr(rc, "value", rc)
        if code != 0:
            self.mqtt_connected = False
            self.mqtt_error = f"connection refused ({rc}) - check username/password"
            log(f"MQTT: {self.mqtt_error}")
            return
        self.mqtt_connected = True
        self.mqtt_error = ""
        log("MQTT: connected")
        base = self.cfg["base_topic"]
        client.subscribe(f"{base}/cmd/#")
        client.subscribe(f"{self.cfg['discovery_prefix']}/status")
        # Listen for our own retained discovery configs so entities left over
        # from an earlier configuration can be cleared instead of lingering in
        # Home Assistant as "unavailable". Scoped to this bridge's own device id.
        dev_id = f"cec_bridge_{base}"
        client.subscribe(f"{self.cfg['discovery_prefix']}/+/{dev_id}/+/config")
        client.publish(f"{base}/status", "online", retain=True)
        client.publish(f"{base}/state/tv_power", self.tv_power, retain=True)
        # Give the broker a moment to deliver those retained messages, then
        # publish, so the first publish can already remove the stale ones.
        threading.Timer(1.5, self.publish_discovery).start()

    def on_disconnect(self, client, userdata, *args):
        if self.mqtt_connected:
            log("MQTT: disconnected, will retry")
        self.mqtt_connected = False
        if not self.mqtt_error:
            self.mqtt_error = "disconnected, retrying"

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace")
        if msg.topic.endswith("/config") and \
                msg.topic.startswith(self.cfg["discovery_prefix"] + "/"):
            if payload:
                self.seen_configs.add(msg.topic)
            else:
                self.seen_configs.discard(msg.topic)
            return
        if msg.topic == f"{self.cfg['discovery_prefix']}/status":
            if payload == "online":  # Home Assistant restarted
                self.publish_discovery()
                self.publish(f"{self.cfg['base_topic']}/status", "online", retain=True)
            return
        cmd = msg.topic.rsplit("/", 1)[-1]
        threading.Thread(target=self._run_mqtt_cmd, args=(cmd, payload), daemon=True).start()

    def _run_mqtt_cmd(self, cmd, payload):
        ok, out = self.command(cmd, payload)
        if not ok:
            log(f"Command '{cmd}' failed: {out.splitlines()[-1] if out else ''}")

    def publish_discovery(self):
        cfg = self.cfg
        base, prefix = cfg["base_topic"], cfg["discovery_prefix"]
        dev_id = f"cec_bridge_{base}"
        device = {
            "identifiers": [dev_id],
            "name": cfg["device_name"],
            "manufacturer": "Raspberry Pi",
            "model": "CEC bridge",
            "sw_version": VERSION,
            "configuration_url": f"http://{host_ip()}:{cfg['web_port']}/",
        }
        common = {"availability_topic": f"{base}/status", "device": device}
        entities = {}

        for inp in cfg["inputs"]:
            entities[f"{prefix}/button/{dev_id}/input_{inp['id']}/config"] = {
                **common, "name": inp["name"], "unique_id": f"{dev_id}_input_{inp['id']}",
                "command_topic": f"{base}/cmd/input", "payload_press": inp["id"],
                "icon": "mdi:video-input-hdmi",
            }
        on = cfg.get("entities", {})
        # A select with no options is rejected by Home Assistant.
        if cfg["inputs"] and on.get("input_select", True):
            entities[f"{prefix}/select/{dev_id}/input/config"] = {
                **common, "name": "Input", "unique_id": f"{dev_id}_input_select",
                "command_topic": f"{base}/cmd/input",
                "state_topic": f"{base}/state/input",
                "options": [i["name"] for i in cfg["inputs"]],
                "icon": "mdi:video-input-hdmi",
            }
        for key, name, icon in (("tv_on", "TV on", "mdi:television"),
                                ("tv_standby", "TV standby", "mdi:television-off"),
                                ("standby_all", "All devices standby", "mdi:power-sleep")):
            if not on.get(key, True):
                continue
            entities[f"{prefix}/button/{dev_id}/{key}/config"] = {
                **common, "name": name, "unique_id": f"{dev_id}_{key}",
                "command_topic": f"{base}/cmd/{key}", "icon": icon,
            }
        if on.get("tv_power", True):
            entities[f"{prefix}/sensor/{dev_id}/tv_power/config"] = {
                **common, "name": "TV power", "unique_id": f"{dev_id}_tv_power",
                "state_topic": f"{base}/state/tv_power", "icon": "mdi:power",
            }
        for entry in cfg.get("key_buttons", []):
            name, dest = parse_target(entry, "0")
            if name not in ALL_KEYS or dest is None:
                continue
            # A key aimed at another device says so, otherwise two "Volume up"
            # buttons for the TV and the AVR would be indistinguishable. The
            # name comes from your inputs where they match, so it reads
            # "Volume up (Nvidia Shield TV)" rather than "(Playback 2)".
            label = key_label(name) if dest == "0" else \
                f"{key_label(name)} ({self.device_label(dest)})"
            entities[f"{prefix}/button/{dev_id}/key_{slug(entry)}/config"] = {
                **common, "name": label,
                "unique_id": f"{dev_id}_key_{slug(entry)}",
                "command_topic": f"{base}/cmd/key", "payload_press": entry,
                "icon": KEY_ICONS.get(name, "mdi:remote"),
            }

        # Clear anything this device published before and no longer wants: a
        # deleted input, an unticked key, an entity switched off. self.seen_configs
        # also holds what was retained from previous runs, so entities left over
        # from an earlier configuration are cleaned up after a restart too.
        if not cfg.get("discovery_enabled", True):
            entities = {}      # plain MQTT only: withdraw everything announced
        stale = (self.published | self.seen_configs) - set(entities)
        for topic in stale:
            self.client.publish(topic, "", retain=True)
        for topic, conf in entities.items():
            self.client.publish(topic, json.dumps(conf), retain=True)
        self.published = set(entities)
        self.seen_configs -= stale
        log(f"MQTT: published {len(entities)} Home Assistant entities"
            + (f", removed {len(stale)} stale" if stale else ""))

    def remove_discovery(self):
        for topic in self.published:
            self.client.publish(topic, "", retain=True)
        self.published = set()

    # ---- config changes from the web page
    def apply_config(self, new):
        with self.cfg_lock:
            old = self.cfg
            mqtt_changed = any(old.get(k) != new.get(k) for k in (
                "mqtt_host", "mqtt_port", "mqtt_user", "mqtt_pass",
                "base_topic", "discovery_prefix"))
            cec_changed = any(old[k] != new[k] for k in ("cec_device", "osd_name"))
            if mqtt_changed and self.client and self.mqtt_connected:
                self.remove_discovery()
            if mqtt_changed:
                self.stop_mqtt()
            self.cfg = new
            save_config(new)
            log("Settings saved")
            if cec_changed:
                threading.Thread(target=self.cec.setup, daemon=True).start()
            if mqtt_changed:
                self.start_mqtt()
            elif self.mqtt_connected:
                self.publish_discovery()
            return old["web_port"] != new["web_port"]

    # ---- background power polling
    def poll_loop(self):
        misses = 0
        while True:
            interval = self.cfg.get("power_poll_seconds", 0)
            if not interval:
                time.sleep(5)
                continue
            state, _ = self.cec.power_status(quiet=True)
            self.set_power(state)
            # When the TV is off or unplugged every poll fails and blocks for the
            # full timeout, so back off up to 8x instead of hammering the bus.
            misses = 0 if state != "unknown" else min(misses + 1, 3)
            time.sleep(interval * (2 ** misses))


def host_ip():
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
        return out[0] if out else "raspberrypi.local"
    except Exception:
        return "raspberrypi.local"


# --------------------------------------------------------------------- web
class Handler(BaseHTTPRequestHandler):
    bridge = None

    def log_message(self, *args):
        pass

    def authorised(self):
        pw = self.bridge.cfg.get("web_password")
        if not pw:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user_pw = base64.b64decode(header[6:]).decode()
                if user_pw.split(":", 1)[1] == pw:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="CEC bridge"')
        self.end_headers()
        return False

    def send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj))

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if not self.authorised():
            return
        b = self.bridge
        if self.path == "/":
            self.send(200, PAGE, "text/html")
        elif self.path == "/favicon.ico":
            self.send(200, FAVICON, "image/svg+xml")
        elif self.path == "/api/config":
            cfg = dict(b.cfg)
            cfg["has_mqtt_pass"] = bool(cfg.pop("mqtt_pass"))
            cfg["has_web_password"] = bool(cfg.pop("web_password"))
            cfg["fixed_entities"] = [
                {"key": k, "name": n, "kind": t, "what": w}
                for k, n, t, w in FIXED_ENTITIES]
            cfg["key_groups"] = [
                {"group": name,
                 "keys": [{"key": k, "label": key_label(k)} for k in keys]}
                for name, keys in KEY_GROUPS]
            self.json(cfg)
        elif self.path == "/api/devices":     # cached, no scan
            self.json({"ok": True, "devices": b.devices_cache,
                       "scanned": b.devices_scanned})
        elif self.path == "/api/status":
            self.json({
                "version": VERSION,
                "mqtt_connected": b.mqtt_connected,
                "mqtt_error": b.mqtt_error,
                "phys_addr": b.cec.phys_addr,
                "logical_addr": b.cec.logical_addr,
                "tv_power": b.tv_power,
                "log": list(LOG)[-120:],
            })
        else:
            self.send(404, "not found", "text/plain")

    def do_POST(self):
        if not self.authorised():
            return
        b = self.bridge
        try:
            data = self.read_json()
        except Exception:
            return self.json({"ok": False, "output": "Invalid JSON"}, 400)
        if self.path == "/api/config":
            new = json.loads(json.dumps(b.cfg))
            for key in DEFAULT_CONFIG:
                if key in ("mqtt_pass", "web_password"):
                    continue
                if key in data:
                    new[key] = data[key]
            if data.get("mqtt_pass"):
                new["mqtt_pass"] = data["mqtt_pass"]
            if data.get("clear_mqtt_pass"):
                new["mqtt_pass"] = ""
            if data.get("web_password"):
                new["web_password"] = data["web_password"]
            if data.get("clear_web_password"):
                new["web_password"] = ""
            for field in ("mqtt_host", "mqtt_user", "base_topic", "discovery_prefix",
                          "device_name", "cec_device", "osd_name"):
                new[field] = str(new[field]).strip()
            new["base_topic"] = new["base_topic"].strip("/")
            new["allow_raw"] = bool(new["allow_raw"])
            new["discovery_enabled"] = bool(new.get("discovery_enabled", True))
            errors = validate_config(new)
            if errors:
                return self.json({"ok": False, "errors": errors}, 400)
            port_changed = b.apply_config(new)
            msg = "Saved."
            if port_changed:
                msg += " The web port changed - restart the service (sudo systemctl restart cec-bridge)."
            self.json({"ok": True, "message": msg})
        elif self.path == "/api/command":
            ok, out = b.command(str(data.get("command", "")), str(data.get("payload", "")))
            self.json({"ok": ok, "output": out})
        elif self.path == "/api/features":
            # The value is the logical address itself, not a "key@dest" pair.
            dest = str(data.get("dest", "0")).strip().lstrip("@") or "0"
            if not (dest.isdigit() and 0 <= int(dest) <= 15):
                return self.json({"ok": False,
                                  "output": "Destination must be 0-15."}, 400)
            ok, got, out = b.cec.features(dest)
            if ok:
                summary = "\n".join(f"{k.replace('_', ' ').title():<14}{v}"
                                    for k, v in got.items())
                out = (f"{b.device_label(dest)} declares:\n\n{summary}\n\n"
                       "rc-profile is the only thing CEC lets a device advertise "
                       "about remote keys. Anything beyond it is up to the "
                       "manufacturer, so test rather than assume.\n\n" + out)
            else:
                out = ("No answer. Give Features is CEC 2.0 only, so older "
                       "devices simply will not reply.\n\n" + out)
            self.json({"ok": ok, "features": got, "output": out})
        elif self.path == "/api/devices":
            ok, found, out = b.scan_devices()
            log(f"Bus scan: {len(found)} device(s)")
            self.json({"ok": ok, "devices": found, "output": out})
        elif self.path == "/api/keytest":
            name, dest = parse_target(str(data.get("key", "")), "0")
            if dest is None:
                return self.json({"ok": False, "output": "Bad destination."}, 400)
            count = int(data.get("count", 10) or 10)
            if name not in ALL_KEYS:
                return self.json({"ok": False, "output": f"Unknown key '{name}'."}, 400)
            log(f"Key test: {name} x{count} to device {dest}")
            sent, acked, why = b.cec.key_test(name, dest, count)
            # Remember the verdict so the key list can mark it from now on.
            entry = name if dest == "0" else f"{name}@{dest}"
            with b.cfg_lock:
                b.cfg.setdefault("key_status", {})[entry] = [acked, sent]
                save_config(b.cfg)
            if acked == sent:
                verdict = (f"All {sent} presses were acknowledged by the TV.\n\n"
                           "The frames are reaching it, so if the TV still does not "
                           "react, it does not implement this key. Nothing on the Pi "
                           "will change that — try a different key for the same job.")
            elif acked == 0:
                verdict = (f"None of the {sent} presses were acknowledged.\n\n"
                           f"The TV is not accepting this frame at all. {why}\n"
                           "Check that CEC is on and the TV is awake.")
            else:
                verdict = (f"{acked} of {sent} presses were acknowledged.\n\n"
                           "Frames are being dropped on the bus. Raising 'retries' "
                           "in Settings will paper over this; a shorter HDMI cable "
                           "or fewer CEC devices fixes the cause.")
            self.json({"ok": True, "sent": sent, "acked": acked,
                       "output": verdict})
        elif self.path == "/api/detect":
            ok, found, out = b.cec.discover()
            # Keep ids unique against what the page already has on screen.
            taken = {str(i) for i in data.get("existing_ids", [])}
            for dev in found:
                base, n = dev["id"], 2
                while dev["id"] in taken:
                    dev["id"] = f"{base}_{n}"
                    n += 1
                taken.add(dev["id"])
            log(f"Detect: found {len(found)} device(s) on the bus")
            self.json({"ok": ok, "found": found, "output": out})
        else:
            self.send(404, "not found", "text/plain")


FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<rect x="1" y="3" width="14" height="9" rx="1.5" fill="#2563eb"/>'
    '<path d="M5 14h6" stroke="#2563eb" stroke-width="1.5" stroke-linecap="round"/>'
    '</svg>'
)

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CEC Bridge</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<style>
/* Colour scheme unchanged; the layout around it is what got reworked. */
:root{--bg:#f4f5f7;--card:#fff;--text:#1d2129;--muted:#667085;--line:#e3e6ea;--accent:#2563eb;--ok:#16a34a;--bad:#dc2626;--code:#f1f3f5}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1e24;--text:#e6e8eb;--muted:#9aa3ae;--line:#2b313a;--accent:#60a5fa;--ok:#4ade80;--bad:#f87171;--code:#0d1014}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1000px;margin:0 auto;padding:0 16px 40px}

/* ---- sticky header: identity, live status, and the tabs ---- */
header{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);
  padding-top:14px;margin-bottom:20px}
.hwrap{max-width:1000px;margin:0 auto;padding:0 16px}
.brand{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.brand h1{font-size:19px;margin:0;letter-spacing:-.01em}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto;align-items:center}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted);
  background:var(--card);border:1px solid var(--line);border-radius:999px;padding:4px 11px;white-space:nowrap}
.chip b{color:var(--text);font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
nav{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav button{border:none;background:none;color:var(--muted);font:inherit;font-size:14px;
  padding:9px 13px;border-bottom:2px solid transparent;cursor:pointer;white-space:nowrap;border-radius:6px 6px 0 0}
nav button:hover{color:var(--text);background:var(--card)}
nav button[aria-selected=true]{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
nav .badge{display:inline-block;min-width:18px;margin-left:6px;padding:0 5px;border-radius:999px;
  background:var(--line);color:var(--muted);font-size:11px;font-weight:600;line-height:17px;text-align:center}

/* ---- panels ---- */
.panel{display:none}
.panel.active{display:block;animation:fade .12s ease-out}
@keyframes fade{from{opacity:0;transform:translateY(2px)}to{opacity:1}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
h2{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:17px;margin:0 0 6px}
h2+.hint{margin-top:0}
h2 .save{margin-left:auto;display:flex;align-items:center;gap:8px;font-weight:400}
h2 button{padding:6px 14px;font-size:13.5px}
h3{font-size:14px;margin:18px 0 8px;color:var(--text)}
.lead{color:var(--muted);font-size:13.5px;margin:0 0 14px}

/* ---- forms and controls ---- */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:5px}
input,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);font:inherit}
input:focus,select:focus{outline:2px solid color-mix(in srgb,var(--accent) 45%,transparent);outline-offset:1px;border-color:var(--accent)}
input[type=checkbox]{width:auto}
button{padding:8px 14px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);font:inherit;cursor:pointer}
button:hover:not(:disabled){filter:brightness(1.08);border-color:var(--accent)}
button:disabled{opacity:.5;cursor:not-allowed}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.hint{color:var(--muted);font-size:13px;margin:8px 0 0}
.msg{margin-left:4px;font-size:13.5px}
.pill{font-size:12px;color:var(--muted);font-weight:400}
.pill.on{color:var(--accent)}
.dirty h2 button.primary{box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 25%,transparent)}

/* ---- output, tables, collapsibles ---- */
pre{background:var(--code);border:1px solid var(--line);border-radius:8px;padding:11px;overflow:auto;max-height:320px;
  font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;margin:12px 0 0}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12.5px;text-transform:uppercase;letter-spacing:.03em}
tbody tr:last-child td{border-bottom:none}
code{font:12.5px ui-monospace,Menlo,Consolas,monospace;background:var(--code);padding:1px 5px;border-radius:4px;word-break:break-all}
.inputs td input{min-width:90px}
.tablewrap{overflow-x:auto}
details{border:1px solid var(--line);border-radius:9px;margin-bottom:8px;background:var(--bg)}
details[open]{background:transparent}
summary{cursor:pointer;padding:10px 12px;font-weight:600;font-size:14px;list-style:none;display:flex;align-items:center;gap:8px}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸";color:var(--muted);transition:transform .15s}
details[open] summary::before{transform:rotate(90deg)}
summary:hover{color:var(--accent)}
summary .count{color:var(--muted);font-weight:400;font-size:13px}
td .sub{display:block;color:var(--muted);font-size:12px;margin-top:2px}
.warnish{color:#d97706 !important}
td.onbus{font-size:13px;min-width:150px}
details .body{padding:0 12px 12px}

/* ---- key grid, with per-key verdicts ---- */
.keygrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:4px 12px}
.keygrid label{display:flex;align-items:baseline;gap:7px;margin:0;padding:4px 0;color:var(--text);font-size:13.5px;cursor:pointer}
.keygrid label:hover{color:var(--accent)}
.keygrid code{font-size:11.5px;color:var(--muted);background:none;padding:0}
.keygrid input{margin:0}
.mark{font-size:11px;font-weight:700;margin-left:2px}
.mark.ok{color:var(--ok)}
.mark.bad{color:var(--bad)}
.mark.part{color:#d97706}
.tiny{font-size:11.5px;padding:2px 7px;border-radius:5px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin:10px 0 0}
/* links keep the accent colour; the browser default is unreadable on dark */
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* ---- remote keys: toolbar ---- */
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;
  padding:12px;background:var(--bg);border:1px solid var(--line);border-radius:10px}
.toolbar .target{display:flex;flex-direction:column;gap:4px;flex:1 1 240px}
.toolbar .target label{margin:0;font-weight:600;color:var(--text);font-size:12.5px}
.toolbar input[type=search]{flex:1 1 180px;width:auto}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;flex:none}
.seg button{border:none;border-radius:0;padding:8px 12px;font-size:13.5px;background:var(--card)}
.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed=true]{background:var(--accent);color:#fff}
.toolbar-foot{display:flex;gap:6px 16px;flex-wrap:wrap;align-items:center;margin:10px 2px 12px;font-size:12.5px}
.toolbar-foot .legend{margin:0}
.toolbar-foot .expand{margin-left:auto;color:var(--muted)}

/* ---- remote keys: one tidy row per key ---- */
.keylist{display:flex;flex-direction:column}
.krow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;
  padding:7px 8px;border-radius:7px}
.krow+.krow{border-top:1px solid var(--line)}
.krow:hover{background:var(--bg)}
.kname{min-width:0;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.kname .label{font-size:14px}
.kname code{font-size:11.5px;color:var(--muted);background:none;padding:0}
.kctl{display:flex;align-items:center;gap:6px}
.verdict{min-width:74px;text-align:right;font-size:12px;color:var(--muted);white-space:nowrap}
.verdict .mark{font-size:13px;margin-right:3px}
.btn-s{padding:4px 10px;font-size:12.5px;border-radius:6px}

/* ---- toggle switch ---- */
.switch{display:inline-flex;align-items:center;gap:7px;margin:0;font-size:12px;color:var(--muted);
  cursor:pointer;user-select:none;white-space:nowrap}
.switch input{appearance:none;-webkit-appearance:none;width:34px;height:20px;padding:0;margin:0;
  border:none;border-radius:999px;background:var(--line);position:relative;cursor:pointer;
  transition:background .15s;flex:none}
.switch input::after{content:"";position:absolute;top:3px;left:3px;width:14px;height:14px;
  border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.25);transition:transform .15s}
.switch input:checked{background:var(--accent)}
.switch input:checked::after{transform:translateX(14px)}
.switch input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* ---- diagnostics ---- */
.diag{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.diag-box{border:1px solid var(--line);border-radius:10px;padding:14px;background:var(--bg)}
.diag-box h3{margin:0 0 6px}

/* ---- Home Assistant: row lists ---- */
.list{border:1px solid var(--line);border-radius:10px;overflow:hidden}
.lrow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;
  padding:10px 12px;margin:0;color:var(--text);font-size:14px}
.lrow+.lrow{border-top:1px solid var(--line)}
label.lrow{cursor:pointer}
label.lrow:hover{background:var(--bg)}
.lrow .sub{display:block;color:var(--muted);font-size:12.5px;margin-top:2px}
.lrow code{font-size:11.5px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;
  border:1px solid var(--line);color:var(--muted);margin-left:6px;vertical-align:1px}
.tag.warn{border-color:color-mix(in srgb,#d97706 55%,transparent);color:#d97706}
.empty{padding:12px;color:var(--muted);font-size:13px}
.callout{margin:12px 0 4px;padding:11px 13px;border-radius:9px;font-size:13px;line-height:1.5;
  background:color-mix(in srgb,var(--accent) 8%,transparent);
  border:1px solid color-mix(in srgb,var(--accent) 30%,transparent)}
.count{color:var(--muted);font-weight:400;font-size:13px}
td .sub{display:block;color:var(--muted);font-size:12px;margin-top:2px}
.warnish{color:#d97706 !important}
td.onbus{font-size:13px;min-width:150px}
#s-warn:not(:empty){padding:10px 13px;border-radius:9px;color:var(--text);
  background:color-mix(in srgb,var(--bad) 10%,transparent);
  border:1px solid color-mix(in srgb,var(--bad) 35%,transparent);margin:0 0 16px}

/* ---- toast, so actions on any tab give feedback where you are ---- */
#toast{position:fixed;left:50%;bottom:22px;transform:translate(-50%,20px);opacity:0;
  background:var(--card);color:var(--text);border:1px solid var(--line);border-left:4px solid var(--accent);
  border-radius:9px;padding:10px 16px;font-size:13.5px;box-shadow:0 6px 24px rgba(0,0,0,.18);
  transition:opacity .18s,transform .18s;pointer-events:none;z-index:50;max-width:min(92vw,560px)}
#toast.show{opacity:1;transform:translate(-50%,0)}
#toast.ok{border-left-color:var(--ok)}
#toast.bad{border-left-color:var(--bad)}

@media (max-width:640px){
  .brand h1{font-size:17px}
  .chips{margin-left:0;width:100%}
  .card{padding:14px}
  .krow{grid-template-columns:1fr}
  .kctl{justify-content:flex-start;flex-wrap:wrap}
  .verdict{text-align:left;min-width:0}
  .toolbar-foot .expand{margin-left:0}
}
</style>
</head>
<body>
<header>
  <div class="hwrap">
    <div class="brand">
      <h1>CEC Bridge</h1>
      <div class="chips">
        <span class="chip"><span class="dot" id="s-dot"></span><span id="s-mqtt">connecting…</span></span>
        <span class="chip">HDMI <b id="s-phys">…</b></span>
        <span class="chip">CEC <b id="s-log">…</b></span>
        <span class="chip">TV <b id="s-power">…</b></span>
      </div>
    </div>
    <nav id="tabs" role="tablist">
      <button role="tab" data-tab="control" onclick="showTab('control')">Control</button>
      <button role="tab" data-tab="devices" onclick="showTab('devices')">Devices<span class="badge" id="b-devices">0</span></button>
      <button role="tab" data-tab="inputs" onclick="showTab('inputs')">Inputs<span class="badge" id="b-inputs">0</span></button>
      <button role="tab" data-tab="keys" onclick="showTab('keys')">Remote keys</button>
      <button role="tab" data-tab="ha" onclick="showTab('ha')">Discovery<span class="badge" id="b-ha">0</span></button>
      <button role="tab" data-tab="settings" onclick="showTab('settings')">Settings</button>
      <button role="tab" data-tab="reference" onclick="showTab('reference')">MQTT</button>
      <button role="tab" data-tab="log" onclick="showTab('log')">Log</button>
    </nav>
  </div>
</header>
<main>
<p class="hint" id="s-warn" style="margin-top:0"></p>

<div class="panel" id="panel-control">
<section class="card">
  <h2>Test commands</h2>
  <div class="row" id="input-buttons"></div>
  <p class="hint" id="input-hint" style="display:none"></p>
  <div class="row" style="margin-top:8px">
    <button onclick="run('tv_on')">TV on</button>
    <button onclick="run('tv_standby')">TV standby</button>
    <button onclick="run('standby_all')">All standby</button>
    <button onclick="run('power_status')">Power status</button>
    <button onclick="run('topology')">Show topology</button>
    <button onclick="run('reconfigure')">Re-register CEC</button>
  </div>
  <div class="grid" style="margin-top:12px">
    <div><label>Any physical address</label>
      <div class="row"><input id="t-addr" placeholder="2.0.0.0" style="flex:1"><button onclick="run('active_source',v('t-addr'))">Switch</button></div></div>
    <div><label>Remote key (see “Remote keys” below for all of them)</label>
      <div class="row"><select id="t-key" style="flex:1"></select><button onclick="run('key',v('t-key'))">Send</button></div></div>
    <div><label>Raw cec-ctl arguments</label>
      <div class="row"><input id="t-raw" placeholder="--to 0 --give-osd-name" style="flex:1"><button onclick="run('raw',v('t-raw'))">Run</button></div></div>
  </div>
  <pre id="out">Output of commands appears here.</pre>
</section>

</div><!-- /control -->

<div class="panel" id="panel-devices">
<section class="card" id="card-devices">
  <h2>Devices on the bus
    <span class="save"><button onclick="scanDevices()">Scan</button></span></h2>
  <p class="hint" style="margin-top:0">HDMI-CEC is one shared wire, not a chain: every device sees every message, so nothing is routed “through” the AVR. You address a device by its <b>logical address</b> — the TV is always 0, an AVR or soundbar is 5, and players take 4, 8 or 11. Add <code>@</code> and that number to any command to aim it there.</p>
  <div class="tablewrap"><table><thead><tr><th>Address</th><th>Device</th><th>Role</th><th>HDMI</th><th>Power</th><th></th></tr></thead><tbody id="devices"><tr><td colspan="6" class="hint">Press Scan to see what is on the bus.</td></tr></tbody></table></div>
  <span class="msg" id="dev-msg"></span>
</section>

</div><!-- /devices -->

<div class="panel" id="panel-inputs">
<section class="card" id="card-inputs">
  <h2>Inputs
    <span class="save"><span class="pill" id="dirty-inputs"></span>
      <button class="primary" onclick="save('inputs')">Save</button></span></h2>
  <p class="hint" style="margin-top:0">One row per TV input you want to switch to. Address = HDMI port: HDMI 1 is 1.0.0.0, HDMI 2 is 2.0.0.0 and so on. Everything else on this page — the test buttons above and the command reference below — follows these rows as you edit them.</p>
  <div class="tablewrap"><table class="inputs"><thead><tr><th>ID (used in MQTT)</th><th>Name</th><th>Address</th><th>On the bus</th><th></th></tr></thead><tbody id="inputs"></tbody></table></div>
  <div class="row" style="margin-top:10px">
    <button class="primary" onclick="detect()">Detect inputs</button>
    <button onclick="addInput()">+ Add input</button>
    <span class="msg" id="detect-msg"></span>
  </div>
  <p class="hint">Detect scans the HDMI bus and adds a row for every device it finds, named as the device names itself. Devices that are switched off do not answer, so switch on what you want found, or add it by hand.</p>
</section>

</div><!-- /inputs -->

<div class="panel" id="panel-keys">
<section class="card" id="card-keys">
  <h2>Remote keys
    <span class="save"><span class="pill" id="dirty-keys"></span>
      <button class="primary" onclick="save('keys')">Save</button></span></h2>
  <p class="lead">All 88 keys HDMI-CEC defines. <b>Send</b> presses a key once, the way you would use it. <b>Test</b> is for diagnosing: it presses the key ten times for real and records how many presses the device <i>acknowledged</i> — received, which is not the same as acted on. <b>Announce</b> makes the key discoverable, so Home Assistant or any other MQTT discovery client gets a button for it. Over MQTT, every key goes to <code id="key-topic">cec_bridge/cmd/key</code>.</p>

  <div class="toolbar">
    <div class="target">
      <label for="key-dest">Send keys to</label>
      <select id="key-dest" onchange="renderKeys();renderRef()"></select>
    </div>
    <input id="key-filter" type="search" placeholder="Filter keys…" oninput="renderKeys()">
    <div class="seg" role="group" aria-label="Show">
      <button data-view="all" onclick="setKeyView('all')">All</button>
      <button data-view="ha" onclick="setKeyView('ha')">Announced</button>
      <button data-view="tested" onclick="setKeyView('tested')">Tested</button>
    </div>
  </div>
  <div class="toolbar-foot">
    <span class="hint" id="key-count" style="margin:0"></span>
    <span class="legend" id="key-legend"></span>
    <span class="expand">
      <a href="#" onclick="expandKeys(true);return false">Expand all</a> ·
      <a href="#" onclick="expandKeys(false);return false">Collapse all</a>
    </span>
  </div>
  <div id="key-groups"></div>
</section>

<section class="card" id="card-diag">
  <h2>Diagnostics</h2>
  <p class="lead">For when a key does nothing. Both tools talk to the device picked under <i>Send keys to</i> above.</p>
  <div class="diag">
    <div class="diag-box">
      <h3>Test a key thoroughly</h3>
      <p class="hint" style="margin-top:0">Presses one key repeatedly and counts how many presses the device acknowledges. All of them and nothing happens on screen: the device ignores that key, and no setting will change it. None of them: the message is not getting through at all.</p>
      <div class="row" style="margin-top:10px">
        <select id="kt-key" style="flex:1;min-width:180px"></select>
        <select id="kt-count" style="width:auto"><option>5</option><option selected>10</option><option>20</option></select>
        <button onclick="keyTest()">Run test</button>
      </div>
      <p class="msg" id="kt-msg" style="margin:8px 0 0"></p>
    </div>
    <div class="diag-box">
      <h3>Ask what the device supports</h3>
      <p class="hint" style="margin-top:0">CEC lets a device advertise one thing about remote keys: its <b>RC profile</b>. Everything else is up to the manufacturer, which is why testing beats assuming. Only CEC 2.0 devices answer this.</p>
      <div class="row" style="margin-top:10px">
        <button onclick="askFeatures()">Ask the device</button>
      </div>
      <p class="msg" id="ft-msg" style="margin:8px 0 0"></p>
    </div>
  </div>
  <pre id="diag-out" style="display:none"></pre>
</section>

</div><!-- /keys -->

<div class="panel" id="panel-ha">
<section class="card" id="card-ha">
  <h2>MQTT discovery
    <span class="save"><span class="pill" id="dirty-ha"></span>
      <button class="primary" onclick="save('ha')">Save</button></span></h2>
  <p class="lead">What the bridge announces over MQTT discovery, so Home Assistant — or openHAB, ioBroker, anything else that reads the Home Assistant discovery format — creates the entities for you. Switch something off and it is withdrawn when you save. Every MQTT topic keeps working either way: announcing only decides what shows up automatically.</p>
  <div class="callout" id="disc-off" style="display:none;border-color:color-mix(in srgb,var(--bad) 35%,transparent);background:color-mix(in srgb,var(--bad) 8%,transparent)">
    <b>Discovery is switched off in Settings,</b> so nothing below is announced right now. The switches keep your choices for when you turn it back on.</div>

  <h3>Built-in controls</h3>
  <div id="ha-fixed" class="list"></div>
  <div class="callout">
    <b>Use TV on and TV standby for power.</b> They send CEC's dedicated Image View On and Standby messages, which almost every TV supports. The <code>power-on-function</code> and <code>power-off-function</code> keys look similar but are optional remote-key codes that many TVs ignore.
  </div>

  <h3>Input buttons <span class="count" id="ha-inputs-count"></span></h3>
  <div id="ha-inputs" class="list"></div>

  <h3>Remote key buttons <span class="count" id="ha-keys-count"></span></h3>
  <div id="ha-keys" class="list"></div>
</section>

</div><!-- /ha -->

<div class="panel" id="panel-settings">
<section class="card" id="card-settings">
  <h2>Settings
    <span class="save"><span class="msg" id="save-msg"></span><span class="pill" id="dirty-settings"></span>
      <button class="primary" onclick="save('settings')">Save</button></span></h2>

  <h3>MQTT broker</h3>
  <div class="grid">
    <div><label for="mqtt_host">Address</label><input id="mqtt_host" placeholder="192.168.1.10"></div>
    <div><label for="mqtt_port">Port</label><input id="mqtt_port" type="number"></div>
    <div><label for="mqtt_user">Username</label><input id="mqtt_user" autocomplete="off"></div>
    <div><label for="mqtt_pass">Password</label><input id="mqtt_pass" type="password" autocomplete="new-password"></div>
  </div>
  <p class="hint">Leave the password empty to keep the current one ·
    <a href="#" onclick="clearPw('mqtt');return false">clear it</a></p>

  <h3>Topics and discovery</h3>
  <div class="grid">
    <div><label for="base_topic">Base topic</label><input id="base_topic">
      <p class="hint" style="margin-top:4px">Every command lives under this, e.g. <code>cec_bridge/cmd/key</code>.</p></div>
    <div><label for="device_name">Device name</label><input id="device_name">
      <p class="hint" style="margin-top:4px">What discovery clients call this bridge.</p></div>
  </div>
  <label class="switch" style="margin-top:12px;font-size:14px;color:var(--text)">
    <input type="checkbox" id="discovery_enabled"> Announce entities via MQTT discovery</label>
  <p class="hint">For Home Assistant, or anything else that reads its discovery format. Off means plain MQTT only: every topic still works, nothing is announced.</p>

  <h3>This page</h3>
  <div class="grid" style="grid-template-columns:minmax(0,440px)">
    <div><label for="web_password">Password</label><input id="web_password" type="password" autocomplete="new-password">
      <p class="hint" style="margin-top:4px">Any username works. Empty keeps the current one ·
        <a href="#" onclick="clearPw('web');return false">remove it</a></p></div>
  </div>

  <details style="margin-top:18px">
    <summary>Advanced <span class="count">rarely needs changing</span></summary>
    <div class="body">
      <div class="grid">
        <div><label for="key_hold_ms">Key hold, ms</label><input id="key_hold_ms" type="number" min="0" max="2000">
          <p class="hint" style="margin-top:4px">Raise to 250–400 if a TV misses keys.</p></div>
        <div><label for="retries">Retries</label><input id="retries" type="number" min="0" max="10">
          <p class="hint" style="margin-top:4px">Resends when a device does not acknowledge.</p></div>
        <div><label for="power_poll_seconds">TV power check, seconds</label><input id="power_poll_seconds" type="number">
          <p class="hint" style="margin-top:4px">How often the TV power state is read. 0 turns it off.</p></div>
        <div><label for="discovery_prefix">Discovery prefix</label><input id="discovery_prefix">
          <p class="hint" style="margin-top:4px">Only change it if your discovery client uses another.</p></div>
        <div><label for="cec_device">CEC device</label><input id="cec_device">
          <p class="hint" style="margin-top:4px"><code>/dev/cec0</code> on a Pi 3; a Pi 4 or 5 has one per HDMI port.</p></div>
        <div><label for="web_port">Page port</label><input id="web_port" type="number">
          <p class="hint" style="margin-top:4px">Takes effect after a restart.</p></div>
      </div>
      <label class="switch" style="margin-top:14px;font-size:14px;color:var(--text)">
        <input type="checkbox" id="allow_raw"> Allow raw cec-ctl commands</label>
      <p class="hint">Lets the page and the <code>cmd/raw</code> topic run any cec-ctl arguments. Handy for experiments; turn it off when you are done.</p>
    </div>
  </details>
</section>

</div><!-- /settings -->

<div class="panel" id="panel-reference">
<section class="card">
  <h2>MQTT command reference</h2>
  <p class="hint" style="margin-top:0">Publish the payload to the topic, e.g. from an HA script with <code>mqtt.publish</code>. Every topic follows your base topic and your inputs, so this is always the reference for <i>your</i> setup.</p>
  <div id="ref"></div>
</section>

</div><!-- /reference -->

<div class="panel" id="panel-log">
<section class="card">
  <h2>Log
    <span class="save"><button onclick="refreshStatus()">Refresh</button></span></h2>
  <p class="lead">The last few hundred lines from the bridge. Everything it sends and every error it hit.</p>
  <pre id="log" style="max-height:60vh"></pre>
</section>
</div><!-- /log -->
</main>
<div id="toast" role="status" aria-live="polite"></div>

<script>
let cfg = {}, inputs = [], keyButtons = new Set(), clearMqtt = false, clearWeb = false;
const SETTING_FIELDS = ['mqtt_host','mqtt_port','mqtt_user','base_topic','discovery_prefix',
  'device_name','cec_device','power_poll_seconds','key_hold_ms','retries','web_port'];
let keyStatus = {};

// ---- tabs. The chosen one is remembered in the URL, so a reload and a
// bookmark both land where you were.
function showTab(name) {
  for (const p of document.querySelectorAll('.panel'))
    p.classList.toggle('active', p.id === 'panel-' + name);
  for (const b of document.querySelectorAll('#tabs button'))
    b.setAttribute('aria-selected', b.dataset.tab === name);
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  window.scrollTo({top: 0});
}
window.addEventListener('hashchange', () => showTab(location.hash.slice(1) || 'control'));

function updateBadges() {
  $('b-inputs').textContent = inputs.filter(i => i.id && i.address).length;
  $('b-devices').textContent = boxes().length;
  const fixedOn = (cfg.fixed_entities || []).filter(e => entityOn[e.key] !== false).length;
  $('b-ha').textContent = fixedOn + keyButtons.size +
    inputs.filter(i => i.id && i.address).length;
}
const $ = id => document.getElementById(id);
const v = id => $(id).value;
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, body) {
  const r = await fetch(path, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
  return r.json();
}

async function loadConfig() {
  cfg = await api('/api/config');
  for (const k of SETTING_FIELDS) $(k).value = cfg[k];
  $('allow_raw').checked = cfg.allow_raw;
  $('discovery_enabled').checked = cfg.discovery_enabled !== false;
  renderDiscoveryState();
  $('mqtt_pass').placeholder = cfg.has_mqtt_pass ? '(unchanged)' : '(none)';
  $('web_password').placeholder = cfg.has_web_password ? '(unchanged)' : '(none - page is open)';
  const keyOptions = cfg.key_groups.map(g =>
    `<optgroup label="${esc(g.group)}">` +
    g.keys.map(k => `<option value="${esc(k.key)}">${esc(k.label)} — ${esc(k.key)}</option>`).join('') +
    `</optgroup>`).join('');
  $('t-key').innerHTML = keyOptions;
  $('kt-key').innerHTML = keyOptions;
  inputs = cfg.inputs.map(i => ({...i, _autoId: false}));
  keyButtons = new Set(cfg.key_buttons || []);
  entityOn = Object.assign({}, cfg.entities || {});
  keyStatus = cfg.key_status || {};
  renderDestOptions();
  renderInputs(); renderButtons(); renderKeys(); renderHA(); renderRef(); updateBadges();
}

// The reference and buttons follow these two fields live as well.
$('base_topic').addEventListener('input', renderRef);
$('allow_raw').addEventListener('change', renderRef);
// Any settings edit flags that section as unsaved.
for (const k of [...SETTING_FIELDS, 'mqtt_pass', 'web_password', 'allow_raw', 'discovery_enabled']) {
  const el = $(k);
  if (el) el.addEventListener('input', () => markDirty('settings'));
}
// Ctrl+S / Cmd+S saves, wherever you are on the page.
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save('settings'); }
});

// Every input row edit re-renders the test buttons and the command reference,
// so the page always shows YOUR inputs, not a fixed list.
function onInputEdit(n, field, value) {
  inputs[n][field] = value;
  markDirty('inputs');
  renderHA();
  if (field === 'name' && inputs[n]._autoId !== false) {
    // Keep the id following the name until the user edits the id themselves.
    inputs[n].id = value.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    const cell = document.querySelector(`#inputs tr:nth-child(${n + 1}) .id-cell`);
    if (cell) cell.value = inputs[n].id;
  }
  renderButtons(); renderRef();
}

function saved(i) {  // is this row live on the bridge, or only typed in?
  return cfg.inputs.some(s => s.id === i.id && s.name === i.name && s.address === i.address);
}

// What the last bus scan found at this input's address, so a name that does
// not match the hardware stands out.
let piAddr = '';
function onBus(i) {
  if (!devices.length) return '<span class="hint" style="margin:0">scan to check</span>';
  if (i.address && i.address === piAddr)
    return `This Pi <span class="sub warnish">the bridge's own port: the TV has nothing to show here</span>`;
  const here = devices.filter(d => d.address && d.address === i.address && !d.is_self);
  if (!here.length)
    return `<span class="hint" style="margin:0">no answer</span>
      <span class="sub">off, or not a CEC device — switching to it still works</span>`;
  const main = here.find(d => d.logical === 5) || here[0];
  const own = main.name && !STATUS.test(main.name) ? main.name : (ROLES[main.logical] || '');
  let note = '';
  if (main.logical === 5)
    note = `<span class="sub warnish">this is the AVR itself: the TV shows whatever the AVR has selected</span>`;
  else if (main.logical === 0)
    note = `<span class="sub warnish">this is the TV itself</span>`;
  return `${esc(own)} <span class="hint" style="margin:0">${esc(ROLES[main.logical] || '')} · ${main.logical}</span>${note}`;
}
function renderOnBus(n) { const c = $('onbus-' + n); if (c) c.innerHTML = onBus(inputs[n]); }

function renderInputs() {
  $('inputs').innerHTML = inputs.map((i, n) => `<tr>
    <td><input class="id-cell" value="${esc(i.id)}" oninput="inputs[${n}]._autoId=false;onInputEdit(${n},'id',this.value)"></td>
    <td><input value="${esc(i.name)}" oninput="onInputEdit(${n},'name',this.value)"></td>
    <td><input value="${esc(i.address)}" oninput="onInputEdit(${n},'address',this.value);renderOnBus(${n})" placeholder="3.0.0.0"></td>
    <td class="onbus" id="onbus-${n}">${onBus(i)}</td>
    <td><button onclick="inputs.splice(${n},1);markDirty('inputs');renderInputs();renderButtons();renderRef()">Remove</button></td></tr>`).join('')
    || `<tr><td colspan="5" class="hint">No inputs yet — press “Detect inputs”, or add one by hand.</td></tr>`;
}
function addInput() { inputs.push({id:'', name:'', address:'', _autoId:true}); markDirty('inputs'); renderInputs(); renderButtons(); renderRef(); }

async function detect() {
  $('detect-msg').style.color = 'var(--muted)';
  $('detect-msg').textContent = 'Scanning the HDMI bus…';
  try {
    const r = await api('/api/detect', {existing_ids: inputs.map(i => i.id)});
    const fresh = (r.found || []).filter(d => !inputs.some(i => i.address === d.address));
    // Detected rows are not saved yet, so tidying the name still tidies the id.
    // Once saved, the id stays put, because automations may already use it.
    fresh.forEach(d => inputs.push({id:d.id, name:d.name, address:d.address, _autoId:true}));
    if (fresh.length) markDirty('inputs');
    renderInputs(); renderButtons(); renderRef();
    $('out').textContent = (r.ok ? '✔ OK' : '✘ Failed') + '\n\n' + (r.output || '');
    $('detect-msg').style.color = fresh.length ? 'var(--ok)' : 'var(--muted)';
    $('detect-msg').textContent = fresh.length
      ? `Added ${fresh.length} device${fresh.length > 1 ? 's' : ''} — check the names, then Save settings.`
      : ((r.found || []).length ? 'Everything found is already listed.'
                                : 'Nothing answered. Switch the devices on and try again.');
  } catch (e) { $('detect-msg').style.color = 'var(--bad)'; $('detect-msg').textContent = 'Failed: ' + e; }
}

function renderButtons() {
  const usable = inputs.filter(i => i.id && i.address);
  $('input-buttons').innerHTML = usable.map(i => saved(i)
    ? `<button class="primary" onclick="run('input','${esc(i.id)}')">${esc(i.name || i.id)} <small>(${esc(i.address)})</small></button>`
    : `<button disabled title="Save settings before testing this one">${esc(i.name || i.id)} <small>(unsaved)</small></button>`
  ).join('');
  const pending = usable.filter(i => !saved(i)).length;
  const hint = $('input-hint');
  hint.style.display = (usable.length && !pending) ? 'none' : '';
  hint.textContent = !usable.length
    ? 'No inputs defined yet — add them under “Inputs” below and they will appear here.'
    : (pending ? 'Greyed-out buttons are edits you have not saved yet.' : '');
}

// ---- Everything announced via MQTT discovery, in one place
let entityOn = {};

function toggleEntity(key, on) {
  entityOn[key] = on;
  markDirty('ha');
  renderHA();
}

function removeKeyButton(entry) {
  keyButtons.delete(entry);
  markDirty('ha');
  renderHA(); renderKeys(); renderRef();
}

// The same action each Home Assistant entity performs, runnable from here.
function fixedAction(key) {
  if (key === 'tv_on' || key === 'tv_standby' || key === 'standby_all')
    return `<button class="btn-s" onclick="runFixed('${key}')">Send</button>`;
  if (key === 'input_select') {
    const ins = inputs.filter(i => i.id && i.address && saved(i));
    return ins.length
      ? `<select id="ha-input-pick" class="btn-s" style="width:auto">${ins.map(i =>
          `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('')}</select>
         <button class="btn-s" onclick="runFixed('input', v('ha-input-pick'))">Switch</button>`
      : '';
  }
  if (key === 'tv_power')
    return `<span class="verdict" id="ha-power">${esc($('s-power').textContent || '')}</span>
            <button class="btn-s" onclick="runFixed('power_status')">Check</button>`;
  return '';
}

const FIXED_DONE = {tv_on: 'TV on sent', tv_standby: 'TV standby sent',
                    standby_all: 'Standby sent to every device', power_status: 'TV power'};
async function runFixed(command, payload = '') {
  try {
    const r = await api('/api/command', {command, payload});
    let msg;
    if (command === 'power_status') {
      const m = (r.output || '').match(/power:\s*(\S+)/);
      msg = `TV power: ${m ? m[1] : 'unknown'}`;
      if ($('ha-power')) $('ha-power').textContent = m ? m[1] : 'unknown';
    } else if (command === 'input') {
      const i = inputs.find(x => x.id === payload);
      msg = `Switched to ${i ? i.name : payload}`;
    } else msg = FIXED_DONE[command];
    toast(r.ok ? msg : `${command}: ${(r.output || 'failed').split('\n').pop()}`, r.ok ? 'ok' : 'bad');
    refreshStatus();
  } catch (e) { toast('Failed: ' + e, 'bad'); }
}

function renderDiscoveryState() {
  const on = $('discovery_enabled').checked;
  if ($('disc-off')) $('disc-off').style.display = on ? 'none' : '';
}
$('discovery_enabled').addEventListener('change', renderDiscoveryState);

function renderHA() {
  updateBadges();
  $('ha-fixed').innerHTML = (cfg.fixed_entities || []).map(e => `
    <div class="lrow" data-entity="${esc(e.key)}">
      <span>${esc(e.name)} <span class="tag">${esc(e.kind)}</span>
        <span class="sub">${esc(e.what)}</span></span>
      <span class="kctl">${fixedAction(e.key)}
        <label class="switch" title="Announce via MQTT discovery"><input type="checkbox" ${entityOn[e.key] === false ? '' : 'checked'}
          onchange="toggleEntity('${esc(e.key)}',this.checked)" aria-label="Announce ${esc(e.name)}">Announce</label></span>
    </div>`).join('');

  const ins = inputs.filter(i => i.id && i.address);
  $('ha-inputs-count').textContent = ins.length ? `${ins.length}, one per input` : '';
  $('ha-inputs').innerHTML = ins.length
    ? ins.map(i => `<div class="lrow"><span>${esc(i.name || i.id)}
        <span class="sub">HDMI <code>${esc(i.address)}</code> · payload <code>${esc(i.id)}</code></span></span>
        ${saved(i) ? `<button class="btn-s" onclick="runFixed('input','${esc(i.id)}')">Switch</button>`
                   : '<span class="hint" style="margin:0">save to use</span>'}</div>`).join('')
    : `<div class="empty">None yet. Every input you add under Inputs gets a button here.</div>`;

  const keys = [...keyButtons].sort();
  $('ha-keys-count').textContent = keys.length ? String(keys.length) : '';
  $('ha-keys').innerHTML = keys.length ? keys.map(k => {
      const name = k.split('@')[0], d = k.split('@')[1] || '0';
      const label = (cfg.key_groups || []).flatMap(g => g.keys).find(x => x.key === name);
      const risky = name === 'power-on-function' || name === 'power-off-function';
      return `<div class="lrow" data-key="${esc(k)}">
        <span>${esc(label ? label.label : name)}${d === '0' ? '' : ` → ${esc(destName(d))}`}
          ${risky ? '<span class="tag warn">remote-key code — prefer TV on / standby</span>' : ''}
          <span class="sub"><code>${esc(k)}</code></span></span>
        <button class="btn-s" onclick="removeKeyButton('${esc(k)}')">Remove</button>
      </div>`;
    }).join('')
    : `<div class="empty">None. Switch on <b>Announce</b> beside a key under Remote keys to add one.</div>`;
}

// ---- Devices on the CEC bus
const ROLES = {0:'TV',1:'Recording 1',2:'Recording 2',3:'Tuner 1',4:'Playback 1',
  5:'Audio system',6:'Tuner 2',7:'Tuner 3',8:'Playback 2',9:'Recording 3',
  10:'Tuner 4',11:'Playback 3',12:'Reserved',13:'Reserved',14:'Specific use',15:'Broadcast'};
let devices = [];

async function scanDevices() {
  $('dev-msg').style.color = 'var(--muted)';
  $('dev-msg').textContent = 'Scanning the bus…';
  try {
    const r = await api('/api/devices', {});
    devices = r.devices || [];
    renderDevices(); renderDestOptions(); renderInputs(); renderKeys(); renderHA(); updateBadges();
    $('dev-msg').style.color = devices.length ? 'var(--ok)' : 'var(--bad)';
    $('dev-msg').textContent = devices.length
      ? busSummary('on the bus')
      : 'Nothing answered — devices in standby do not always reply.';
  } catch (e) { $('dev-msg').style.color = 'var(--bad)'; $('dev-msg').textContent = 'Scan failed: ' + e; }
}

// One box, two addresses: say so, so it does not look like a duplicate.
function sameBox(d) {
  const others = sharing(d).filter(x => x.logical !== d.logical);
  if (!others.length) return '';
  const tuner = d.logical === 3 || d.logical === 6 || d.logical === 7 || d.logical === 10;
  return `<span class="sub">same box as ${others.map(o => o.logical).join(', ')}${
    tuner ? ' — its radio tuner; send keys to the audio system instead' : ''}</span>`;
}

// One physical address is one HDMI connection, so everything answering there
// is the same box: a Denon AVR is both "audio system" (5) and "tuner" (3).
// Show it once, addressed by its most useful logical address.
const PRIORITY = [0, 5, 4, 8, 11, 1, 2, 9, 3, 6, 7, 10, 14, 12, 13];
function boxes() {
  const by = new Map();
  for (const d of devices) {
    const k = d.address || `la:${d.logical}`;
    if (!by.has(k)) by.set(k, []);
    by.get(k).push(d);
  }
  const out = [...by.values()].map(m => {
    m.sort((a, b) => PRIORITY.indexOf(a.logical) - PRIORITY.indexOf(b.logical));
    return {primary: m[0], members: m, address: m[0].address};
  });
  // TV first, then in HDMI tree order so things behind an AVR follow it.
  const key = b => b.address ? b.address.split('.').map(n => parseInt(n, 16)) : [99];
  out.sort((a, b) => {
    const x = key(a), y = key(b);
    for (let i = 0; i < 4; i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) - (y[i] || 0);
    return 0;
  });
  return out;
}

// Where a device sits: 3.0.0.0 is TV input 3; 3.4.0.0 is input 4 on whatever
// sits at 3.0.0.0. That is the whole of HDMI's addressing scheme.
function hdmiPath(address, all) {
  if (!address || address === '0.0.0.0') return '';
  const parts = address.split('.');
  let last = -1;
  parts.forEach((p, i) => { if (p !== '0') last = i; });
  if (last < 0) return '';
  const port = parseInt(parts[last], 16);
  const parentAddr = parts.map((p, i) => i === last ? '0' : p).join('.');
  if (parentAddr === '0.0.0.0') return `TV input ${port}`;
  const parent = all.find(b => b.address === parentAddr);
  return `input ${port} on ${parent ? boxName(parent) : parentAddr}`;
}

function boxName(b) { return deviceName(b.primary); }

function renderDevices() {
  const all = boxes();
  $('devices').innerHTML = all.length ? all.map(b => {
    const d = b.primary, name = boxName(b);
    const nested = b.address && b.address !== '0.0.0.0' &&
      !hdmiPath(b.address, all).startsWith('TV input');
    // Only the primary's own name: the tuner calling itself "AV Receiver" is
    // not what the box is. And not when it differs from ours only by case.
    const reported = d.name && !STATUS.test(d.name) && d.name.toLowerCase() !== name.toLowerCase()
      && d.name !== m_role(b) ? d.name : '';
    // 0x000c03 is the placeholder vendor ID for "HDMI", not a manufacturer.
    const vendor = d.vendor && d.vendor !== 'HDMI' ? d.vendor : '';
    const others = b.members.slice(1);
    // An input of yours pointing at a hub shows whatever the hub passes
    // through; say so, instead of silently renaming the hub after it.
    const pointing = inputs.find(i => i.address && i.address === b.address && i.name !== name);
    return `<tr>
      <td>${b.members.map(m => `<code>${m.logical}</code>`).join(' ')}</td>
      <td>${nested ? '<span class="hint" style="margin:0">↳ </span>' : ''}${esc(name)}${
          d.is_self ? ' <span class="hint">(this Pi)</span>' : ''}${
          vendor ? ` <span class="hint">${esc(vendor)}</span>` : ''}${
          reported ? `<span class="sub">reports itself as ${esc(reported)}</span>` : ''}${
          others.length ? `<span class="sub">also answers as ${others.map(m =>
              `${m.logical} (${esc((ROLES[m.logical] || m.type).toLowerCase())})`).join(', ')}</span>` : ''}${
          pointing && !d.is_self ? `<span class="sub">your input “${esc(pointing.name)}” points at this port</span>` : ''}</td>
      <td>${esc(d.role)}</td>
      <td>${esc(b.address || '—')}${b.address ? `<span class="sub">${esc(hdmiPath(b.address, all))}</span>` : ''}</td>
      <td>${esc(d.power || '—')}</td>
      <td><button onclick="aimAt(${d.logical})"${d.is_self ? ' disabled' : ''}>Send keys here</button></td>
    </tr>`;
  }).join('')
    : `<tr><td colspan="6" class="hint">Nothing found. Devices in standby often do not answer.</td></tr>`;
}
function m_role(b) { return ROLES[b.primary.logical]; }
function busSummary(where) {
  const n = boxes().length, la = devices.length;
  return `${n} device${n === 1 ? '' : 's'} ${where}` +
    (la > n ? ` · ${la} addresses, as some devices answer on more than one` : '');
}

// Your input names win over the device's own OSD name: you called HDMI 3.4.0.0
// "Nvidia Shield TV", so that is what the picker should say. The scan is what
// links a physical address (3.4.0.0, what inputs use) to a logical one (8,
// what commands are addressed to) — there is no way to derive one from the
// other without asking the bus.
// cec-ctl writes "Tx, OK, Rx, Timeout" where a device did not answer.
const STATUS = /^\s*(Tx|Rx)\s*,/i;
function sharing(d) { return devices.filter(x => x.address && x.address === d.address); }

// Your input name labels a device only when it is the one source at that
// address. Never the TV, and never an AVR: an input pointing at the AVR's port
// shows whatever the AVR passes through, and an AVR holds two addresses at
// once (5 audio system, 3 tuner for its radio). Mirrors input_name_applies().
function inputNameApplies(d) {
  return d.address && d.logical !== 0 && d.logical !== 5 && sharing(d).length === 1;
}
function deviceName(d) {
  if (inputNameApplies(d)) {
    const match = inputs.find(i => i.address && i.address === d.address);
    if (match && match.name) return match.name;
  }
  if (d.name && d.name !== d.type && !STATUS.test(d.name)) return d.name;
  return ROLES[d.logical] || d.type || `Device ${d.logical}`;
}

function renderDestOptions() {
  const sel = $('key-dest'), current = sel.value || '0';
  const self = String(($('s-log') && $('s-log').textContent) || '').trim();
  let opts = '';
  // One entry per box, aimed at its most useful address — the AVR's audio
  // system, not its radio tuner — and never the Pi itself.
  for (const b of boxes()) {
    const d = b.primary;
    if (d.is_self || String(d.logical) === self) continue;
    opts += `<option value="${d.logical}">${esc(boxName(b))} — ${d.logical}</option>`;
  }
  // Before any scan, offer the addresses that exist on almost every setup.
  if (!devices.length) {
    for (const la of [0, 5, 4, 8, 11])
      if (String(la) !== self) opts += `<option value="${la}">${ROLES[la]} — ${la}</option>`;
  }
  // Keep a previously chosen address selectable even if it has gone quiet.
  if (![...new DOMParser().parseFromString(`<select>${opts}</select>`, 'text/html')
        .querySelectorAll('option')].some(o => o.value === current) && current !== '0')
    opts += `<option value="${esc(current)}">${esc(destName(current))} — ${esc(current)} (not answering)</option>`;
  sel.innerHTML = opts;
  sel.value = [...sel.options].some(o => o.value === current) ? current : '0';
}

function destName(la) {
  const d = devices.find(x => String(x.logical) === String(la));
  return d ? deviceName(d) : (ROLES[la] || `device ${la}`);
}

function aimAt(la) {
  $('key-dest').value = String(la);
  renderKeys(); renderRef();
  showTab('keys');
  toast(`Keys now go to ${destName(la)}`);
}

// Every user-control code in CEC is optional for the manufacturer, so there is
// no honest static list of "supported" keys. These marks come from actually
// testing the key against your TV, which is the only reliable answer.
function keyMark(id) {
  const st = keyStatus[id];
  if (!st) return '';
  const [acked, sent] = st;
  if (acked === sent) return `<b class="mark ok" title="${acked}/${sent} presses acknowledged. The device received the key; whether it acts on it is something only you can see.">\u2713</b>acked`;
  if (acked === 0) return `<b class="mark bad" title="0/${sent} presses acknowledged: the device did not accept this key">\u2717</b>no ack`;
  return `<b class="mark part" title="${acked}/${sent} acknowledged">~</b>${acked}/${sent}`;
}

// Test presses a key ten times for real. For these, that means ten power
// toggles, ten ejects, a factory-setup menu... so ask first.
const DISRUPTIVE = {
  'power': 'switch the TV on and off ten times',
  'power-toggle-function': 'switch the TV on and off ten times',
  'power-off-function': 'send power-off ten times',
  'eject': 'eject ten times',
  'record': 'start recording',
  'input-select': 'cycle through inputs ten times',
  'select-av-input-function': 'change inputs',
  'channel-up': 'change channel ten times',
  'channel-down': 'change channel ten times',
  'previous-channel': 'change channel ten times',
  'initial-configuration': 'open the initial setup menu',
  '3d-mode': 'toggle 3D mode ten times',
};
function confirmTest(key, count) {
  const what = DISRUPTIVE[key.split('@')[0]];
  if (!what) return true;
  return confirm(`Testing presses the key ${count} times for real, so this will ${what}.\n\nRun the test anyway?`);
}

let toastTimer;
function toast(text, kind = '') {
  const t = $('toast');
  t.textContent = text;
  t.className = 'show ' + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = kind; }, 2600);
}

// Press a key once, right from its row.
async function sendKey(key) {
  const id = keyId(key);
  try {
    const r = await api('/api/command', {command: 'key', payload: id});
    toast(r.ok ? `Sent ${id}` : `${id}: ${(r.output || 'failed').split('\n')[0]}`, r.ok ? 'ok' : 'bad');
  } catch (e) { toast('Send failed: ' + e, 'bad'); }
}

// Test from the row: the verdict appears in place, no need to look elsewhere.
async function testOneKey(key) {
  const id = keyId(key);
  if (!confirmTest(id, 10)) return;
  const cell = document.querySelector(`.krow[data-key="${CSS.escape(id)}"] .verdict`);
  if (cell) cell.textContent = 'testing…';
  try {
    const r = await api('/api/keytest', {key: id, count: 10});
    if (r.ok) keyStatus[id] = [r.acked, r.sent];
    renderKeys();
    toast(r.ok ? `${id}: ${r.acked}/${r.sent} acknowledged${r.acked === 0 ? ' — the device ignores it' : ''}`
               : `${id}: test failed`, r.ok && r.acked === r.sent ? 'ok' : 'bad');
  } catch (e) { toast('Test failed: ' + e, 'bad'); renderKeys(); }
}

let keyView = 'all';
function setKeyView(view) { keyView = view; renderKeys(); }
function expandKeys(open) {
  document.querySelectorAll('#key-groups details').forEach(d => d.open = open);
}

// ---- Remote keys: every CEC key, grouped, filterable, tickable for HA
// A key is stored as "volume-up" for the TV, or "volume-up@5" for another
// device, so the same key can have a button per device.
function keyId(key) { const d = $('key-dest').value; return d === '0' ? key : `${key}@${d}`; }

function toggleKey(key, on) {
  const id = keyId(key);
  on ? keyButtons.add(id) : keyButtons.delete(id);
  markDirty('keys');
  renderKeys(); renderHA(); renderRef();
}

// Send one key many times and report how many the TV acknowledged.
async function askFeatures() {
  const dest = $('key-dest').value;
  $('ft-msg').style.color = 'var(--muted)';
  $('ft-msg').textContent = 'Asking…';
  try {
    const r = await api('/api/features', {dest});
    $('diag-out').style.display = ''; $('diag-out').textContent = r.output || '';
    $('ft-msg').style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
    $('ft-msg').textContent = r.ok
      ? (r.features.rc_profile ? `RC profile: ${r.features.rc_profile}` : 'answered')
      : 'no answer (CEC 2.0 only)';
  } catch (e) { $('ft-msg').style.color = 'var(--bad)'; $('ft-msg').textContent = 'failed: ' + e; }
}

async function keyTest() {
  const key = v('kt-key'), count = v('kt-count');
  if (!confirmTest(key, count)) return;
  $('kt-msg').style.color = 'var(--muted)';
  $('kt-msg').textContent = `Sending ${key} ${count} times…`;
  $('diag-out').style.display = ''; $('diag-out').textContent = `Testing ${key}…`;
  try {
    const r = await api('/api/keytest', {key, count: Number(count)});
    $('diag-out').textContent = (r.ok ? '' : '✘ ') + (r.output || '');
    if (r.ok) { keyStatus[key] = [r.acked, r.sent]; renderKeys(); }
    $('kt-msg').style.color = !r.ok ? 'var(--bad)'
      : (r.acked === r.sent ? 'var(--ok)' : r.acked ? 'var(--bad)' : 'var(--bad)');
    $('kt-msg').textContent = r.ok ? `${r.acked}/${r.sent} acknowledged` : 'failed';
  } catch (e) { $('kt-msg').style.color = 'var(--bad)'; $('kt-msg').textContent = 'failed: ' + e; }
}

function renderKeys() {
  const q = ($('key-filter').value || '').trim().toLowerCase();
  const dest = $('key-dest').value;
  const inView = k => keyView === 'all' ||
    (keyView === 'ha' && keyButtons.has(keyId(k.key))) ||
    (keyView === 'tested' && keyStatus[keyId(k.key)]);
  const match = k => inView(k) && (!q || k.key.includes(q) || k.label.toLowerCase().includes(q));
  for (const b of document.querySelectorAll('.seg button'))
    b.setAttribute('aria-pressed', b.dataset.view === keyView);

  // Keep open whatever was open: switching device or flipping a switch
  // should not collapse the section you are working in.
  const wasOpen = new Set([...$('key-groups').querySelectorAll('details[data-group]')]
    .filter(d => d.open).map(d => d.dataset.group));
  const first = !$('key-groups').children.length;
  let shown = 0;
  $('key-groups').innerHTML = cfg.key_groups.map((g, gi) => {
    const keys = g.keys.filter(match);
    if (!keys.length) return '';
    shown += keys.length;
    const inHA = keys.filter(k => keyButtons.has(keyId(k.key))).length;
    const open = q || keyView !== 'all' || wasOpen.has(g.group) || (first && gi === 0);
    return `<details data-group="${esc(g.group)}"${open ? ' open' : ''}>
      <summary>${esc(g.group)} <span class="count">${keys.length} key${keys.length > 1 ? 's' : ''}${inHA ? ` · ${inHA} announced` : ''}</span></summary>
      <div class="body keylist">${keys.map(k => {
        const id = keyId(k.key);
        return `<div class="krow" data-key="${esc(id)}">
          <div class="kname"><span class="label">${esc(k.label)}</span><code>${esc(id)}</code></div>
          <div class="kctl">
            <span class="verdict">${keyMark(id)}</span>
            <button class="btn-s" onclick="sendKey('${esc(k.key)}')" title="Press this key once, as a remote would">Send</button>
            <button class="btn-s" onclick="testOneKey('${esc(k.key)}')" title="Press it 10 times and count how many the device acknowledges">Test</button>
            <label class="switch" title="Announce via MQTT discovery, so discovery clients get a button for this key">
              <input type="checkbox" ${keyButtons.has(id) ? 'checked' : ''}
                onchange="toggleKey('${esc(k.key)}',this.checked)" aria-label="Announce ${esc(k.label)}">Announce</label>
          </div>
        </div>`;
      }).join('')}</div></details>`;
  }).join('') || `<div class="empty">${
      keyView === 'ha' ? 'No keys are announced for this device yet.'
    : keyView === 'tested' ? 'Nothing tested for this device yet. Press Test beside a key.'
    : 'No key matches that filter.'}</div>`;

  $('key-legend').innerHTML = Object.keys(keyStatus).length
    ? `<span><b class="mark ok">✓</b> acknowledged</span>
       <span><b class="mark part">~</b> sometimes</span>
       <span><b class="mark bad">✗</b> never acknowledged</span>
       <span>· acknowledged means received, not necessarily acted on</span>`
    : '';
  const total = cfg.key_groups.reduce((n, g) => n + g.keys.length, 0);
  const here = [...keyButtons].filter(k => (k.split('@')[1] || '0') === dest).length;
  $('key-count').textContent = `${shown} of ${total} keys` +
    (keyButtons.size ? ` · ${keyButtons.size} announced${
      dest !== '0' || here !== keyButtons.size ? ` (${here} for this device)` : ''}` : '');
  updateBadges();
}

// ---- MQTT reference: built from your inputs, keys and base topic
function renderRef() {
  const b = ($('base_topic').value || cfg.base_topic).replace(/^\/+|\/+$/g, '');
  $('key-topic').textContent = `${b}/cmd/key`;
  const sec = (title, rows, open) => rows.length ? `<details${open ? ' open' : ''}>
      <summary>${esc(title)} <span class="count">${rows.length} topic${rows.length > 1 ? 's' : ''}</span></summary>
      <div class="body"><div class="tablewrap"><table><thead><tr><th>Topic</th><th>Payload</th><th>What it does</th></tr></thead><tbody>${rows.join('')}</tbody></table></div></div>
    </details>` : '';
  const row = (t, p, d) => `<tr><td><code>${esc(t)}</code></td><td>${p}</td><td>${d}</td></tr>`;

  const list = inputs.filter(i => i.id && i.address);
  const ins = list.length
    ? list.map(i => row(`${b}/cmd/input`, `<code>${esc(i.id)}</code>`,
        `Switch to ${esc(i.name || i.id)} (${esc(i.address)})${saved(i) ? '' : ' <i>— unsaved</i>'}. The name or the address works as payload too.`))
    : [row(`${b}/cmd/input`, '<code>&lt;input id&gt;</code>', 'Switch to one of your inputs. Add some under “Inputs” and each is listed here by name.')];
  ins.push(row(`${b}/cmd/active_source`, '<code>2.0.0.0</code>', 'Switch to any physical address, even one not in your input list.'));

  const power = [
    row(`${b}/cmd/tv_on`, 'empty, or <code>@5</code>', 'Wake the TV (Image View On), or the device at that address.'),
    row(`${b}/cmd/tv_standby`, 'empty, or <code>@5</code>', 'Put the TV in standby, or just the AVR with <code>@5</code>.'),
    row(`${b}/cmd/standby_all`, 'anything', 'Broadcast standby to every CEC device at once.'),
    row(`${b}/cmd/power_status`, 'empty, or <code>@5</code>', `Ask a device its power state. For the TV the answer also lands on <code>${esc(b)}/state/tv_power</code>.`),
  ];

  const keys = [row(`${b}/cmd/key`, '<code>volume-down</code>',
      `Send any of the ${cfg.key_groups.reduce((n, g) => n + g.keys.length, 0)} CEC keys to the TV. The full list is under “Remote keys” above.`),
    row(`${b}/cmd/key`, '<code>volume-up@5</code>', 'Send a key to any device on the bus instead of the TV: 5 is the audio system, 4/8/11 are players such as a Shield.')];
  [...keyButtons].sort().forEach(k => {
    const d = k.split('@')[1] || '0';
    keys.push(row(`${b}/cmd/key`, `<code>${esc(k)}</code>`,
      `Announced, so discovery clients also get a button for it${d === '0' ? '' : `, aimed at ${esc(destName(d))}`}.`));
  });

  const diag = [
    row(`${b}/cmd/topology`, 'anything', `Rescan the HDMI bus; the listing lands on <code>${esc(b)}/topology</code>.`),
    row(`${b}/cmd/reconfigure`, 'anything', 'Re-register the Pi on the CEC bus, after a TV power cut for instance.'),
  ];
  if ($('allow_raw').checked) diag.push(row(`${b}/cmd/raw`, '<code>--to 0 --give-osd-name</code>', 'Run cec-ctl with these arguments (advanced).'));

  const state = [
    row(`${b}/status`, '<i>published</i>', '<code>online</code> or <code>offline</code>. Discovery clients use this for availability.'),
    row(`${b}/state/tv_power`, '<i>published</i>', '<code>on</code>, <code>standby</code>, <code>to-on</code>, <code>to-standby</code> or <code>unknown</code>.'),
    row(`${b}/state/input`, '<i>published</i>', 'Name of the last input the bridge switched to.'),
    row(`${b}/topology`, '<i>published</i>', 'Output of the last topology scan.'),
  ];

  $('ref').innerHTML = sec('Inputs', ins, true) + sec('Power', power) +
                       sec('Remote keys', keys) + sec('Diagnostics and maintenance', diag) +
                       sec('Topics the bridge publishes', state);
}

async function run(command, payload='') {
  $('out').textContent = `Running ${command} ${payload}…`;
  try {
    const r = await api('/api/command', {command, payload});
    $('out').textContent = (r.ok ? '✔ OK' : '✘ Failed') + '\n\n' + (r.output || '');
  } catch (e) { $('out').textContent = 'Error: ' + e; }
  refreshStatus();
}

function clearPw(which) {
  if (which === 'mqtt') { clearMqtt = true; $('mqtt_pass').value = ''; $('mqtt_pass').placeholder = '(will be cleared on save)'; }
  else { clearWeb = true; $('web_password').value = ''; $('web_password').placeholder = '(will be removed on save)'; }
}

// Every section's Save sends the whole page, because the page holds all of
// it — so it never matters which button you reach for. The message appears
// beside the one you pressed, which is the part you are looking at.
async function save(section) {
  const body = {inputs: inputs.map(i => ({id:(i.id||'').trim(), name:(i.name||'').trim(), address:(i.address||'').trim()})),
                key_buttons: [...keyButtons], entities: entityOn};
  for (const k of [...SETTING_FIELDS, 'mqtt_pass', 'web_password']) body[k] = v(k);
  body.allow_raw = $('allow_raw').checked;
  body.discovery_enabled = $('discovery_enabled').checked;
  body.clear_mqtt_pass = clearMqtt; body.clear_web_password = clearWeb;

  const spots = section ? [$('dirty-' + section)] : [];
  spots.forEach(s => { s.className = 'pill'; s.textContent = 'Saving…'; });
  const r = await api('/api/config', body);
  const msg = r.ok ? (r.message.startsWith('Saved') && r.message.length < 8 ? 'Saved' : r.message)
                   : r.errors.join(' ');
  for (const el of [$('save-msg'), ...spots]) {
    if (!el) continue;
    el.style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
    el.className = el.id.startsWith('dirty-') ? 'pill' : 'msg';
    el.textContent = msg;
  }
  if (r.ok) {
    clearMqtt = clearWeb = false; $('mqtt_pass').value = ''; $('web_password').value = '';
    await loadConfig();
    markClean();
    setTimeout(() => spots.forEach(s => { if (s.textContent === msg) s.textContent = ''; }), 2500);
  }
}

// Tells you which sections hold edits you have not saved yet.
function markDirty(section) {
  const el = $('dirty-' + section);
  if (!el) return;
  el.style.color = ''; el.className = 'pill on'; el.textContent = 'unsaved changes';
}
function markClean() {
  for (const s of ['inputs', 'keys', 'ha', 'settings']) {
    const el = $('dirty-' + s);
    if (el) { el.textContent = ''; el.className = 'pill'; el.style.color = ''; }
  }
}

async function refreshStatus() {
  try {
    const s = await api('/api/status');
    $('s-dot').style.background = s.mqtt_connected ? 'var(--ok)' : 'var(--bad)';
    $('s-mqtt').textContent = s.mqtt_connected ? 'Connected'
      : (s.mqtt_error || 'Not connected').replace(/\.\.\.$/, '…');
    $('s-phys').textContent = s.phys_addr;
    if (s.phys_addr !== piAddr && /^[0-9a-f](\.[0-9a-f]){3}$/i.test(s.phys_addr) && s.phys_addr !== 'f.f.f.f') {
      piAddr = s.phys_addr;
      inputs.forEach((_, n) => renderOnBus(n));
    }
    $('s-log').textContent = s.logical_addr;
    $('s-power').textContent = s.tv_power;
    $('s-warn').textContent = (s.phys_addr === 'f.f.f.f' || s.phys_addr === 'unknown')
      ? 'The Pi cannot see the TV on HDMI (address f.f.f.f). See "Troubleshooting" in the guide.' : '';
    const log = $('log'), atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 5;
    log.textContent = s.log.join('\n');
    if (atBottom) log.scrollTop = log.scrollHeight;
  } catch (e) {}
}

// The bridge scans the bus at startup, so names are ready on first load.
async function loadDevices() {
  try {
    const r = await fetch('/api/devices').then(x => x.json());
    devices = r.devices || [];
    if (devices.length) {
      renderDevices(); renderDestOptions(); renderInputs(); renderKeys(); renderHA(); renderRef();
      $('dev-msg').style.color = 'var(--muted)';
      $('dev-msg').textContent = busSummary('from the last scan');
    }
  } catch (e) {}
}

showTab(location.hash.slice(1) || 'control');
loadConfig().then(loadDevices); refreshStatus(); setInterval(refreshStatus, 3000);
</script>
</body>
</html>
"""


# -------------------------------------------------------------------- main
def main():
    bridge = Bridge()
    log(f"CEC bridge {VERSION} starting, config: {CONFIG_PATH}")
    bridge.cec.setup()
    bridge.start_mqtt()
    # Learn the bus in the background so the settings page can name devices
    # the moment it opens, without waiting on a scan.
    threading.Thread(target=bridge.scan_devices, daemon=True).start()
    threading.Thread(target=bridge.poll_loop, daemon=True).start()

    Handler.bridge = bridge
    port = bridge.cfg["web_port"]
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"Web page: http://{host_ip()}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop_mqtt()


if __name__ == "__main__":
    sys.exit(main())
