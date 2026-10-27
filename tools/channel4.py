"""Channel 4: a nonstop lineup of public-domain films on the tube TV.

    python3 tools/channel4.py            # run the station (Ctrl-C to stop)
    python3 tools/channel4.py --fetch    # just download the whole lineup

Daytime plays the "day" list (cartoons, shorts) shuffled; from night_starts
to night_ends it plays the "night" list (creature features). Before each
show, a station card says what's up next. Clips come from archive.org once,
shrunk to 320 px wide, into cache/; the next one downloads while the current
one plays (CHANNEL4_CACHE overrides the folder). Each show is streamed with
play.py, so its flags pass through (e.g. --host=, --fps=).

Runs 24/7 as channel4.service on a Linux container: deploy/.
"""
import json, os, random, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.environ.get("CHANNEL4_CACHE", ROOT / "cache"))
LINEUP = json.loads((Path(__file__).parent / "lineup.json").read_text())
PLAY = [sys.executable, "-u", str(Path(__file__).parent / "play.py")]
passthrough = [a for a in sys.argv[1:] if a.startswith("--") and a != "--fetch"]

def font(size):
    """Bold sans: Helvetica on the Mac, DejaVu on the station's Debian box."""
    for path, index in (("/System/Library/Fonts/Helvetica.ttc", 1),
                        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0)):
        if os.path.exists(path):
            return ImageFont.truetype(path, size, index=index)
    return ImageFont.load_default(size)


def slug(show):
    return "".join(c if c.isalnum() else "-" for c in show["title"].lower()).strip("-")


def fetch(show):
    """Download and shrink a show into the cache, once."""
    out = CACHE / f"{slug(show)}.mp4"
    if out.exists():
        return out
    CACHE.mkdir(exist_ok=True)
    url = f"https://archive.org/download/{quote(show['item'])}/{quote(show['file'])}"
    tmp = out.with_suffix(".part.mp4")
    print(f"fetching {show['title']}", flush=True)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-reconnect", "1",
                    "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
                    "-reconnect_delay_max", "5", "-i", url, "-vf", "scale=320:-2", "-ac", "1",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac", str(tmp)],
                   check=True)
    tmp.rename(out)
    return out


def station_card(show):
    """A few seconds of 'Channel 4, up next' before each show."""
    out = CACHE / f"card-{slug(show)}.mp4"
    if out.exists():
        return out
    img = Image.new("L", (640, 480), 40)
    d = ImageDraw.Draw(img)
    big, mid, small = (font(s) for s in (150, 40, 30))
    d.ellipse([200, 40, 440, 280], fill=230, outline=0, width=10)
    d.text((320, 160), "4", font=big, fill=0, anchor="mm")
    d.text((320, 320), "CHANNEL 4", font=mid, fill=230, anchor="mm")
    d.text((320, 375), "UP NEXT", font=small, fill=170, anchor="mm")
    title = show["title"]
    size = 34 if len(title) < 32 else 26
    d.text((320, 425), f"{title} ({show['year']})", font=font(size),
           fill=230, anchor="mm")
    png = out.with_suffix(".png")
    img.save(png)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-loop", "1", "-i", str(png),
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "5", "-vf", "fps=24",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out)], check=True)
    return out


def block():
    hour = datetime.now().hour
    night = hour >= LINEUP["night_starts"] or hour < LINEUP["night_ends"]
    return "night" if night else "day"


def schedule():
    """Endless shows: each block's list shuffled, never the same show twice in a row."""
    last = None
    while True:
        shows = LINEUP[block()][:]
        random.shuffle(shows)
        for show in shows:
            if show is last:
                continue
            yield show
            last = show
            if block() != ("night" if show in LINEUP["night"] else "day"):
                break  # the block changed: start the other list


if "--fetch" in sys.argv:
    for show in LINEUP["day"] + LINEUP["night"]:
        fetch(show)
    sys.exit()

shows = schedule()
show = next(shows)
fetch(show)
while True:
    upcoming = next(shows)
    prefetch = threading.Thread(target=fetch, args=(upcoming,), daemon=True)
    prefetch.start()
    print(f"{datetime.now():%H:%M} now: {show['title']}", flush=True)
    subprocess.run([*PLAY, str(station_card(show)), *passthrough])
    subprocess.run([*PLAY, str(fetch(show)), *passthrough])
    prefetch.join()
    show = upcoming
