"""Channel 4: the tube TV's station, with its own web remote.

    python3 tools/station.py [--host=tubetv.local] [--port=8080]

The port defaults to 80, which needs root or CAP_NET_BIND_SERVICE (the systemd
unit in deploy/ grants it). The night block follows the box's own time zone.

Plays the lineup nonstop to the ESP32 transmitter over one long-lived
connection: the "day" list shuffled, the "evening" list (prime time) from
evening_starts, the "night" list (creature features) from night_starts to
night_ends, shows on the hour and half hour with commercials between, and an
"up next" card before each show.
Clips come from archive.org once, shrunk to 256 px, into the cache. Black bars
baked into a file are found once and cut off, and a picture that's nearly 4:3
fills the screen.

The web page (tools/web/index.html, LAN only, no sign-in) shows what's on
with a live thumbnail, on four pages: Now (the remote: skip, stand by, test
pattern, sign off, mute, volume, what's coming up), Lineup (the editor with an
archive.org search), Downloads (what's cached, space left) and Settings
(picture, sound, the late-night hours). "Play next" queues shows ahead of the lineup. Lineup and settings live in the cache folder
(CHANNEL4_CACHE, default ./cache), seeded from tools/lineup.json.

Runs 24/7 as a systemd service on any small Linux box: see deploy/.
"""
import io, json, os, random, re, shutil, subprocess, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from tvlink import CLEANUP, H, RATE, SILENCE, SOUND_METHODS, W, Source, TV, audio_share, cmd, packet, remap, setup_commands

HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("CHANNEL4_CACHE", HERE.parent / "cache"))
CACHE.mkdir(parents=True, exist_ok=True)
# Where new downloads go (e.g. a NAS share); clips already in CACHE stay there and
# are used first, so the station keeps most of its lineup if the share is down.
CLIPS = Path(os.environ.get("CHANNEL4_CLIPS") or CACHE)
flags = {a.split("=", 1)[0]: (a.split("=", 1) + [""])[1] for a in sys.argv[1:] if a.startswith("--")}

DEFAULT_SETTINGS = {"channel": "4", "fps": 24, "sound": "w4", "dither": True, "brightness": 0.0, "contrast": 1.0, "muted": False,
                    "volume": 100, "cleanup": "strong", "smooth": True, "match_fps": True, "blend": True,
                    "linearity": 0.0}

# The broadcast day, kept in lineup.json next to the late-night hours.
DEFAULT_SCHEDULE = {"cartoon_mornings": True, "mornings_from": 7, "mornings_to": 12,  # weekend mornings: cartoons only
                    "features_from": 12,                                 # daytime features wait for the afternoon
                    "off_air": True, "sign_off": 1, "sign_on": 6,        # O Canada, then the test pattern overnight
                    "breaks": "light",                                   # commercials between shows
                    "on_the_clock": True, "fill_max": 10,                # shows start on the hour and half hour
                    "eras": []}                                          # decades to play (1960, ...); none: all
ERAS = (1920, 1930, 1940, 1950, 1960, 1970)
BREAKS = {"off": 0, "light": 1, "classic": 2}
POOLS = ("day", "evening", "night")  # the blocks shows are drawn from; "bumpers" are the commercials
KINDS = ("cartoon", "short", "episode", "feature")
GUESS = {"cartoon": 420, "short": 1020, "episode": 1560, "feature": 4800, "bumper": 60}  # seconds, until downloaded
SEASONS = {"halloween": ((10, 1), (10, 31)), "christmas": ((12, 1), (12, 26))}
# Columbia Mixed Quartette, recorded 24 May 1913 (Columbia A1369): public domain.
ANTHEM = {"title": "O Canada", "year": 1913, "item": "OCanada_284", "file": "OCanada.mp3"}
IDENT, UP_NEXT = 4, 5  # seconds: "we'll be right back" before commercials, "up next" before a show
SLOT = 30 * 60         # on the clock, shows start on the hour and the half hour
CHANNEL_CARD = 3       # seconds of the channel's card when you switch to it
# Stills go out no brighter than this. The set takes its sound from the beat between
# the picture and sound carriers, and white is the weakest picture carrier (negative
# AM): a still card with big white areas and no soundtrack to cover it buzzed.
CARD_WHITE = 128
DIM = bytes(min(v, CARD_WHITE) for v in range(256))
# The board takes at most 800 audio samples a packet (AUD_MAX), so at 15625 Hz
# nothing may go out under 20 fps: a 15 fps show sent as-is had every packet
# thrown away (frozen picture, no sound).
MIN_FPS = 20


def ulaw_decode(u):  # G.711, +/-32124
    u = ~u & 0xFF
    t = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7)
    return 0x84 - t if u & 0x80 else t - 0x84


def ulaw_encode(x):
    sign = 0x80 if x < 0 else 0
    x = min(abs(x), 32635) + 0x84
    exp = min(7, x.bit_length() - 8)
    return ~(sign | exp << 4 | (x >> (exp + 3)) & 0x0F) & 0xFF


def row_map(k):
    """Which picture row each scan line shows (None: black), to undo a tube's uneven
    vertical sweep, one half at a time: k > 0 squeezes the top half (an old set that
    spreads its top lines out), harder toward the edge, and leaves the bottom half
    alone; k < 0 does the same to the bottom. The lines freed at that edge go out
    black (usually behind the bezel). 0 leaves the picture alone."""
    if not k:
        return None
    rows = []
    for y in range(H):
        t = y / (H - 1)
        u = 0.5 - t if k > 0 else t - 0.5  # distance into the half being squeezed
        if u > 0:
            off = u * (1 + abs(k) * 2 * u)
            t = 0.5 - off if k > 0 else 0.5 + off
        rows.append(round(t * (H - 1)) if 0 <= t <= 1 else None)
    return rows


def pinned():
    """taskset to the last two CPUs we may use, for downloads ([] without taskset)."""
    cpus = sorted(os.sched_getaffinity(0))
    return ["taskset", "-c", ",".join(map(str, cpus[-2:]))] if shutil.which("taskset") and len(cpus) > 2 else []


def volume_table(volume):
    """A bytes.translate table that scales mu-law audio; 0-100 on a squared
    curve, so the slider feels even."""
    gain = (max(0, min(100, volume)) / 100) ** 2
    return bytes(ulaw_encode(round(ulaw_decode(u) * gain)) for u in range(256))


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


def decade(show):
    """1962 -> 1960; None when the show has no year."""
    m = re.match(r"\d{4}", str(show.get("year") or ""))
    return int(m.group()) // 10 * 10 if m else None


def within(hour, start, end):
    """Is the hour in [start, end), which may run past midnight?"""
    return start <= hour < end if start <= end else hour >= start or hour < end


def in_season(show, when):
    """A Halloween or Christmas show plays only in its season; the rest always."""
    season = SEASONS.get(show.get("season") or "")
    if not season:
        return True
    start, end = season
    return start <= (when.month, when.day) <= end


def font(size):
    """Bold sans: Helvetica on the Mac, DejaVu on the station's Debian box."""
    for path, index in (("/System/Library/Fonts/Helvetica.ttc", 1),
                        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0)):
        if os.path.exists(path):
            return ImageFont.truetype(path, size, index=index)
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1 has only the small bitmap font
        return ImageFont.load_default()


# ---- cards: drawn at 256x192, scaled to the board's picture size --------------
BW, BH = 256, 192
ON_AIR = {"number": 4, "name": "Channel 4"}  # the channel tuned in, for the cards


def fit(img, resample=Image.BILINEAR):
    return img if img.size == (W, H) else img.resize((W, H), resample)


def channel_card(*lines):
    img = Image.new("L", (BW, BH), 40)
    d = ImageDraw.Draw(img)
    d.ellipse([88, 10, 168, 90], fill=230, outline=0, width=4)
    number = str(ON_AIR["number"])
    d.text((128, 50), number, font=font(56 if len(number) < 2 else 40), fill=0, anchor="mm")
    d.text((128, 108), ON_AIR["name"].upper(), font=font(18), fill=230, anchor="mm")
    y = 136
    for text, size, shade in lines:
        while font(size).getlength(text) > BW - 16 and size > 9:
            size -= 1
        d.text((128, y), text, font=font(size), fill=shade, anchor="mm")
        y += size + 8
    return fit(img).tobytes()


def test_pattern():
    """For tuning: grey steps, a circle with crosshairs, fine and coarse bars."""
    img = Image.new("L", (BW, BH), 128)
    d = ImageDraw.Draw(img)
    for i in range(8):
        d.rectangle([i * 32, 0, i * 32 + 31, 30], fill=round(i * 255 / 7))
    for x in range(0, BW, 16):
        d.line([x, 32, x, BH - 32], fill=90)
    for y in range(32, BH - 31, 16):
        d.line([0, y, BW, y], fill=90)
    d.ellipse([68, 36, 188, 156], fill=230, outline=0, width=4)
    d.line([128, 36, 128, 156], fill=0, width=2)
    d.line([68, 96, 188, 96], fill=0, width=2)
    d.text((128, 76), ON_AIR["name"].upper(), font=font(16), fill=0, anchor="mm")
    d.text((128, 118), "TEST PATTERN", font=font(12), fill=0, anchor="mm")
    img = fit(img)
    d = ImageDraw.Draw(img)
    sx, sy = W / BW, H / BH
    for i, step in enumerate((1, 2, 4)):  # resolution wedges, finest on the left, at the board's own pixels
        x0, y0, y1 = round((8 + i * 84) * sx), H - round(26 * sy), H - round(4 * sy)
        for x in range(x0, x0 + round(72 * sx), step * 2):
            d.rectangle([x, y0, x + step - 1, y1], fill=0)
            d.rectangle([x + step, y0, x + 2 * step - 1, y1], fill=255)
    return img.tobytes()


# The maple leaf from the flag's construction sheet (Wikimedia's SVG path, in
# 1/2400ths of the flag's height); arcs are drawn as straight lines, which
# nobody can tell at this size.
LEAF = ("m-90 2030 45-863a95 95 0 0 0-111-98l-859 151 116-320a65 65 0 0 0-20-73l-941-762 212-99a65 65 0 0 0 34-79"
        "l-186-572 542 115a65 65 0 0 0 73-38l105-247 423 454a65 65 0 0 0 111-57l-204-1052 327 189a65 65 0 0 0 91-27"
        "l332-652 332 652a65 65 0 0 0 91 27l327-189-204 1052a65 65 0 0 0 111 57l423-454 105 247a65 65 0 0 0 73 38"
        "l542-115-186 572a65 65 0 0 0 34 79l212 99-941 762a65 65 0 0 0-20 73l116 320-859-151a95 95 0 0 0-111 98l45 863z")


def leaf_points():
    tokens = re.findall(r"[mlaz]|-?\d+(?:\.\d+)?", LEAF)
    x = y = 0.0
    pts, i, cmd = [], 0, None
    while i < len(tokens):
        if tokens[i] in "mlaz":
            cmd = tokens[i]
            i += 1
            if cmd == "z":
                continue
        if cmd == "a":  # rx ry rotation large sweep dx dy: keep the end point
            x, y = x + float(tokens[i + 5]), y + float(tokens[i + 6])
            i += 7
        else:
            x, y = x + float(tokens[i]), y + float(tokens[i + 1])
            i += 2
            cmd = "l" if cmd == "m" else cmd
        pts.append((x, y))
    return pts


def flag_card():
    """The flag in grey: white field, dark bars and leaf, "O CANADA" under it."""
    img = Image.new("L", (BW, BH), 20)  # ffmpeg scales it to the board's size
    d = ImageDraw.Draw(img)
    top, fh = 22, 128  # a 2:1 flag, 256 x 128
    d.rectangle([0, top, BW - 1, top + fh - 1], fill=235)
    d.rectangle([0, top, 63, top + fh - 1], fill=70)
    d.rectangle([192, top, BW - 1, top + fh - 1], fill=70)
    pts = leaf_points()
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    scale = (fh * 0.8) / (max(ys) - min(ys))
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    d.polygon([(128 + (px - cx) * scale, top + fh / 2 + (py - cy) * scale) for px, py in pts], fill=70)
    d.text((128, top + fh + 22), "O CANADA", font=font(18), fill=230, anchor="mm")
    return img


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
        self.durations = load("durations.json", {})
        # Channel 4 is lineup.json; any others are lineup-<number>.json, each with its own
        # shows, schedule, shuffle and plan. Only one is on the set at a time: the
        # remote switches, and each runs on its own clock, so you join what's on.
        main = load("lineup.json", None) or json.loads((HERE / "lineup.json").read_text())
        self.lineups = {str(main.setdefault("number", 4)): main}
        for path in sorted(CACHE.glob("lineup-*.json")):
            other = load(path.name, None)
            if other and other.get("number"):
                self.lineups[str(other["number"])] = other
        if len(self.lineups) == 1:
            kids = self.kids_channel(main)
            self.lineups[str(kids["number"])] = kids
            save(f"lineup-{kids['number']}.json", kids)
        self.plans, self.bagsets = {}, {}  # channel -> its plan, its shuffles
        self.airing = {}                   # channel -> {"show", "at"}: what it was playing when we left it
        self.started = None                # when the show on now started (or would have, joined part way)
        self.tuned = False                 # switched channels: join what's on the new one
        self.settings = {**DEFAULT_SETTINGS, **load("settings.json", {})}
        self.ch = self.settings["channel"] if self.settings["channel"] in self.lineups else next(iter(self.lineups))
        ON_AIR.update(number=self.lineup["number"], name=self.channel_name(self.lineup))
        self.volume_lut = volume_table(self.settings["volume"])
        self.rows = row_map(self.settings["linearity"])
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
        self.urgent = set()              # the next show's download, which goes ahead of a "download all"
        self.encoder = threading.Lock()  # one download at a time: shrinking is CPU-bound, and playback needs a core
        self.framing = load("framing.json", {})  # slug -> {"crop", "fill"}: black bars to cut, zoom to fill
        self.resume = load("now.json", None)  # {"show", "pos"}: a restart picks the show up where it was
        self.bulk = None                      # progress of a "download all"
        self.changed = threading.Event()
        self.flag = CACHE / "flag.png"
        flag_card().save(self.flag)
        self.last_ask = 0.0              # when we last asked the board how it's doing
        self.signed_off = 0.0            # when the last sign-off ran (one O Canada a night)

    def setup(self):
        return setup_commands(self.settings["sound"], self.settings["dither"])

    # -- channels -----------------------------------------------------------------
    # The tuned channel's lineup, shuffles (pool -> shows not played yet) and plan
    # (what comes after the queue: {"type": show|signoff|signon, "show", "at"}).
    @property
    def lineup(self):
        return self.lineups[self.ch]

    @property
    def bags(self):
        return self.bagsets.setdefault(self.ch, {})

    @bags.setter
    def bags(self, value):
        self.bagsets[self.ch] = value

    @property
    def plan(self):
        return self.plans.setdefault(self.ch, [])

    @plan.setter
    def plan(self, value):
        self.plans[self.ch] = value

    def kids_channel(self, main):
        """A second channel to start with: the daytime cartoons, and the commercials
        that aren't late-night only, all day, signing off at 8. (Only daytime: a late
        movie not downloaded yet can pass for a cartoon until its length is known.)"""
        night = main.get("night_starts", 22)
        cartoons = [s for s in main.get("day", []) if self.kind(s) == "cartoon"]
        return {"number": 9, "name": "Kids' Channel", "day": cartoons, "evening": [], "night": [],
                "bumpers": [b for b in main.get("bumpers", []) if b.get("block", "day") == "day"],
                "night_starts": night, "evening_starts": night, "night_ends": night,
                "schedule": {"cartoon_mornings": False, "features_from": 12, "sign_off": 20, "sign_on": 6}}

    @staticmethod
    def channel_name(lineup):
        return lineup.get("name") or f"Channel {lineup['number']}"

    def numbers(self):
        """The channels in dial order."""
        return sorted(self.lineups, key=lambda ch: int(self.lineups[ch]["number"]))

    def tune(self, ch=None, step=0):
        """Switch channels, to one or `step` up or down the dial (round the end). The
        one we leave keeps its clock: back on it later, its show is still on if it
        hasn't ended."""
        if step:
            dial = self.numbers()
            ch = dial[(dial.index(self.ch) + step) % len(dial)]
        ch = str(ch)
        if ch not in self.lineups:
            return False
        with self.lock:
            if ch != self.ch:
                if self.now and not self.now.get("special") and self.started:
                    self.airing[self.ch] = {"show": self.now, "at": self.started}
                self.ch = ch
                ON_AIR.update(number=self.lineup["number"], name=self.channel_name(self.lineup))
                self.tuned = True
                self.settings = {**self.settings, "channel": ch}
                save("settings.json", self.settings)
                save("now.json", None)  # a restart picks up the new channel, not the show we left
        self.set_mode("lineup")
        return True

    def tune_in(self):
        """What's on the channel just tuned to, and how far in: the show it was
        playing if that hasn't ended, else the planned show whose time it is. What
        aired while we were away is gone, like real TV. Or (None, 0): carry on."""
        now = datetime.now()
        with self.lock:
            self.now = None
            was = self.airing.pop(self.ch, None)
            if was and was["at"] + timedelta(seconds=self.length_of(was["show"])) > now:
                return was["show"], (now - was["at"]).total_seconds()
            while self.plan and self.plan[0]["at"] + timedelta(seconds=self.item_length(self.plan[0])) <= now:
                if self.plan[0]["type"] == "signoff" and self.off_air(now):
                    self.signed_off = time.time()  # straight to the test pattern, no second anthem
                    break
                self.plan.pop(0)
            if self.plan and self.plan[0]["type"] == "show" and self.plan[0]["at"] <= now:
                item = self.plan.pop(0)
                return item["show"], (now - item["at"]).total_seconds()
        return None, 0.0

    # -- the schedule -------------------------------------------------------------
    # The station plans a few hours ahead (the TV guide shows the plan): each
    # show is drawn for the time it will start, so the block and the daytime
    # rules apply then, not now. Commercials are picked as they come up.
    def schedule(self):
        return {**DEFAULT_SCHEDULE, **self.lineup.get("schedule", {})}

    def block(self, when=None):
        """Late night, then prime time (from evening_starts until late night; none
        if it's unset), daytime the rest."""
        hour, night = (when or datetime.now()).hour, self.lineup["night_starts"]
        if within(hour, night, self.lineup["night_ends"]):
            return "night"
        evening = self.lineup.get("evening_starts", night)
        return "evening" if evening != night and within(hour, evening, night) else "day"

    def in_era(self, show):
        """Is the show from one of the chosen decades? With none chosen, or no year
        on the show, yes."""
        eras = self.schedule()["eras"]
        return not eras or decade(show) is None or decade(show) in eras

    def pool(self, when):
        """The block's shows, or the day's if the block has none from the chosen
        decades (prime time in the 1930s: the 1930s daytime shows)."""
        block = self.block(when)
        for p in (block, "day"):
            if any(self.in_era(s) for s in self.lineup.get(p, [])):
                return p
        return block if self.lineup.get(block) else "day"

    def off_air(self, when):
        sc = self.schedule()
        if not sc["off_air"] or sc["sign_off"] == sc["sign_on"]:
            return False
        hour, a, b = when.hour, sc["sign_off"], sc["sign_on"]
        return a <= hour < b if a < b else hour >= a or hour < b

    def sign_on_time(self, when):
        on = when.replace(hour=self.schedule()["sign_on"], minute=0, second=0, microsecond=0)
        return on if on > when else on + timedelta(days=1)

    def kind(self, show):
        if show.get("kind") in KINDS:
            return show["kind"]
        if self.durations.get(slug(show), 0) > 2400:
            return "feature"
        return "short" if "stooges" in show["title"].lower() else "cartoon"

    def length_of(self, show):
        return self.durations.get(slug(show)) or GUESS[self.kind(show)]

    def fits(self, show, when):
        """May this show start then? Its season, and by day: no features before
        features_from, and only cartoons on weekend mornings."""
        if not in_season(show, when):
            return False
        if self.block(when) != "day":
            return True
        sc, kind = self.schedule(), self.kind(show)
        if kind == "feature" and when.hour < sc["features_from"]:
            return False
        if sc["cartoon_mornings"] and when.weekday() >= 5 and sc["mornings_from"] <= when.hour < sc["mornings_to"]:
            return kind == "cartoon"
        return True

    def draw(self, pool, when, avoid=None, rules=True, within_s=None):
        """The next show in a pool's shuffle that may start at `when`, never the
        same one twice running. Shows that don't fit yet stay in the shuffle for
        later; a seasonal show goes in twice while it's in season. With `within_s`,
        only a show that long or shorter, or None."""
        shows = self.lineup.get(pool) or []
        shows = [s for s in shows if self.in_era(s)] or shows  # the chosen decades, if the pool has any
        if not shows:
            return None
        # commercials: their season, and their block if they have one (late-night spots)
        ok = (lambda s: self.fits(s, when)) if rules else (
            lambda s: in_season(s, when) and s.get("block", self.block(when)) == self.block(when))
        if within_s is not None:
            ok = (lambda base: lambda s: base(s) and self.length_of(s) <= within_s)(ok)
        bag = self.bags.setdefault(pool, [])
        bag[:] = [s for s in bag if s in shows]
        # not one that's on now or already planned (a refill puts every show back), unless there's no other
        busy = {slug(i["show"]) for i in self.plan if i["type"] == "show"} | ({slug(self.now)} if self.now else set())
        for fresh_only in (True, False):
            for refill in (False, True):
                if refill:
                    fresh = [s for s in shows for _ in range(2 if s.get("season") and in_season(s, when) else 1)]
                    random.shuffle(fresh)
                    bag[:] = fresh if len(bag) > 2 * len(shows) else bag + fresh
                for i, show in enumerate(bag):
                    if ok(show) and (avoid is None or slug(show) != slug(avoid) or len(shows) == 1) and \
                            not (fresh_only and pool != "bumpers" and slug(show) in busy):
                        return bag.pop(i)
        if within_s is not None:
            return None
        return random.choice([s for s in shows if in_season(s, when)] or shows)

    def break_length(self):
        n = BREAKS.get(self.schedule()["breaks"], 0)
        return IDENT + n * GUESS["bumper"] if n and self.lineup.get("bumpers") else 0

    def item_length(self, item):
        if item["type"] == "show":
            return self.length_of(item["show"])
        anthem = self.durations.get(slug(ANTHEM), 190)
        return anthem + (8 if item["type"] == "signoff" else 6)

    def left(self):
        """Seconds left of the show on now (its guessed length while it downloads)."""
        return (self.length or (self.length_of(self.now) if self.now and not self.now.get("special") else 0)) - self.pos

    def next_slot(self, when):
        """The first hour or half hour at or after `when`."""
        base = when.replace(minute=0, second=0, microsecond=0)
        return base + timedelta(seconds=-(-(when - base).total_seconds() // SLOT) * SLOT)

    def extend_plan(self, hours=3):
        """Plan until `hours` from now. Call with the lock held.

        On the clock, a show that ends within fill_max minutes of the next hour or
        half hour is followed by commercials until then, so the next one starts on
        time; with more room than that, a short show that fits goes in first (from
        the daytime list too in prime time), the way stations filled odd minutes."""
        now, sc = datetime.now(), self.schedule()
        gap = self.break_length() + UP_NEXT
        if self.plan:
            last = self.plan[-1]
            end, after_show = last["at"] + timedelta(seconds=self.item_length(last)), last["type"] == "show"
        elif self.now:
            end, after_show = now + timedelta(seconds=max(0, self.left())), True
        else:
            end, after_show = now, False
        while end < now + timedelta(hours=hours) and len(self.plan) < 300:
            t = end + timedelta(seconds=gap if after_show else UP_NEXT)
            if self.off_air(t):
                self.plan.append({"type": "signoff", "at": t})
                self.plan.append({"type": "signon", "at": self.sign_on_time(t)})
                end, after_show = self.plan[-1]["at"] + timedelta(seconds=self.item_length(self.plan[-1])), False
                continue
            last = next((i["show"] for i in reversed(self.plan) if i["type"] == "show"), self.now)
            pool, show, fill = self.pool(t), None, False
            if after_show and sc["on_the_clock"]:
                slot = self.next_slot(end + timedelta(seconds=IDENT + UP_NEXT))
                room = (slot - end).total_seconds()
                if room - UP_NEXT <= sc["fill_max"] * 60:
                    t, fill = slot, True
                else:
                    for p in (pool, "day") if self.block(t) == "evening" and pool != "day" else (pool,):
                        show = self.draw(p, t, avoid=last, within_s=room - 2 * gap)
                        if show:
                            break
            show = show or self.draw(self.pool(t), t, avoid=last)  # t may have moved to the next block
            if show is None:
                break
            self.plan.append({"type": "show", "show": show, "at": t, **({"fill": True} if fill else {})})
            end, after_show = t + timedelta(seconds=self.length_of(show)), True

    def replan(self):
        """Start the plan over (it drifted, or the hours changed). Planned shows go
        back to the front of their shuffles, so none are lost. Lock held."""
        for item in reversed(self.plan):
            if item["type"] == "show":
                pool = next((p for p in POOLS if item["show"] in self.lineup.get(p, [])), None)
                if pool:
                    self.bags.setdefault(pool, []).insert(0, item["show"])
        self.plan = []

    def stale(self, item, now):
        """A planned item that no longer belongs here: a show more than 15 minutes
        off its time or due while off the air, or a sign-off before its hour."""
        if item["type"] == "show":
            return abs((now - item["at"]).total_seconds()) > 900 or self.off_air(now)
        if item["type"] == "signoff":
            return not self.off_air(now)
        return False

    def next_item(self):
        with self.lock:
            now = datetime.now()
            if self.plan and self.stale(self.plan[0], now):
                self.replan()
            if not self.plan:
                self.extend_plan()
            item = self.plan.pop(0) if self.plan else None
            self.extend_plan()
            return item

    def peek(self):
        """The next show: the queue's first, else the plan's."""
        with self.lock:
            if self.queue:
                return self.queue[0]
            self.extend_plan()
            return next((i["show"] for i in self.plan if i["type"] == "show"), None)

    def guide(self, hours=12):
        """What's on from now, with estimated start times: the current show, the
        queue, then the plan. The sign-on keeps its own time."""
        with self.lock:
            self.extend_plan(hours)
            plan, queue = list(self.plan), list(self.queue)
        now = datetime.now()
        gap = self.break_length() + UP_NEXT
        rows = []

        def row(kind, show, start, length, **extra):
            out = {"type": kind, "start": start.timestamp(), "time": start.strftime("%H:%M"), "length": round(length)}
            if show:
                out.update(title=show["title"], year=show.get("year", ""), slug=slug(show), kind=self.kind(show),
                           season=show.get("season", ""), cached=self.path(show).exists())
            rows.append({**out, **extra})

        t = now
        if self.now and not self.now.get("special") and self.mode == "lineup":
            row("show", self.now, now - timedelta(seconds=self.pos), self.length or self.length_of(self.now), on_now=True)
            t = now + timedelta(seconds=max(0, self.left()) + gap)
        for show in queue:
            row("show", show, t, self.length_of(show), queued=True)
            t += timedelta(seconds=self.length_of(show) + gap)
        for item in plan:
            if t > now + timedelta(hours=hours):
                break
            if item["type"] == "signon":
                t = max(t, item["at"])
            elif item.get("fill"):  # on the clock: commercials until its time
                t = max(t - timedelta(seconds=gap - IDENT - UP_NEXT), item["at"])
            row(item["type"], item.get("show"), t, self.item_length(item),
                **({"title": "Sign-off: O Canada"} if item["type"] == "signoff" else
                   {"title": "Sign-on: O Canada"} if item["type"] == "signon" else {}))
            t += timedelta(seconds=self.item_length(item) + (gap if item["type"] == "show" else UP_NEXT))
        return rows

    def path(self, show):
        local = CACHE / f"{slug(show)}.mp4"
        return local if CLIPS == CACHE or local.exists() else CLIPS / f"{slug(show)}.mp4"

    def fetch(self, show, again=False):
        """Download and shrink a show into the cache, once (`again`: once more, at the
        current quality, into CLIPS; the old copy plays until the new one is in)."""
        out = CLIPS / f"{slug(show)}.mp4" if again else self.path(show)
        if out.exists() and not again:
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
            with self.encoder:
                return self._fetch(show, out)
        finally:
            with self.lock:
                self.fetching.discard(key)

    def fetch_next(self, show):
        """Fetch the show coming up, ahead of a "download all" (which waits for it)."""
        if self.path(show).exists():
            return
        self.urgent.add(slug(show))
        try:
            self.fetch(show)
        finally:
            self.urgent.discard(slug(show))

    def _fetch(self, show, out):
        """Download and shrink, at the lowest CPU priority so playback never stutters."""
        url = f"https://archive.org/download/{urllib.parse.quote(show['item'])}/{urllib.parse.quote(show['file'])}"
        tmp = out.with_suffix(".part.mp4")
        for attempt, wait in enumerate((0, 5, 20)):  # archive.org throws the odd 5XX
            time.sleep(wait)
            print(f"fetching {show['title']}" + (f" (try {attempt + 1})" if attempt else ""), flush=True)
            audio_only = show["file"].lower().endswith((".mp3", ".ogg", ".flac", ".wav", ".m4a"))
            # the board's width at CRF 23: CRF 28 at 256 wide broke into blocks on movement
            # from the low-bitrate TV uploads (a feature ~90 MB; the share has room)
            # a spot cut from a reel of commercials: its start and end in the reel
            cut = (["-ss", str(show["start"])] if show.get("start") else []) + \
                  (["-to", str(show["end"])] if show.get("end") else [])
            shrink = ["-vn"] if audio_only else ["-vf", f"scale={W}:-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
            r = subprocess.run([*pinned(), "nice", "-n", "19", "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-reconnect", "1",
                                "-reconnect_streamed", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
                                # two cores at most (and pinned, above): x264 takes them all otherwise,
                                # and nice alone didn't keep playback smooth (the box sat at load 12)
                                "-threads", "2", *cut, "-i", url, *shrink, "-threads", "2", "-filter_threads", "1",
                                "-ac", "1", "-c:a", "aac", str(tmp)])
            if r.returncode == 0:
                break
            tmp.unlink(missing_ok=True)
        else:
            print(f"fetch failed: {show['title']}", flush=True)
            return None
        try:
            tmp.replace(out)
        except OSError as e:  # the share wouldn't swap a file that's playing: keep the old one
            print(f"couldn't swap in {show['title']}: {e}", flush=True)
            tmp.unlink(missing_ok=True)
            return out
        local = CACHE / out.name
        if local != out and local.exists():  # an old copy in the cache would be played first
            local.unlink()
        key = slug(show)
        self.durations.pop(key, None)  # measured again from the new file: its bars and size differ
        self.framing.pop(key, None)
        self.duration(show)
        self.frame_up(show)
        return out

    def old_encode(self, show):
        """Downloaded before downloads came at the board's width (256 or 320 wide, CRF 26-28)?"""
        path = self.path(show)
        if not path.exists():
            return False
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        return r.stdout.strip().isdigit() and int(r.stdout) != W

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

    def frame_up(self, show):
        """Find black bars baked into the file (a 4:3 cartoon inside a 16:9 frame
        gets shrunk into a box with bars all round), once per show. Several
        samples, keeping the widest picture any of them sees, so a dark scene
        isn't mistaken for a bar."""
        key = slug(show)
        if key in self.framing or not self.path(show).exists():
            return self.framing.get(key)
        path, length = self.path(show), self.duration(show)
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        try:
            full_w, full_h = (int(v) for v in r.stdout.split(",")[:2])
        except ValueError:
            return None
        boxes = []
        for share in (0.1, 0.25, 0.4, 0.55, 0.7, 0.85):
            r = subprocess.run(["nice", "-n", "19", "ffmpeg", "-nostdin", "-ss", f"{length * share:.1f}", "-i", str(path),
                                "-t", "3", "-vf", "cropdetect=limit=0.1:round=2:reset=0", "-an", "-f", "null", "-"],
                               capture_output=True, text=True)
            found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
            if found:
                w, h, x, y = (int(v) for v in found[-1])
                boxes.append((x, y, x + w, y + h))
        framing = {"crop": None, "fill": False}
        if boxes:
            x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
            x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
            w, h = x1 - x0, y1 - y0
            if w * h >= 0.4 * full_w * full_h:  # anything smaller is a misread, not bars
                if (w, h) != (full_w, full_h):
                    framing["crop"] = f"{w}:{h}:{x0}:{y0}"
                full_w, full_h = w, h
        framing["fill"] = abs(full_w / full_h / (W / H) - 1) <= 0.15  # nearly 4:3: no thin bars
        self.framing[key] = framing
        save("framing.json", self.framing)
        return framing

    def source_fps(self, show):
        """The show's own frame rate, rounded (23.976 -> 24, 29.97 -> 30), once per show."""
        key = slug(show)
        framing = self.framing.get(key)
        if framing is not None and "fps" in framing:
            return framing["fps"]
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                            "-of", "csv=p=0", str(self.path(show))], capture_output=True, text=True)
        try:
            num, _, den = r.stdout.strip().partition("/")
            fps = round(float(num) / float(den or 1))
        except ValueError:
            return None
        if framing is not None:  # measured with the framing; saved with it
            framing["fps"] = fps
            save("framing.json", self.framing)
        return fps

    def show_fps(self, show):
        """The frame rate to send a show at. Converting 24 fps film to 30 repeats every
        fourth frame (and 30 to 24 drops every fifth), which judders; matching the
        show's own rate doesn't. The fps setting stays the ceiling (60 fps halves to 30)."""
        cap = self.settings["fps"]
        own = self.source_fps(show) if self.settings["match_fps"] else None
        if not own:
            return cap
        if own < MIN_FPS:  # 12 fps goes as 24 (each frame twice); 15 as the cap
            own = next((own * k for k in (2, 3) if MIN_FPS <= own * k <= cap), cap)
        if own <= cap:
            return own
        if own % 2 == 0 and MIN_FPS <= own // 2 <= cap:
            return own // 2
        return cap

    def frame_up_all(self):
        """Measure the shows cached before framing existed, in the background,
        and fetch the anthem if it isn't here yet."""
        self.fetch(ANTHEM)
        for show in self.everything():
            try:
                with self.encoder:  # CPU-bound like a download: one at a time
                    self.frame_up(show)
                    self.source_fps(show)
            except Exception as e:
                print(f"framing failed: {show['title']}: {e!r}", flush=True)

    # -- sending ----------------------------------------------------------------
    def interrupted(self):
        return self.skip or self.tuned or self.override is not None or self.mode != "lineup"

    def send(self, gray, mulaw):
        if self.pending_setup:  # settings go down the same connection, from this thread
            self.pending_setup = False
            self.tv.send(self.tv.setup)
        if time.time() - self.last_ask > 30:  # firmware that knows S answers with a #status line
            self.last_ask = time.time()
            self.tv.send(cmd("S"))
        if self.settings["muted"]:  # silence, not no carrier: the set stays quiet instead of buzzing
            mulaw = bytes([SILENCE]) * len(mulaw)
        elif self.settings["volume"] < 100:
            mulaw = mulaw.translate(self.volume_lut)
        self.tv.send(packet(gray, mulaw))
        self.frame = gray

    def hold(self, gray, seconds=None, until=None):
        """Send a still frame (with silence) for a while, or until `until()` says stop."""
        fps = self.settings["fps"]
        # vertical linearity (shows get it in Source, before their dither)
        gray, n = remap(gray.translate(DIM), self.rows), 0
        while (seconds is None or n < seconds * fps) and not (until and until()):
            self.send(gray, bytes([SILENCE]) * audio_share(n, fps))
            n += 1

    def play(self, show, path, start=0.0, resumable=True):
        self.length = self.duration(show)
        while True:
            s = self.settings
            fps = self.show_fps(show)
            own = self.source_fps(show)
            framing = self.framing.get(slug(show)) or {}
            source = Source.file(path, start, fps=fps, brightness=s["brightness"], contrast=s["contrast"],
                                 cleanup=s["cleanup"], crop=framing.get("crop"), fill=framing.get("fill", False),
                                 smooth=s["smooth"], decimate=own == 30 and fps == 24,
                                 blend=s["blend"] and bool(own) and own > fps and own % fps != 0,
                                 rows=self.rows)
            n = 0
            try:
                for gray, mulaw in source:
                    self.send(gray, mulaw)
                    n += 1
                    self.pos = start + n / fps
                    if resumable and n % (5 * fps) == 0:
                        save("now.json", {"show": show, "pos": self.pos})
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

    def segment(self, part):
        """One part of a break, sign-off or sign-on: skip ends just that part;
        "play now" or a mode change ends the whole thing."""
        try:
            part()
        except Interrupted:
            pass
        if self.tuned or self.override is not None or self.mode != "lineup":
            raise Interrupted
        self.skip = False

    def commercial_break(self, until=None):
        """'We'll be right back', then one or two commercials from the bumpers
        list, or with `until` (on the clock) as many as fit before then and the
        station's card for the odd seconds left. One that isn't downloaded yet is
        fetched for next time."""
        bumpers = self.lineup.get("bumpers")
        n = BREAKS.get(self.schedule()["breaks"], 0)
        left = (until - datetime.now()).total_seconds() - IDENT - UP_NEXT if until else None
        if not bumpers or (left is None and not n):
            return
        ads, tries = [], 0
        while tries < 4 * len(bumpers) and (len(ads) < n if left is None else left >= 10):
            tries += 1
            ad = self.draw("bumpers", datetime.now(), avoid=ads[-1] if ads else None, rules=False)
            if not ad:
                break
            if not self.path(ad).exists():
                threading.Thread(target=self.fetch, args=(ad,), daemon=True).start()
            elif left is None:
                ads.append(ad)
            elif self.duration(ad) <= left:
                ads.append(ad)
                left -= self.duration(ad)
        if not ads and (left is None or left > 60):  # nothing downloaded yet: don't sit on a card
            return
        self.now, self.pos, self.length = {"title": "Commercial break", "special": "break"}, 0, 0
        self.segment(lambda: self.hold(channel_card(("WE'LL BE RIGHT BACK", 16, 230), ("after these messages", 12, 170)),
                                       seconds=IDENT, until=self.interrupted))
        for ad in ads:
            self.now, self.pos = {**ad, "special": "commercial"}, 0
            self.segment(lambda: self.play(ad, self.path(ad), resumable=False))
        if until:  # the odd seconds left, a minute at most (the show starts early rather than wait longer)
            self.now, self.pos = {"title": "Station break", "special": "break"}, 0
            stop = min(until - timedelta(seconds=UP_NEXT), datetime.now() + timedelta(seconds=60))
            self.segment(lambda: self.hold(channel_card(("YOU'RE WATCHING", 13, 170), (ON_AIR["name"].upper(), 20, 230)),
                                           until=lambda: datetime.now() >= stop or self.interrupted()))

    def anthem(self):
        """O Canada over the flag, if it's downloaded (fetched for next time if not)."""
        path = self.path(ANTHEM)
        if not path.exists():
            threading.Thread(target=self.fetch, args=(ANTHEM,), daemon=True).start()
            return
        s, fps = self.settings, self.settings["fps"]
        self.length, self.pos = self.duration(ANTHEM), 0
        source = Source(["-loop", "1", "-framerate", str(fps), "-i", str(self.flag)], ["-i", str(path)], fps=fps,
                        duration=self.length, cleanup=s["cleanup"], smooth=s["smooth"], rows=self.rows)
        try:
            for n, (gray, mulaw) in enumerate(source, 1):
                self.send(gray, mulaw)
                self.pos = n / fps
                if self.interrupted():
                    raise Interrupted
        finally:
            source.close()

    def sign_off(self):
        """End of the broadcast day: a card, O Canada, then the test pattern (no
        tone) until sign-on, or until off_air is turned off. After a "play now"
        in the night it goes straight back to the test pattern."""
        if time.time() - self.signed_off > 6 * 3600:
            print(f"{datetime.now():%H:%M} sign-off", flush=True)
            self.signed_off = time.time()
            self.now, self.pos, self.length = {"title": "Sign-off", "special": "signoff"}, 0, 0
            self.segment(lambda: self.hold(channel_card(("THIS CONCLUDES", 16, 230), ("OUR BROADCAST DAY", 16, 230)),
                                           seconds=8, until=self.interrupted))
            self.segment(self.anthem)
        on = self.sign_on_time(datetime.now())
        self.now, self.pos, self.length = {"title": f"Off the air until {on:%-I %p}", "special": "offair"}, 0, 0
        self.hold(test_pattern(), until=lambda: datetime.now() >= on or not self.off_air(datetime.now())
                  or self.tuned or self.override is not None or self.mode != "lineup")
        if self.tuned or self.override is not None or self.mode != "lineup":
            raise Interrupted

    def sign_on(self):
        print(f"{datetime.now():%H:%M} sign-on", flush=True)
        self.signed_off = 0.0
        self.now, self.pos, self.length = {"title": "Sign-on", "special": "signon"}, 0, 0
        self.segment(self.anthem)
        self.now = {"title": "Sign-on", "special": "signon"}
        self.segment(lambda: self.hold(channel_card(("GOOD MORNING", 20, 230), (f"{ON_AIR['name']} is on the air", 13, 170)),
                                       seconds=6, until=self.interrupted))

    def run(self):
        played = False  # a show just ended: a commercial break comes before the next
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
                    self.hold(channel_card(("GOOD NIGHT", 22, 230), (f"{ON_AIR['name']} is off the air", 12, 170)),
                              seconds=6, until=lambda: self.mode != "off")
                    self.hold(bytes(W * H), seconds=1)  # then black and silence
                    while self.mode == "off":
                        self.changed.wait(1)
                    continue
                with self.lock:
                    current, self.override = self.override, None
                    self.skip = False
                    if current is None and self.queue:
                        current = self.queue.pop(0)
                        save("queue.json", self.queue)
                start_at, switched = 0.0, False
                if self.tuned:  # a new channel: join what's on (the queue still goes first)
                    self.tuned, switched = False, True
                    self.resume = None
                    if current is None:
                        current, start_at = self.tune_in()
                if current is None and self.resume and self.mode == "lineup":
                    current, start_at, self.resume = self.resume["show"], float(self.resume["pos"]), None
                item = None
                if current is None:
                    item = self.next_item()
                    if item is None:
                        self.hold(channel_card(("NO SHOWS IN THE LINEUP", 14, 230)), seconds=5)
                        continue
                    if item["type"] != "show":
                        played = False
                        if item["type"] == "signoff":
                            self.sign_off()
                        else:
                            self.sign_on()
                        continue
                    current = item["show"]
                # the plan runs on from the end of this show (a resumed one too), so set it first
                self.now, self.pos, self.length = current, start_at, self.duration(current)
                self.upcoming = self.peek()
                if self.upcoming:
                    threading.Thread(target=self.fetch_next, args=(self.upcoming,), daemon=True).start()
                if played and not start_at:
                    played = False
                    self.commercial_break(until=item["at"] if item and item.get("fill") else None)
                self.now, self.pos, self.length = current, start_at, self.duration(current)
                # when it started (or would have), so leaving the channel and coming back finds it still on
                self.started = datetime.now() - timedelta(seconds=start_at)
                if switched and start_at and self.path(current).exists():
                    # joining part way: the channel's card first (a new show gets it with "up
                    # next"), while the show runs on behind it
                    self.hold(channel_card(("NOW SHOWING", 12, 170), (current["title"], 14, 230)),
                              seconds=CHANNEL_CARD, until=self.interrupted)
                    if self.interrupted():
                        raise Interrupted
                    start_at = self.pos = start_at + CHANNEL_CARD
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
                    continue
                print(f"{datetime.now():%H:%M} now: {current['title']}" + (f" from {start_at:.0f} s" if start_at else ""),
                      flush=True)
                if not start_at:
                    self.hold(channel_card(("UP NEXT", 14, 170), (f"{current['title']} ({current.get('year', '')})", 16, 230)),
                              seconds=5, until=self.interrupted)
                    if self.interrupted():
                        raise Interrupted
                self.play(current, path, start_at)
                save("now.json", None)
                played = True
            except Interrupted:
                # Skip goes straight to what's next (the queue, then the plan), with
                # no commercials; "play now" and mode changes are picked up at the top.
                played = False
            except Exception as e:  # keep the station on the air whatever happens
                print(f"station error: {e!r}", flush=True)
                played = False
                time.sleep(2)

    # -- remote -------------------------------------------------------------------
    def state(self):
        def show(s):
            if not s:
                return s
            path = self.path(s)
            size = path.stat().st_size if path.exists() else 0
            return {**s, "slug": slug(s), "cached": bool(size), "mb": round(size / 1e6, 1),
                    "length": round(self.durations.get(slug(s), 0))}
        lineup = {k: [{**show(s), "kind": self.kind(s), "kind_set": "kind" in s, "season": s.get("season", "")}
                      for s in self.lineup.get(k, [])]
                  for k in (*POOLS, "bumpers")}
        upcoming = self.peek()
        disk = shutil.disk_usage(CLIPS)  # where downloads go
        link = self.tv.stats
        return {"mode": self.mode, "block": self.block(), "now": show(self.now), "upcoming": show(upcoming),
                "coming": [r for r in self.guide(hours=2) if not r.get("on_now")][:6],
                "disk": {"free_mb": disk.free // 10**6, "total_mb": disk.total // 10**6},
                "link": {**link, "connected": self.tv.sock is not None}, "board": self.tv.board,
                "schedule": self.schedule(), "off_air": self.off_air(datetime.now()), "size": f"{W}x{H}",
                "queue": [show(q) for q in self.queue],
                "pos": round(self.pos), "length": round(self.length), "settings": self.settings,
                "lineup": {**self.lineup, **lineup}, "fetching": sorted(self.fetching), "bulk": self.bulk,
                "clock": datetime.now().strftime("%H:%M"), "channel": self.ch, "channels": self.channels(),
                "eras": {e: sum(decade(s) == e for p in POOLS for s in self.lineup.get(p, [])) for e in ERAS}}

    def channels(self):
        """Each channel, with what's on it now (the others: what they were playing,
        or their plan's show for now; None when it's not known)."""
        now, out = datetime.now(), []
        for ch in self.numbers():
            lineup, on = self.lineups[ch], None
            if ch == self.ch:
                on = self.now
            elif self.airing.get(ch) and self.airing[ch]["at"] + timedelta(seconds=self.length_of(self.airing[ch]["show"])) > now:
                on = self.airing[ch]["show"]
            else:
                on = next((i.get("show") for i in self.plans.get(ch, [])
                           if i["at"] <= now < i["at"] + timedelta(seconds=self.item_length(i))), None)
            out.append({"channel": ch, "number": lineup["number"], "name": self.channel_name(lineup),
                        "on": on and {"title": on["title"], "year": on.get("year", "")}})
        return out

    def everything(self):
        """Every show and commercial on every channel."""
        return [s for lineup in self.lineups.values() for k in (*POOLS, "bumpers") for s in lineup.get(k, [])]

    def find(self, key):
        for s in self.everything():
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
        """Replace a channel's lineup (the one tuned in, unless it says which)."""
        ch = str(lineup.get("channel") or self.ch)
        if ch not in self.lineups:
            raise ValueError("no such channel")
        old = self.lineups[ch]
        sc = {**DEFAULT_SCHEDULE, **old.get("schedule", {})}
        for k, v in (lineup.get("schedule") or {}).items():
            if k in ("cartoon_mornings", "off_air"):
                sc[k] = bool(v)
            elif k == "breaks":
                sc[k] = v if v in BREAKS else sc[k]
            elif k == "on_the_clock":
                sc[k] = bool(v)
            elif k == "eras":
                sc[k] = sorted({int(e) for e in v} & set(ERAS)) if isinstance(v, list) else []
            elif k == "fill_max":
                sc[k] = max(0, min(20, int(v)))
            elif k in DEFAULT_SCHEDULE:
                sc[k] = int(v) % 24
        night = int(lineup["night_starts"]) % 24
        clean = {"number": old["number"], **({"name": old["name"]} if old.get("name") else {}),
                 **{k: [] for k in (*POOLS, "bumpers")}, "night_starts": night,
                 "evening_starts": int(lineup.get("evening_starts", night)) % 24,
                 "night_ends": int(lineup["night_ends"]) % 24, "schedule": sc}
        for block in (*POOLS, "bumpers"):
            for s in lineup.get(block, []):
                show = {"title": str(s["title"]), "year": s.get("year") or "", "item": str(s["item"]), "file": str(s["file"])}
                if s.get("kind") in KINDS:
                    show["kind"] = s["kind"]
                if s.get("season") in SEASONS:
                    show["season"] = s["season"]
                for k in ("start", "end"):  # a spot cut from a reel
                    if s.get(k):
                        show[k] = round(float(s[k]), 2)
                if block == "bumpers" and s.get("block") in POOLS:
                    show["block"] = s["block"]  # a commercial for one block only
                clean[block].append(show)
        with self.lock:
            self.lineups[ch] = clean
            self.bagsets[ch], self.plans[ch] = {}, []
        save("lineup.json" if ch == next(iter(self.lineups)) else f"lineup-{ch}.json", clean)

    def set_settings(self, new):
        s = {**self.settings}
        s["fps"] = max(MIN_FPS, min(30, int(new.get("fps", s["fps"]))))
        s["match_fps"] = bool(new.get("match_fps", s["match_fps"]))
        s["smooth"] = bool(new.get("smooth", s["smooth"]))
        s["blend"] = bool(new.get("blend", s["blend"]))
        s["linearity"] = max(-1.0, min(1.0, float(new.get("linearity", s["linearity"]))))
        self.rows = row_map(s["linearity"])
        s["sound"] = new.get("sound", s["sound"]) if new.get("sound") in SOUND_METHODS else s["sound"]
        s["dither"] = bool(new.get("dither", s["dither"]))
        s["brightness"] = max(-0.5, min(0.5, float(new.get("brightness", s["brightness"]))))
        s["contrast"] = max(0.3, min(3.0, float(new.get("contrast", s["contrast"]))))
        s["cleanup"] = new.get("cleanup", s["cleanup"]) if new.get("cleanup") in CLEANUP else s["cleanup"]
        picture = any(s[k] != self.settings[k] for k in ("fps", "match_fps", "smooth", "blend", "brightness", "contrast",
                                                         "cleanup", "linearity"))  # reopen the source
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

    def download(self, scope):
        """Fetch every show not yet cached: the queue, or ("all") the queue and the whole lineup,
        then ("all") fetch again, at the current quality, what was downloaded at an older one."""
        if self.bulk and self.bulk["running"]:
            return
        # what's planned first, then the commercials (small, and they make the breaks), then the rest
        with self.lock:
            planned = [i["show"] for i in self.plan if i["type"] == "show"]
        everything = planned + [ANTHEM] + self.lineup.get("bumpers", []) + self.everything()
        shows = list(self.queue) + ([] if scope == "queue" else everything)
        todo, seen = [], set()
        for show in shows:
            if slug(show) not in seen and not self.path(show).exists():
                seen.add(slug(show))
                todo.append(show)
        self.bulk = {"scope": scope, "total": len(todo), "done": 0, "failed": [], "running": True, "current": None}
        save("bulk.json", {"scope": scope})  # a restart carries on with it

        def work():
            for show in todo:  # one at a time, so playback keeps its CPU
                while self.urgent:  # the next show on the air comes first
                    time.sleep(1)
                self.bulk["current"] = show["title"]
                if self.fetch(show) is None:
                    self.bulk["failed"].append(show["title"])
                self.bulk["done"] += 1
            if scope == "all":
                again, seen = [], set()
                for show in everything:
                    if slug(show) not in seen:
                        seen.add(slug(show))
                        if self.old_encode(show):
                            again.append(show)
                self.bulk["total"] += len(again)
                for show in again:
                    while self.urgent:
                        time.sleep(1)
                    self.bulk["current"] = f"{show['title']} (better quality)"
                    if self.fetch(show, again=True) is None:
                        self.bulk["failed"].append(show["title"])
                    self.bulk["done"] += 1
            self.bulk["running"], self.bulk["current"] = False, None
            save("bulk.json", None)

        threading.Thread(target=work, daemon=True).start()

    def set_volume(self, volume):
        volume = max(0, min(100, int(volume)))
        self.volume_lut = volume_table(volume)
        self.settings = {**self.settings, "volume": volume}
        save("settings.json", self.settings)

    def set_muted(self, muted):
        self.settings = {**self.settings, "muted": bool(muted)}
        save("settings.json", self.settings)

    def remove_cached(self, key):
        show = self.find(key)
        if show:
            self.path(show).unlink(missing_ok=True)
            self.durations.pop(key, None)
            save("durations.json", self.durations)
            self.framing.pop(key, None)
            save("framing.json", self.framing)


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

    def cross_site(self):
        """There's no sign-in, so refuse what another web page could send: a
        foreign Origin, or a body that isn't JSON (text/plain skips the CORS preflight)."""
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return True
        return self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json"

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
            if url.path == "/api/guide":
                return self.reply(200, station.guide(hours=min(24, int(q.get("hours", 12)))))
            if url.path == "/api/search":
                return self.reply(200, archive_search(q.get("q", "")))
            if url.path == "/api/files":
                return self.reply(200, archive_files(q["item"]))
            self.reply(404, {"error": "not found"})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if self.cross_site():
            return self.reply(403, {"error": "cross-site request refused"})
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
            elif path == "/api/channel":
                if not station.tune(b.get("channel", ""), step=int(b.get("step") or 0)):
                    return self.reply(404, {"error": "no such channel"})
            elif path == "/api/settings":
                station.set_settings(b)
            elif path == "/api/queue":
                if not station.queue_add(b.get("slugs") or [b.get("slug", "")]):
                    return self.reply(404, {"error": "no such show"})
            elif path == "/api/queue/remove":
                station.queue_remove(int(b.get("index", -1)))
            elif path == "/api/queue/clear":
                station.queue_clear()
            elif path == "/api/download":
                station.download("queue" if b.get("scope") == "queue" else "all")
            elif path == "/api/volume":
                station.set_volume(b.get("volume", 100))
            elif path == "/api/mute":
                station.set_muted(b.get("muted", not station.settings["muted"]))
            elif path == "/api/uncache":
                station.remove_cached(b.get("slug", ""))
            else:
                return self.reply(404, {"error": "not found"})
            self.reply(200, station.state())
        except Exception as e:
            self.reply(400, {"error": str(e)})


port = int(flags.get("--port") or 80)
server = ThreadingHTTPServer(("0.0.0.0", port), Remote)  # bind first: a refused port stops us before we touch the TV
threading.Thread(target=station.run, daemon=True).start()
threading.Thread(target=station.frame_up_all, daemon=True).start()
if load("bulk.json", None):  # a "download all" the last run didn't finish
    station.download(load("bulk.json", None)["scope"])
print(f"remote on http://0.0.0.0:{port}/", flush=True)
server.serve_forever()
