#!/bin/sh
# Update the board's firmware over Wi-Fi (it needs a build with OTA_PASSWORD in
# rf-video/wifi_secrets.h, flashed once over USB):
#   sh tools/ota.sh
# Builds rf-video here, then pushes it from the station box (first line of
# deploy/host, or $1): that box is wired to the LAN and needs no firewall
# exception for the board to connect back. Channel 4 stops for the update
# (one stream at a time) and starts again after the board reboots.
set -e
cd "$(dirname "$0")/.."
HOST=${1:-$(sed -n '/^[^#[:space:]]/{s/[[:space:]]*$//;p;q;}' deploy/host 2>/dev/null | tr -d '\r')}
[ -n "$HOST" ] || { echo "usage: sh tools/ota.sh root@station-host" >&2; exit 1; }
PASS=$(sed -n 's/^#define OTA_PASSWORD "\(.*\)".*/\1/p' rf-video/wifi_secrets.h)
[ -n "$PASS" ] || { echo "no OTA_PASSWORD in rf-video/wifi_secrets.h" >&2; exit 1; }
ESPOTA=$(ls ~/Library/Arduino15/packages/esp32/hardware/esp32/*/tools/espota.py ~/.arduino15/packages/esp32/hardware/esp32/*/tools/espota.py 2>/dev/null | tail -1)
[ -n "$ESPOTA" ] || { echo "espota.py not found (esp32 Arduino core)" >&2; exit 1; }
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
arduino-cli compile --fqbn esp32:esp32:esp32 --output-dir "$BUILD" rf-video | tail -2
scp -q "$BUILD/rf-video.ino.bin" "$ESPOTA" "$HOST:/tmp/"
# The password goes to the box over stdin, so it isn't in the ssh command line;
# espota.py itself takes it as an argument there, for the few seconds it runs.
printf '%s\n' "$PASS" | ssh "$HOST" 'read -r PASS
  systemctl stop channel4 2>/dev/null; sleep 2
  python3 /tmp/espota.py -i "$(getent hosts tubetv.local | cut -d" " -f1)" -a "$PASS" -f /tmp/rf-video.ino.bin -r; rc=$?
  rm -f /tmp/rf-video.ino.bin
  sleep 10  # the board reboots and rejoins Wi-Fi
  systemctl start channel4 2>/dev/null
  exit $rc'
echo "updated; Channel 4 is back on"
