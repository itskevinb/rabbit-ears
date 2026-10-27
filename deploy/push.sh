#!/bin/sh
# Deploy the station to a Linux container and restart it.
set -e
cd "$(dirname "$0")/.."
ssh root@station.lan mkdir -p /opt/tube-tv
rsync -a --delete tools/ root@station.lan:/opt/tube-tv/tools/
scp -q deploy/channel4.service root@station.lan:/etc/systemd/system/channel4.service
ssh root@station.lan 'systemctl daemon-reload && systemctl enable --now channel4 && systemctl restart channel4 && sleep 3 && systemctl --no-pager --lines=5 status channel4'
