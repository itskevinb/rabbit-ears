#!/bin/sh
# Let Claude see and hear the TV through the Mac: a still, a 4-frame strip over
# 2 s, and the loudness of 3 s of microphone audio. Output in $1 (default /tmp/tvlook).
D=${1:-/tmp/tvlook}; mkdir -p "$D"
ffmpeg -hide_banner -loglevel error -y -f avfoundation -framerate 30 -video_size 1280x720 \
  -i "0:none" -t 3 "$D/clip.mp4"
ffmpeg -hide_banner -loglevel error -y -ss 1 -i "$D/clip.mp4" -frames:v 1 "$D/still.jpg"
ffmpeg -hide_banner -loglevel error -y -ss 0.8 -i "$D/clip.mp4" \
  -vf "fps=2,scale=640:-1,tile=2x2" -frames:v 1 "$D/strip.jpg"
ffmpeg -hide_banner -loglevel error -y -f avfoundation -i "none:MacBook Pro Microphone" -t 3 "$D/sound.wav"
ffmpeg -hide_banner -i "$D/sound.wav" -af volumedetect -f null - 2>&1 | grep -E "mean_volume|max_volume"
