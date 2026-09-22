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

VERSION = "1.0"
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
    "osd_name": "HA CEC",
    "power_poll_seconds": 30,
    "web_port": 8080,
    "web_password": "",
    "allow_raw": True,
    # Empty on purpose: use "Detect inputs" on the settings page, or add rows
    # by hand. Nothing about this bridge is tied to particular devices.
    "inputs": [],
    # Remote keys to expose as buttons in Home Assistant. Every key still works
    # over MQTT whether or not it is listed here.
    "key_buttons": [],
}

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
    except (ValueError, KeyError, TypeError):
        errors.append("Ports and poll interval must be numbers.")
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
    unknown = [k for k in new.get("key_buttons", []) if k not in ALL_KEYS]
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

    def run(self, *args, timeout=15, quiet=False):
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
        m = re.search(r"^Physical Address\s*:\s*([0-9a-fA-F.]+)", out, re.M)
        self.phys_addr = m.group(1) if m else "unknown"
        # The real logical address is the indented line "  Logical Address : 4 (...)".
        # The unindented "Logical Addresses : 1" is only a count, so anchor on indentation.
        m = re.search(r"^[ \t]+Logical Address\s*:\s*(\d+)", out, re.M)
        self.logical_addr = m.group(1) if m else "unknown"

    def active_source(self, addr):
        return self.run("--to", "15", "--active-source", f"phys-addr={addr}")

    def tv_on(self):
        return self.run("--to", "0", "--image-view-on")

    def tv_standby(self):
        return self.run("--to", "0", "--standby")

    def standby_all(self):
        return self.run("--to", "15", "--standby")

    def key(self, name, dest="0"):
        ok, out = self.run("--to", dest, "--user-control-pressed", f"ui-cmd={name}")
        ok2, out2 = self.run("--to", dest, "--user-control-released")
        return ok and ok2, out + "\n" + out2

    def power_status(self, quiet=False):
        ok, out = self.run("--to", "0", "--give-device-power-status",
                           timeout=10, quiet=quiet)
        m = re.search(r"pwr-state:\s*([a-z-]+)", out)
        return (m.group(1) if (ok and m) else "unknown"), out

    def topology(self):
        return self.run("--show-topology", timeout=30)

    def discover(self):
        """Scan the bus and turn what is plugged in into ready-made input rows."""
        ok, out = self.topology()
        found, seen = [], set()
        # cec-ctl prints one indented block per device on the bus.
        for block in re.split(r"\n(?=\S)", out):
            addr = re.search(r"Physical Address\s*:\s*([0-9a-fA-F.]+)", block)
            if not addr:
                continue
            addr = addr.group(1)
            # 0.0.0.0 is the TV itself; f.f.f.f is unknown; skip our own port.
            if addr in ("0.0.0.0", "f.f.f.f", self.phys_addr) or addr in seen:
                continue
            seen.add(addr)
            name = re.search(r"OSD Name\s*:\s*'?([^'\n]+?)'?\s*$", block, re.M)
            kind = re.search(r"Primary Device Type\s*:\s*(\S+)", block)
            label = (name.group(1).strip() if name else "") or \
                    (f"{kind.group(1)} device" if kind else f"HDMI {addr[0]}")
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
        self.published = set()  # discovery topics we have published
        self.cfg_lock = threading.Lock()

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
        if cmd == "tv_on":
            return self.cec.tv_on()
        if cmd == "tv_standby":
            return self.cec.tv_standby()
        if cmd == "standby_all":
            return self.cec.standby_all()
        if cmd == "key":
            # "volume-up" goes to the TV; "volume-up@5" to another CEC device
            # (5 is the audio system), for setups with a soundbar or AVR.
            name, _, dest = payload.partition("@")
            dest = dest.strip() or "0"
            if not KEY_RE.match(name):
                return False, "Payload must be a key name like volume-up"
            if not (dest.isdigit() and 0 <= int(dest) <= 15):
                return False, "Destination after @ must be a logical address, 0-15"
            if name not in ALL_KEYS:
                return False, (f"Unknown key '{name}'. The settings page lists "
                               "every key CEC defines.")
            return self.cec.key(name, dest)
        if cmd == "power_status":
            state, out = self.cec.power_status()
            self.set_power(state)
            return True, f"TV power: {state}\n\n{out}"
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
        self.publish_discovery()
        client.publish(f"{base}/status", "online", retain=True)
        client.publish(f"{base}/state/tv_power", self.tv_power, retain=True)

    def on_disconnect(self, client, userdata, *args):
        if self.mqtt_connected:
            log("MQTT: disconnected, will retry")
        self.mqtt_connected = False
        if not self.mqtt_error:
            self.mqtt_error = "disconnected, retrying"

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace")
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
        if cfg["inputs"]:  # a select with no options is rejected by Home Assistant
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
            entities[f"{prefix}/button/{dev_id}/{key}/config"] = {
                **common, "name": name, "unique_id": f"{dev_id}_{key}",
                "command_topic": f"{base}/cmd/{key}", "icon": icon,
            }
        entities[f"{prefix}/sensor/{dev_id}/tv_power/config"] = {
            **common, "name": "TV power", "unique_id": f"{dev_id}_tv_power",
            "state_topic": f"{base}/state/tv_power", "icon": "mdi:power",
        }
        for key in cfg.get("key_buttons", []):
            if key not in ALL_KEYS:
                continue
            entities[f"{prefix}/button/{dev_id}/key_{slug(key)}/config"] = {
                **common, "name": key_label(key),
                "unique_id": f"{dev_id}_key_{slug(key)}",
                "command_topic": f"{base}/cmd/key", "payload_press": key,
                "icon": KEY_ICONS.get(key, "mdi:remote"),
            }

        # remove entities that no longer exist (e.g. a deleted input)
        for topic in self.published - set(entities):
            self.client.publish(topic, "", retain=True)
        for topic, conf in entities.items():
            self.client.publish(topic, json.dumps(conf), retain=True)
        self.published = set(entities)
        log(f"MQTT: published {len(entities)} Home Assistant entities")

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
            cfg["key_groups"] = [
                {"group": name,
                 "keys": [{"key": k, "label": key_label(k)} for k in keys]}
                for name, keys in KEY_GROUPS]
            self.json(cfg)
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
:root{--bg:#f4f5f7;--card:#fff;--text:#1d2129;--muted:#667085;--line:#e3e6ea;--accent:#2563eb;--ok:#16a34a;--bad:#dc2626;--code:#f1f3f5}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1e24;--text:#e6e8eb;--muted:#9aa3ae;--line:#2b313a;--accent:#60a5fa;--ok:#4ade80;--bad:#f87171;--code:#0d1014}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0 16px}
h2{font-size:17px;margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:4px}
input,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text);font:inherit}
input[type=checkbox]{width:auto}
button{padding:8px 14px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text);font:inherit;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button:hover{filter:brightness(1.08)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.stat{display:flex;flex-direction:column}
.stat b{font-size:16px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.hint{color:var(--muted);font-size:13px;margin:6px 0 0}
pre{background:var(--code);border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto;max-height:320px;font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;margin:10px 0 0}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}
code{font:12.5px ui-monospace,Menlo,Consolas,monospace;background:var(--code);padding:1px 5px;border-radius:4px;word-break:break-all}
.inputs td input{min-width:90px}
.msg{margin-left:8px;font-size:14px}
.tablewrap{overflow-x:auto}
details{border:1px solid var(--line);border-radius:8px;margin-bottom:8px;background:var(--bg)}
details[open]{background:transparent}
summary{cursor:pointer;padding:9px 12px;font-weight:600;font-size:14px;list-style:none;display:flex;align-items:center;gap:8px}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸";color:var(--muted);transition:transform .15s}
details[open] summary::before{transform:rotate(90deg)}
summary .count{color:var(--muted);font-weight:400;font-size:13px}
details .body{padding:0 12px 12px}
.keygrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:4px 12px}
.keygrid label{display:flex;align-items:baseline;gap:7px;margin:0;padding:3px 0;color:var(--text);font-size:13.5px;cursor:pointer}
.keygrid label:hover{color:var(--accent)}
.keygrid code{font-size:11.5px;color:var(--muted);background:none;padding:0}
.keygrid input{margin:0}
</style>
</head>
<body>
<main>
<h1>CEC Bridge</h1>

<section class="card">
  <h2>Status</h2>
  <div class="grid">
    <div class="stat"><span class="hint">MQTT</span><b id="s-mqtt">…</b></div>
    <div class="stat"><span class="hint">Pi HDMI address</span><b id="s-phys">…</b></div>
    <div class="stat"><span class="hint">CEC logical address</span><b id="s-log">…</b></div>
    <div class="stat"><span class="hint">TV power</span><b id="s-power">…</b></div>
  </div>
  <p class="hint" id="s-warn"></p>
</section>

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

<section class="card">
  <h2>Inputs</h2>
  <p class="hint" style="margin-top:0">One row per TV input you want to switch to. Address = HDMI port: HDMI 1 is 1.0.0.0, HDMI 2 is 2.0.0.0 and so on. Everything else on this page — the test buttons above and the command reference below — follows these rows as you edit them.</p>
  <div class="tablewrap"><table class="inputs"><thead><tr><th>ID (used in MQTT)</th><th>Name (shown in HA)</th><th>Address</th><th></th></tr></thead><tbody id="inputs"></tbody></table></div>
  <div class="row" style="margin-top:10px">
    <button class="primary" onclick="detect()">Detect inputs</button>
    <button onclick="addInput()">+ Add input</button>
    <span class="msg" id="detect-msg"></span>
  </div>
  <p class="hint">Detect scans the HDMI bus and adds a row for every device it finds, named as the device names itself. Devices that are switched off do not answer, so switch on what you want found, or add it by hand.</p>
</section>

<section class="card">
  <h2>Remote keys</h2>
  <p class="hint" style="margin-top:0">Every key HDMI-CEC defines, named as <code>cec-ctl</code> accepts them. Send any of them to <code id="key-topic">cec_bridge/cmd/key</code>. Tick a key to also give it a button in Home Assistant. Keys go to the TV unless you add <code>@5</code> for the audio system, e.g. <code>volume-up@5</code>. TVs implement only part of this list, so try a key before relying on it.</p>
  <div class="row" style="margin-bottom:10px">
    <input id="key-filter" placeholder="Filter keys…" style="max-width:260px" oninput="renderKeys()">
    <span class="hint" id="key-count" style="margin:0"></span>
  </div>
  <div id="key-groups"></div>
</section>

<section class="card">
  <h2>Settings</h2>
  <div class="grid">
    <div><label>MQTT host (IP of Home Assistant)</label><input id="mqtt_host"></div>
    <div><label>MQTT port</label><input id="mqtt_port" type="number"></div>
    <div><label>MQTT username</label><input id="mqtt_user" autocomplete="off"></div>
    <div><label>MQTT password</label><input id="mqtt_pass" type="password" autocomplete="new-password"></div>
    <div><label>Base topic</label><input id="base_topic"></div>
    <div><label>Discovery prefix</label><input id="discovery_prefix"></div>
    <div><label>Device name in HA</label><input id="device_name"></div>
    <div><label>CEC device</label><input id="cec_device"></div>
    <div><label>OSD name (max 14 chars)</label><input id="osd_name" maxlength="14"></div>
    <div><label>TV power poll, seconds (0 = off)</label><input id="power_poll_seconds" type="number"></div>
    <div><label>Web page port</label><input id="web_port" type="number"></div>
    <div><label>Web page password (user: any)</label><input id="web_password" type="password" autocomplete="new-password"></div>
  </div>
  <div class="row" style="margin-top:12px">
    <label style="display:flex;gap:6px;align-items:center;margin:0"><input type="checkbox" id="allow_raw"> Allow raw cec-ctl commands</label>
  </div>
  <p class="hint">Password fields left empty keep the current password.
    <a href="#" onclick="clearPw('mqtt');return false">Clear MQTT password</a> ·
    <a href="#" onclick="clearPw('web');return false">Remove web password</a></p>
  <div class="row" style="margin-top:12px"><button class="primary" onclick="save()">Save settings</button><span class="msg" id="save-msg"></span></div>
</section>

<section class="card">
  <h2>MQTT command reference</h2>
  <p class="hint" style="margin-top:0">Publish the payload to the topic, e.g. from an HA script with <code>mqtt.publish</code>. Every topic follows your base topic and your inputs, so this is always the reference for <i>your</i> setup.</p>
  <div id="ref"></div>
</section>

<section class="card">
  <h2>Log</h2>
  <pre id="log" style="max-height:260px"></pre>
</section>
</main>

<script>
let cfg = {}, inputs = [], keyButtons = new Set(), clearMqtt = false, clearWeb = false;
const $ = id => document.getElementById(id);
const v = id => $(id).value;
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, body) {
  const r = await fetch(path, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
  return r.json();
}

async function loadConfig() {
  cfg = await api('/api/config');
  for (const k of ['mqtt_host','mqtt_port','mqtt_user','base_topic','discovery_prefix','device_name','cec_device','osd_name','power_poll_seconds','web_port']) $(k).value = cfg[k];
  $('allow_raw').checked = cfg.allow_raw;
  $('mqtt_pass').placeholder = cfg.has_mqtt_pass ? '(unchanged)' : '(none)';
  $('web_password').placeholder = cfg.has_web_password ? '(unchanged)' : '(none - page is open)';
  $('t-key').innerHTML = cfg.key_groups.map(g =>
    `<optgroup label="${esc(g.group)}">` +
    g.keys.map(k => `<option value="${esc(k.key)}">${esc(k.label)} — ${esc(k.key)}</option>`).join('') +
    `</optgroup>`).join('');
  inputs = cfg.inputs.map(i => ({...i, _autoId: false}));
  keyButtons = new Set(cfg.key_buttons || []);
  renderInputs(); renderButtons(); renderKeys(); renderRef();
}

// The reference and buttons follow these two fields live as well.
$('base_topic').addEventListener('input', renderRef);
$('allow_raw').addEventListener('change', renderRef);

// Every input row edit re-renders the test buttons and the command reference,
// so the page always shows YOUR inputs, not a fixed list.
function onInputEdit(n, field, value) {
  inputs[n][field] = value;
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

function renderInputs() {
  $('inputs').innerHTML = inputs.map((i, n) => `<tr>
    <td><input class="id-cell" value="${esc(i.id)}" oninput="inputs[${n}]._autoId=false;onInputEdit(${n},'id',this.value)"></td>
    <td><input value="${esc(i.name)}" oninput="onInputEdit(${n},'name',this.value)"></td>
    <td><input value="${esc(i.address)}" oninput="onInputEdit(${n},'address',this.value)" placeholder="3.0.0.0"></td>
    <td><button onclick="inputs.splice(${n},1);renderInputs();renderButtons();renderRef()">Remove</button></td></tr>`).join('')
    || `<tr><td colspan="4" class="hint">No inputs yet — press “Detect inputs”, or add one by hand.</td></tr>`;
}
function addInput() { inputs.push({id:'', name:'', address:'', _autoId:true}); renderInputs(); renderButtons(); renderRef(); }

async function detect() {
  $('detect-msg').style.color = 'var(--muted)';
  $('detect-msg').textContent = 'Scanning the HDMI bus…';
  try {
    const r = await api('/api/detect', {existing_ids: inputs.map(i => i.id)});
    const fresh = (r.found || []).filter(d => !inputs.some(i => i.address === d.address));
    // Detected rows are not saved yet, so tidying the name still tidies the id.
    // Once saved, the id stays put, because automations may already use it.
    fresh.forEach(d => inputs.push({id:d.id, name:d.name, address:d.address, _autoId:true}));
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

// ---- Remote keys: every CEC key, grouped, filterable, tickable for HA
function toggleKey(key, on) {
  on ? keyButtons.add(key) : keyButtons.delete(key);
  renderKeys(); renderRef();
}

function renderKeys() {
  const q = ($('key-filter').value || '').trim().toLowerCase();
  const match = k => !q || k.key.includes(q) || k.label.toLowerCase().includes(q);
  let shown = 0;
  $('key-groups').innerHTML = cfg.key_groups.map(g => {
    const keys = g.keys.filter(match);
    if (!keys.length) return '';
    shown += keys.length;
    const ticked = keys.filter(k => keyButtons.has(k.key)).length;
    return `<details${q || ticked ? ' open' : ''}>
      <summary>${esc(g.group)} <span class="count">${keys.length} key${keys.length > 1 ? 's' : ''}${ticked ? ` · ${ticked} in HA` : ''}</span></summary>
      <div class="body"><div class="keygrid">${keys.map(k => `
        <label title="Send ${esc(k.key)}">
          <input type="checkbox" ${keyButtons.has(k.key) ? 'checked' : ''} onchange="toggleKey('${esc(k.key)}',this.checked)">
          <span>${esc(k.label)} <code>${esc(k.key)}</code></span>
        </label>`).join('')}</div></div></details>`;
  }).join('') || '<p class="hint">No key matches that filter.</p>';
  $('key-count').textContent = `${shown} of ${cfg.key_groups.reduce((n, g) => n + g.keys.length, 0)} keys` +
    (keyButtons.size ? ` · ${keyButtons.size} exposed in Home Assistant` : '');
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
    row(`${b}/cmd/tv_on`, 'anything', 'Wake the TV (Image View On).'),
    row(`${b}/cmd/tv_standby`, 'anything', 'Put the TV in standby.'),
    row(`${b}/cmd/standby_all`, 'anything', 'Broadcast standby to every CEC device, consoles included.'),
    row(`${b}/cmd/power_status`, 'anything', `Ask the TV its power state; the answer lands on <code>${esc(b)}/state/tv_power</code>.`),
  ];

  const keys = [row(`${b}/cmd/key`, '<code>volume-down</code>',
      `Send any of the ${cfg.key_groups.reduce((n, g) => n + g.keys.length, 0)} CEC keys to the TV. The full list is under “Remote keys” above.`),
    row(`${b}/cmd/key`, '<code>volume-up@5</code>', 'Send a key to another CEC device instead of the TV — 5 is the audio system, for a soundbar or AVR.')];
  [...keyButtons].forEach(k => keys.push(row(`${b}/cmd/key`, `<code>${esc(k)}</code>`,
      'Also has its own button in Home Assistant, because you ticked it above.')));

  const diag = [
    row(`${b}/cmd/topology`, 'anything', `Rescan the HDMI bus; the listing lands on <code>${esc(b)}/topology</code>.`),
    row(`${b}/cmd/reconfigure`, 'anything', 'Re-register the Pi on the CEC bus, after a TV power cut for instance.'),
  ];
  if ($('allow_raw').checked) diag.push(row(`${b}/cmd/raw`, '<code>--to 0 --give-osd-name</code>', 'Run cec-ctl with these arguments (advanced).'));

  const state = [
    row(`${b}/status`, '<i>published</i>', '<code>online</code> or <code>offline</code>. Home Assistant uses this for availability.'),
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

async function save() {
  const body = {inputs: inputs.map(i => ({id:(i.id||'').trim(), name:(i.name||'').trim(), address:(i.address||'').trim()})),
                key_buttons: [...keyButtons]};
  for (const k of ['mqtt_host','mqtt_port','mqtt_user','mqtt_pass','base_topic','discovery_prefix','device_name','cec_device','osd_name','power_poll_seconds','web_port','web_password']) body[k] = v(k);
  body.allow_raw = $('allow_raw').checked;
  body.clear_mqtt_pass = clearMqtt; body.clear_web_password = clearWeb;
  const r = await api('/api/config', body);
  $('save-msg').style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
  $('save-msg').textContent = r.ok ? r.message : r.errors.join(' ');
  if (r.ok) { clearMqtt = clearWeb = false; $('mqtt_pass').value = ''; $('web_password').value = ''; await loadConfig(); }
}

async function refreshStatus() {
  try {
    const s = await api('/api/status');
    $('s-mqtt').innerHTML = `<span class="dot" style="background:${s.mqtt_connected ? 'var(--ok)' : 'var(--bad)'}"></span>` +
      (s.mqtt_connected ? 'Connected' : esc(s.mqtt_error || 'Not connected'));
    $('s-phys').textContent = s.phys_addr;
    $('s-log').textContent = s.logical_addr;
    $('s-power').textContent = s.tv_power;
    $('s-warn').textContent = (s.phys_addr === 'f.f.f.f' || s.phys_addr === 'unknown')
      ? 'The Pi cannot see the TV on HDMI (address f.f.f.f). See "Troubleshooting" in the guide.' : '';
    const log = $('log'), atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 5;
    log.textContent = s.log.join('\n');
    if (atBottom) log.scrollTop = log.scrollHeight;
  } catch (e) {}
}

loadConfig(); refreshStatus(); setInterval(refreshStatus, 3000);
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
