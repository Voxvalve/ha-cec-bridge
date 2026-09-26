# CEC Bridge for Home Assistant

Switches any HDMI-CEC TV between inputs from Home Assistant, using a Raspberry
Pi plugged into a spare HDMI port. No vendor API, no certificates, nothing
brand-specific: HDMI-CEC is a standard, so this works on Hisense, Sharp,
Samsung, LG, Sony and the rest alike.

## Install from your own computer (easiest)

From this folder on your Linux or macOS machine, with the Pi on the network:

    ./deploy.sh cec@192.168.1.11

It copies everything over SSH, installs it, and checks that it came up. Run it
with no arguments to be asked for the address and MQTT details instead.
Re-running it upgrades an existing install and keeps your settings.

    ./deploy.sh cec@192.168.1.11 --mqtt-host 192.168.1.10 --mqtt-user ha --detect-inputs
    ./deploy.sh --update
    ./deploy.sh cec@192.168.1.11 --uninstall
    ./deploy.sh --help

## Updating

    ./deploy.sh --update

Updates the software and nothing else. It asks no questions, changes no
settings, and reuses the Pi from last time so you need not retype the address.
It refuses to run alongside any flag that would change configuration, refuses
if no bridge is installed yet, and checks the settings file is byte-identical
afterwards, reporting what it found:

    ✓ settings untouched — 2 input(s), 3 key button(s), MQTT 192.168.1.10:1883

## Install on the Pi itself

    sudo ./install.sh

Then open the settings page shown at the end, e.g. http://<pi-ip>:8080/

## Configuration

Nothing is hardcoded and nothing is configured by editing this script.
Everything lives on the settings page:

- Inputs start empty. Press "Detect inputs" to scan the HDMI bus and add a row
  per device found, or add rows by hand.
- All 88 HDMI-CEC remote keys are listed, grouped and filterable. Tick any of
  them to also get a button for it in Home Assistant.
- The test buttons and the MQTT command reference are generated from your own
  configuration and update as you type.
- The device name in Home Assistant and the MQTT base topic are both settings.
- Each section has its own Save button, so no scrolling to the bottom. Ctrl+S
  works too, and a marker shows which sections hold unsaved edits.

## Other devices on the bus (AVR, Shield, players)

HDMI-CEC is one shared wire, not a chain. Every device sees every message, so
nothing is routed "through" an AVR — you address a device directly by its
logical address. The TV is always 0, an AVR or soundbar is 5, and players take
4, 8 or 11. Press Scan under "Devices on the bus" to see what is out there.

Add `@` and the address to any device command:

    cec_bridge/cmd/key           volume-up@5     volume to the AVR
    cec_bridge/cmd/key           select@8        OK on a Shield
    cec_bridge/cmd/tv_standby    @5              send only the AVR to standby
    cec_bridge/cmd/power_status  @8              ask the Shield if it is on

In the settings page, pick a device in "Send to" under Remote keys and the
keys you tick become Home Assistant buttons aimed at it. Devices are named
from your own input list wherever the HDMI address matches, so the picker and
the buttons read "Nvidia Shield TV" rather than "Playback 2". The same key can
have its own button per device.

If a scan finds nothing, the bridge falls back to polling each logical address
in turn, which works even when the TV declines to report its neighbours.

## What ends up in Home Assistant

The "In Home Assistant" section lists everything the bridge publishes, in one
place: the five fixed entities, one button per input, and one per remote key
you ticked. Untick anything you do not want and it is removed from Home
Assistant on save; the MQTT topic behind it keeps working either way. Entities
left over from an earlier configuration are cleared automatically on the next
connect, so nothing lingers as "unavailable".

### "TV on / TV standby" vs the "Power on/off function" keys

These are not the same thing, despite the similar names.

| | What it sends | Support |
| --- | --- | --- |
| TV on | Image View On, a dedicated CEC message | Among the best-supported commands |
| TV standby | Standby, a dedicated CEC message | Among the best-supported commands |
| `power-on-function` | Remote-key code 0x6D | Optional; many TVs never implement it |
| `power-off-function` | Remote-key code 0x6C | Optional; many TVs never implement it |

Use the TV on and TV standby buttons for power. The power-* keys are listed
under Remote keys for completeness, and the overview flags them if you tick
them.

## Which keys does my TV actually support?

There is no honest static answer: CEC's user-control codes are optional for
the manufacturer, so a list marking "supported" keys would be a guess. The
bridge answers it two ways instead.

- **Ask the device.** Under Remote keys, "What does the device say it
  supports?" sends CEC 2.0's Give Features and reports the RC profile, which
  is the only thing the standard lets a device advertise about remote keys.
  Older devices will not answer at all.
- **Measure it.** Press "test" beside any key. The result is remembered and
  marked in the list from then on:

      ✓  your TV acknowledged every press
      ~  acknowledged some of them (frames being dropped)
      ✗  never acknowledged — your TV ignores this key
         no mark: not tested yet

Over time the key list becomes a map of what your own TV honours, which is
worth more than any table in the spec.

## Send vs Test

**Send** presses a key once, the way you would use it. Unacknowledged presses
are resent automatically (the Retries setting).

**Test** is a diagnostic: it presses the key ten times *for real*, with the
automatic resending switched off so each press counts honestly, and records how
many the device acknowledged. For keys where ten presses would do something
drastic (power, eject, input select, channel changes, initial setup) it asks
first.

A ✓ means the device *received* the key. Whether it acted on it is only
visible on screen: a TV can acknowledge Back and still do nothing with it.

## When a key does nothing

CEC support is command-by-command and optional per manufacturer, so a TV may
implement volume and input switching perfectly and ignore Exit entirely. Under
"Remote keys" there is a test that sends one key repeatedly and counts how many
presses the TV acknowledges on the bus. That separates the two cases:

- All acknowledged, nothing happens: the TV received it and chose to do
  nothing. It does not implement that key; no setting will change it.
- None acknowledged: the frame is not being accepted at all. Check CEC is on
  and the TV is awake.
- Some acknowledged: frames are being dropped. Raise "Retries" in Settings,
  and look at cable length and how many CEC devices are on the bus.

Two settings help with flaky keys: "Key hold" (how long a key is held before
release, some TVs ignore a press released too quickly) and "Retries" (how many
times to resend a frame the TV did not acknowledge).

## Files

- deploy.sh           installs onto a Pi over SSH, from your own computer
- cec_bridge.py       the bridge (MQTT + CEC + settings web page)
- install.sh          installs packages, copies files, enables the service
- cec-bridge.service  systemd unit
- config.json         created on first run, next to cec_bridge.py

## Useful commands

    sudo systemctl restart cec-bridge
    journalctl -u cec-bridge -f
