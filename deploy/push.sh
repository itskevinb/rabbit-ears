#!/bin/sh
# Deploy the station to a Linux box and restart it:
#   sh deploy/push.sh root@192.0.2.10
# (or put that target in deploy/host, which is gitignored, and leave it off).
# The box needs python3, python3-pil, ffmpeg and mDNS (avahi + libnss-mdns)
# to find tubetv.local.
set -e
cd "$(dirname "$0")/.."
HOST=${1:-$(cat deploy/host 2>/dev/null || true)}
[ -n "$HOST" ] || { echo "usage: sh deploy/push.sh user@host" >&2; exit 1; }
ssh "$HOST" mkdir -p /opt/tube-tv
rsync -a --delete tools/ "$HOST":/opt/tube-tv/tools/
scp -q deploy/channel4.service "$HOST":/etc/systemd/system/channel4.service
ssh "$HOST" 'systemctl daemon-reload && systemctl enable --now channel4 && systemctl restart channel4 && sleep 3 && systemctl --no-pager --lines=5 status channel4'
