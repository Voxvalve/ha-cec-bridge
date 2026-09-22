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
    ./deploy.sh cec@192.168.1.11 --uninstall
    ./deploy.sh --help

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

## Files

- deploy.sh           installs onto a Pi over SSH, from your own computer
- cec_bridge.py       the bridge (MQTT + CEC + settings web page)
- install.sh          installs packages, copies files, enables the service
- cec-bridge.service  systemd unit
- config.json         created on first run, next to cec_bridge.py

## Useful commands

    sudo systemctl restart cec-bridge
    journalctl -u cec-bridge -f
