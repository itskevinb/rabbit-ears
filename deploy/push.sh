#!/bin/sh
# Deploy the station to a Linux box and restart it, as root (it writes to
# /opt and /etc/systemd/system):
#   sh deploy/push.sh root@192.0.2.10
# or put that target on the first line of deploy/host (gitignored) and leave
# it off. The box needs python3, python3-pil, ffmpeg, fonts-dejavu-core, and
# mDNS (avahi-daemon + libnss-mdns) to find tubetv.local; the night block
# follows its time zone (timedatectl set-timezone ...).
# Only committed files go: tools/ as of HEAD.
set -e
cd "$(dirname "$0")/.."
HOST=${1:-$(sed -n '/^[^#[:space:]]/{s/[[:space:]]*$//;p;q;}' deploy/host 2>/dev/null | tr -d '\r')}
[ -n "$HOST" ] || { echo "usage: sh deploy/push.sh root@host" >&2; exit 1; }
git diff --quiet HEAD -- tools deploy || echo "note: uncommitted changes in tools/ or deploy/ are not deployed" >&2
git archive --format=tar HEAD tools | ssh "$HOST" 'rm -rf /opt/tube-tv/tools && mkdir -p /opt/tube-tv && tar -x -C /opt/tube-tv'
git show HEAD:deploy/channel4.service | ssh "$HOST" 'cat > /etc/systemd/system/channel4.service'
ssh "$HOST" 'systemctl daemon-reload && systemctl enable --now channel4 && systemctl restart channel4 && sleep 3 && systemctl --no-pager --lines=5 status channel4'
