"""Channel 4: the tube TV's station, with its own web remote.

    python3 tools/station.py [--host=tubetv.local] [--port=80]

Plays the lineup nonstop to the ESP32 transmitter over one long-lived
connection: the "day" list shuffled, the "night" list (creature features)
from night_starts to night_ends, an "up next" card before each show.
Clips come from archive.org once, shrunk to 320 px, into the cache.

The web page (tools/web/index.html, LAN only, no sign-in) shows what's on
with a live thumbnail and has the remote: skip, play a show now, stand by,
test pattern, sign off, mute; the lineup editor with an archive.org search; and
the picture/sound settings. "Play next" queues shows ahead of the lineup. Lineup and settings live in the cache folder
(CHANNEL4_CACHE, default ./cache), seeded from tools/lineup.json.

Runs 24/7 as channel4.service on a Linux container: deploy/.
"""
import io, json, os, random, subprocess, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from tvlink import H, RATE, SILENCE, SOUND_METHODS, W, Source, TV, audio_share, packet, setup_commands

HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("CHANNEL4_CACHE", HERE.parent / "cache"))
CACHE.mkdir(parents=True, exist_ok=True)
flags = {a.split("=", 1)[0]: (a.split("=", 1) + [""])[1] for a in sys.argv[1:] if a.startswith("--")}

DEFAULT_SETTINGS = {"fps": 24, "sound": "w4", "dither": True, "brightness": 0.0, "contrast": 1.0, "muted": False}


def load(name, default):
    path = CACHE / name
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(name, data):
    tmp = CACHE / (name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(CACHE / name)


def slug(show):
    return "".join(c if c.isalnum() else "-" for c in show["title"].lower()).strip("-")


def font(size):
    """Bold sans: Helvetica on the Mac, DejaVu on the station's Debian box."""
    for path, index in (("/System/Library/Fonts/Helvetica.ttc", 1),
                        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0)):
        if os.path.exists(path):
            return ImageFont.truetype(path, size, index=index)
    return ImageFont.load_default(size)


# ---- cards, drawn straight to a 256x192 grey frame ---------------------------

def channel_card(*lines):
    img = Image.new("L", (W, H), 40)
    d = ImageDraw.Draw(img)
    d.ellipse([88, 10, 168, 90], fill=230, outline=0, width=4)
    d.text((128, 50), "4", font=font(56), fill=0, anchor="mm")
    d.text((128, 108), "CHANNEL 4", font=font(18), fill=230, anchor="mm")
    y = 136
    for text, size, shade in lines:
        while font(size).getlength(text) > W - 16 and size > 9:
            size -= 1
        d.text((128, y), text, font=font(size), fill=shade, anchor="mm")
        y += size + 8
    return img.tobytes()


def test_pattern():
    """For tuning: grey steps, a circle with crosshairs, fine and coarse bars."""
    img = Image.new("L", (W, H), 128)
    d = ImageDraw.Draw(img)
    for i in range(8):
        d.rectangle([i * 32, 0, i * 32 + 31, 30], fill=round(i * 255 / 7))
    for x in range(0, W, 16):
        d.line([x, 32, x, H - 32], fill=90)
    for y in range(32, H - 31, 16):
        d.line([0, y, W, y], fill=90)
    d.ellipse([68, 36, 188, 156], fill=230, outline=0, width=4)
    d.line([128, 36, 128, 156], fill=0, width=2)
    d.line([68, 96, 188, 96], fill=0, width=2)
    d.text((128, 76), "CHANNEL 4", font=font(16), fill=0, anchor="mm")
    d.text((128, 118), "TEST PATTERN", font=font(12), fill=0, anchor="mm")
    for i, step in enumerate((1, 2, 4)):  # resolution wedges, finest on the left
        x0 = 8 + i * 84
        for x in range(x0, x0 + 72, step * 2):
            d.rectangle([x, H - 26, x + step - 1, H - 4], fill=0)
            d.rectangle([x + step, H - 26, x + 2 * step - 1, H - 4], fill=255)
    return img.tobytes()


# ---- archive.org ---------------------------------------------------------------

def archive_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "channel4-tube-tv"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def archive_search(q):
    query = urllib.parse.urlencode({"q": f"({q}) AND mediatype:movies", "fl[]": ["identifier", "title", "year", "downloads"],
                                    "sort[]": "downloads desc", "rows": 20, "output": "json"}, doseq=True)
    docs = archive_json(f"https://archive.org/advancedsearch.php?{query}")["response"]["docs"]
    return [{"item": d["identifier"], "title": d.get("title", d["identifier"]), "year": d.get("year"),
             "downloads": d.get("downloads", 0)} for d in docs]


def archive_files(item):
    meta = archive_json(f"https://archive.org/metadata/{urllib.parse.quote(item)}")
    files = [{"file": f["name"], "size": int(f.get("size", 0)), "length": float(f.get("length", 0) or 0)}
             for f in meta.get("files", []) if f["name"].lower().endswith((".mp4", ".m4v", ".mpeg", ".ogv", ".avi"))]
    return {"title": meta.get("metadata", {}).get("title", item), "year": meta.get("metadata", {}).get("year"),
            "files": sorted(files, key=lambda f: f["size"])}


# ---- the station ----------------------------------------------------------------

class Interrupted(Exception):
    pass


class Station:
    def __init__(self):
        self.lock = threading.Lock()
        self.lineup = load("lineup.json", None) or json.loads((HERE / "lineup.json").read_text())
        self.settings = {**DEFAULT_SETTINGS, **load("settings.json", {})}
        self.tv = TV(flags.get("--host") or "tubetv.local", self.setup())
        self.mode = "lineup"            # lineup | standby | testcard | off
        self.now = self.upcoming = None  # shows
        self.override = None             # "play now" pick
        self.queue = load("queue.json", [])  # "play next" picks, in order, ahead of the lineup
        self.skip = self.reopen = False
        self.pending_setup = False       # new sound/dither commands to send
        self.frame = channel_card()      # last grey frame sent, for the thumbnail
        self.pos = self.length = 0.0     # seconds into / length of the current show
        self.fetching = set()
        self.durations = load("durations.json", {})
        self.bags = {}                   # block -> shuffled shows not played yet
        self.changed = threading.Event()

    def setup(self):
        return setup_commands(self.settings["sound"], self.settings["dither"])

    # -- lineup ---------------------------------------------------------------
    def block(self):
        hour = datetime.now().hour
        night = hour >= self.lineup["night_starts"] or hour < self.lineup["night_ends"]
        return "night" if night else "day"

    def pick(self, avoid=None):
        """Next show from the current block: a shuffled pass through the list,
        never the same show twice in a row."""
        block = self.block()
        shows = self.lineup.get(block) or self.lineup.get("day") or self.lineup.get("night")
        if not shows:
            return None
        bag = [s for s in self.bags.get(block, []) if s in shows]
        if not bag:
            bag = shows[:]
            random.shuffle(bag)
        for i, show in enumerate(bag):
            if avoid is None or slug(show) != slug(avoid) or len(shows) == 1:
                bag.pop(i)
                self.bags[block] = bag
                return show
        return bag[0]

    def path(self, show):
        return CACHE / f"{slug(show)}.mp4"

    def fetch(self, show):
        """Download and shrink a show into the cache, once."""
        out = self.path(show)
        if out.exists():
            return out
        key = slug(show)
        with self.lock:
            if key in self.fetching:
                busy = True
            else:
                busy = False
                self.fetching.add(key)
        if busy:
            while key in self.fetching:
                time.sleep(1)
            return out if out.exists() else None
        try:
            url = f"https://archive.org/download/{urllib.parse.quote(show['item'])}/{urllib.parse.quote(show['file'])}"
            tmp = out.with_suffix(".part.mp4")
            for attempt, wait in enumerate((0, 5, 20)):  # archive.org throws the odd 5XX
                time.sleep(wait)
                print(f"fetching {show['title']}" + (f" (try {attempt + 1})" if attempt else ""), flush=True)
                r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-reconnect", "1",
                                    "-reconnect_streamed", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
                                    "-i", url, "-vf", "scale=320:-2", "-ac", "1", "-c:v", "libx264", "-preset", "veryfast",
                                    "-crf", "26", "-c:a", "aac", str(tmp)])
                if r.returncode == 0:
                    break
                tmp.unlink(missing_ok=True)
            else:
                print(f"fetch failed: {show['title']}", flush=True)
                return None
            tmp.rename(out)
            self.duration(show)
            return out
        finally:
            with self.lock:
                self.fetching.discard(key)

    def duration(self, show):
        key = slug(show)
        if key not in self.durations and self.path(show).exists():
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(self.path(show))], capture_output=True, text=True)
            try:
                self.durations[key] = float(r.stdout.strip())
                save("durations.json", self.durations)
            except ValueError:
                return 0.0
        return self.durations.get(key, 0.0)

    # -- sending ----------------------------------------------------------------
    def interrupted(self):
        return self.skip or self.override is not None or self.mode != "lineup"

    def send(self, gray, mulaw):
        if self.pending_setup:  # settings go down the same connection, from this thread
            self.pending_setup = False
            self.tv.send(self.tv.setup)
        if self.settings["muted"]:  # silence, not no carrier: the set stays quiet instead of buzzing
            mulaw = bytes([SILENCE]) * len(mulaw)
        self.tv.send(packet(gray, mulaw))
        self.frame = gray

    def hold(self, gray, seconds=None, until=None):
        """Send a still frame (with silence) for a while, or until `until()` says stop."""
        fps = self.settings["fps"]
        n = 0
        while (seconds is None or n < seconds * fps) and not (until and until()):
            self.send(gray, bytes([SILENCE]) * audio_share(n, fps))
            n += 1

    def play(self, show, path):
        self.length = self.duration(show)
        start = 0.0
        while True:
            fps, s = self.settings["fps"], self.settings
            source = Source.file(path, start, fps=fps, brightness=s["brightness"], contrast=s["contrast"])
            n = 0
            try:
                for gray, mulaw in source:
                    self.send(gray, mulaw)
                    n += 1
                    self.pos = start + n / fps
                    if self.reopen:  # picture settings changed: pick up where we are
                        break
                    if self.interrupted():
                        raise Interrupted
                else:
                    return
            finally:
                source.close()
            self.reopen = False
            start = self.pos

    def run(self):
        current = None
        while True:
            try:
                if self.mode == "standby":
                    self.now = None
                    self.hold(channel_card(("PLEASE STAND BY", 20, 230)), until=lambda: self.mode != "standby")
                    continue
                if self.mode == "testcard":
                    self.now = None
                    self.hold(test_pattern(), until=lambda: self.mode != "testcard")
                    continue
                if self.mode == "off":
                    self.now = None
                    self.changed.clear()
                    self.hold(channel_card(("GOOD NIGHT", 22, 230), ("Channel 4 is off the air", 12, 170)),
                              seconds=6, until=lambda: self.mode != "off")
                    self.hold(bytes(W * H), seconds=1)  # then black and silence
                    while self.mode == "off":
                        self.changed.wait(1)
                    continue
                with self.lock:
                    if self.override is not None:
                        current, self.override = self.override, None
                    self.skip = False
                if current is None:
                    with self.lock:
                        if self.queue:
                            current = self.queue.pop(0)
                            save("queue.json", self.queue)
                current = current or self.pick()
                if current is None:
                    self.hold(channel_card(("NO SHOWS IN THE LINEUP", 14, 230)), seconds=5)
                    continue
                self.now, self.pos, self.length = current, 0.0, self.duration(current)
                self.upcoming = self.queue[0] if self.queue else self.pick(avoid=current)
                if self.upcoming:
                    threading.Thread(target=self.fetch, args=(self.upcoming,), daemon=True).start()
                if not self.path(current).exists():  # first time: stand by while it downloads
                    done = threading.Event()
                    threading.Thread(target=lambda: (self.fetch(current), done.set()), daemon=True).start()
                    self.hold(channel_card(("PLEASE STAND BY", 20, 230), (current["title"], 14, 170)),
                              until=lambda: done.is_set() or self.interrupted())
                    if self.interrupted():
                        raise Interrupted
                # Never download from this thread: the TV gets no frames while it
                # waits (a slow retry once left the screen frozen for minutes).
                path = self.path(current) if self.path(current).exists() else None
                if path is None:  # couldn't get it: move on rather than hang
                    current = None
                    continue
                print(f"{datetime.now():%H:%M} now: {current['title']}", flush=True)
                self.hold(channel_card(("UP NEXT", 14, 170), (f"{current['title']} ({current.get('year', '')})", 16, 230)),
                          seconds=5, until=self.interrupted)
                if self.interrupted():
                    raise Interrupted
                self.play(current, path)
                current = None if self.queue else self.upcoming  # the queue goes first
            except Interrupted:
                # Skip goes to the show the page says is up next; "play now" and
                # mode changes are picked up at the top of the loop.
                skipping = self.skip and self.override is None and self.mode == "lineup" and not self.queue
                current = self.upcoming if skipping else None
            except Exception as e:  # keep the station on the air whatever happens
                print(f"station error: {e!r}", flush=True)
                current = None
                time.sleep(2)

    # -- remote -------------------------------------------------------------------
    def state(self):
        show = lambda s: s and {**s, "slug": slug(s), "cached": self.path(s).exists()}
        lineup = {k: [show(s) for s in self.lineup.get(k, [])] for k in ("day", "night")}
        upcoming = self.queue[0] if self.queue else self.upcoming
        return {"mode": self.mode, "block": self.block(), "now": show(self.now), "upcoming": show(upcoming),
                "queue": [show(q) for q in self.queue],
                "pos": round(self.pos), "length": round(self.length), "settings": self.settings,
                "lineup": {**self.lineup, **lineup}, "fetching": sorted(self.fetching),
                "clock": datetime.now().strftime("%H:%M")}

    def find(self, key):
        for s in self.lineup.get("day", []) + self.lineup.get("night", []):
            if slug(s) == key:
                return s
        return None

    def set_mode(self, mode):
        self.mode = mode
        self.changed.set()

    def play_now(self, key):
        show = self.find(key)
        if show:
            self.override = show
            self.set_mode("lineup")
        return show is not None

    def set_lineup(self, lineup):
        clean = {"day": [], "night": [], "night_starts": int(lineup["night_starts"]) % 24,
                 "night_ends": int(lineup["night_ends"]) % 24}
        for block in ("day", "night"):
            for s in lineup.get(block, []):
                clean[block].append({"title": str(s["title"]), "year": s.get("year") or "",
                                     "item": str(s["item"]), "file": str(s["file"])})
        self.lineup = clean
        self.bags = {}
        save("lineup.json", clean)

    def set_settings(self, new):
        s = {**self.settings}
        s["fps"] = max(8, min(30, int(new.get("fps", s["fps"]))))
        s["sound"] = new.get("sound", s["sound"]) if new.get("sound") in SOUND_METHODS else s["sound"]
        s["dither"] = bool(new.get("dither", s["dither"]))
        s["brightness"] = max(-0.5, min(0.5, float(new.get("brightness", s["brightness"]))))
        s["contrast"] = max(0.3, min(3.0, float(new.get("contrast", s["contrast"]))))
        picture = any(s[k] != self.settings[k] for k in ("fps", "brightness", "contrast"))
        board = any(s[k] != self.settings[k] for k in ("sound", "dither"))
        self.settings = s
        save("settings.json", s)
        if board:
            self.tv.setup = self.setup()  # the engine thread sends it with the next frame
            self.pending_setup = True
        if picture:
            self.reopen = True

    def queue_add(self, keys):
        """Play these next, in order, after anything already queued."""
        shows = [s for s in (self.find(k) for k in keys) if s]
        with self.lock:
            self.queue += shows
            save("queue.json", self.queue)
        if shows:  # start downloading the first one now
            threading.Thread(target=self.fetch, args=(self.queue[0],), daemon=True).start()
        return len(shows)

    def queue_remove(self, index):
        with self.lock:
            if 0 <= index < len(self.queue):
                self.queue.pop(index)
            save("queue.json", self.queue)

    def queue_clear(self):
        with self.lock:
            self.queue = []
            save("queue.json", self.queue)

    def set_muted(self, muted):
        self.settings = {**self.settings, "muted": bool(muted)}
        save("settings.json", self.settings)

    def remove_cached(self, key):
        show = self.find(key)
        if show:
            self.path(show).unlink(missing_ok=True)
            self.durations.pop(key, None)
            save("durations.json", self.durations)


station = Station()


class Remote(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        try:
            if url.path in ("/", "/index.html"):
                return self.reply(200, (HERE / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self.reply(200, station.state())
            if url.path == "/api/frame.png":
                img = Image.frombytes("L", (W, H), station.frame).point(lambda v: (v >> 4) * 17)  # what the TV gets
                buf = io.BytesIO()
                img.resize((W * 2, H * 2), Image.NEAREST).save(buf, "PNG")
                return self.reply(200, buf.getvalue(), "image/png")
            if url.path == "/api/search":
                return self.reply(200, archive_search(q.get("q", "")))
            if url.path == "/api/files":
                return self.reply(200, archive_files(q["item"]))
            self.reply(404, {"error": "not found"})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            b = self.body()
            if path == "/api/skip":
                station.skip = True
            elif path == "/api/mode" and b.get("mode") in ("lineup", "standby", "testcard", "off"):
                station.set_mode(b["mode"])
            elif path == "/api/play":
                if not station.play_now(b.get("slug", "")):
                    return self.reply(404, {"error": "no such show"})
            elif path == "/api/lineup":
                station.set_lineup(b)
            elif path == "/api/settings":
                station.set_settings(b)
            elif path == "/api/queue":
                if not station.queue_add(b.get("slugs") or [b.get("slug", "")]):
                    return self.reply(404, {"error": "no such show"})
            elif path == "/api/queue/remove":
                station.queue_remove(int(b.get("index", -1)))
            elif path == "/api/queue/clear":
                station.queue_clear()
            elif path == "/api/mute":
                station.set_muted(b.get("muted", not station.settings["muted"]))
            elif path == "/api/uncache":
                station.remove_cached(b.get("slug", ""))
            else:
                return self.reply(404, {"error": "not found"})
            self.reply(200, station.state())
        except Exception as e:
            self.reply(400, {"error": str(e)})


threading.Thread(target=station.run, daemon=True).start()
port = int(flags.get("--port") or 80)
print(f"remote on http://0.0.0.0:{port}/", flush=True)
ThreadingHTTPServer(("0.0.0.0", port), Remote).serve_forever()
