"""Streaming to the ESP32 TV station (rf-video firmware): shared by play.py and station.py.

The stream is framed by A5 5A: "C" + a letter is a command, "V2"/"V3" a
packet of one 4-bit picture (V3 zlib-compressed; 256x192, or 288x216 on an S3) plus that frame's
share of the mu-law audio at 15625 Hz (one sample per TV line). See
rf-video/rf-video.ino for the board side.
"""
import os, select, socket, struct, subprocess, time, zlib

# The picture size the board's firmware was built with: 256x192 on a classic
# ESP32, 288x216 on an S3. It has to match (the board drops packets of any
# other size), so set CHANNEL4_SIZE; the board's #status reports its own.
W, H = (int(v) for v in os.environ.get("CHANNEL4_SIZE", "256x192").lower().split("x"))
RATE, PORT = 15625, 5000
SOUND_METHODS = {"w4": "Y", "w2": "X", "w1": "W", "old": "O", "six": "N"}  # see rf-video.ino
HI = bytes((v >> 4) << 4 for v in range(256))
LO = bytes(v >> 4 for v in range(256))
SILENCE = 0xFF  # mu-law zero


def cmd(letter):
    return bytes([0xA5, 0x5A, ord("C"), ord(letter)])


def setup_commands(sound="w4", dither=True):
    return cmd(SOUND_METHODS[sound]) + cmd("D" if dither else "d")


# 4x4 ordered dither down to the 16 levels the board shows: smooth shading
# instead of flat bands, and a fixed pattern that doesn't crawl or cost much
# more to compress (~25%). One table per Bayer threshold, so it runs as
# C-speed translates (~2.5 ms a frame); the output is already 16-level (q * 17).
BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]
DITHER = [[bytes(min(15, int(v * 15 / 255 + (t + 0.5) / 16)) * 17 for v in range(256)) for t in row] for row in BAYER]


def dither(gray):
    out = bytearray(gray)
    for y in range(H):
        row, tables, o = gray[y * W:(y + 1) * W], DITHER[y & 3], y * W
        for k in range(4):
            out[o + k:o + W:4] = row[k::4].translate(tables[k])
    return bytes(out)


def pack(gray):
    """8-bit grey, W*H bytes -> 4-bit, two pixels per byte, left pixel high.
    (C-speed bytes ops: a per-pixel Python loop can't keep up at 256x192.)"""
    hi, lo = gray[0::2].translate(HI), gray[1::2].translate(LO)
    return (int.from_bytes(hi, "big") | int.from_bytes(lo, "big")).to_bytes(len(hi), "big")


def packet(gray, mulaw, compress=True):
    """Compressed (V3) when it helps: the board's TCP window caps raw at ~430 KB/s."""
    p = pack(gray)
    if compress:
        z = zlib.compress(p, 6)
        if len(z) < len(p):
            return b"\xa5\x5aV3" + struct.pack("<HH", len(mulaw), len(z)) + z + mulaw
    return b"\xa5\x5aV2" + struct.pack("<H", len(mulaw)) + p + mulaw


def audio_share(frame_no, fps):
    """Samples that belong to frame frame_no, so the total stays exact at any fps."""
    return (frame_no + 1) * RATE // fps - frame_no * RATE // fps


def video_filter(fps, brightness=0.0, contrast=1.0, crop=None, fill=False, blend=False):
    # crop ("w:h:x:y") cuts black bars baked into the file; fill zooms a picture
    # that's nearly 4:3 to the whole screen (the set's overscan hides the edges)
    # instead of leaving thin bars. normalize stretches each scene to the full
    # grey range (old films are often tinted, which comes out murky), smoothed
    # over ~3 s so it doesn't pump. blend mixes neighbouring frames when the
    # show is faster than the rate it's sent at (30 -> 24), instead of dropping
    # every fifth frame, which judders on pans.
    eq = f"eq=brightness={brightness}:contrast={contrast}," if brightness or contrast != 1 else ""
    fit = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}," if fill
           else f"scale={W}:{H}:force_original_aspect_ratio=decrease,")
    rate = f"framerate=fps={fps}," if blend else f"fps={fps},"
    return (rate + (f"crop={crop}," if crop else "") + fit +
            f"normalize=smoothing={3 * fps}:independence=0,{eq}"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,format=gray")


# Cleanup of old prints, before the compressor (which would otherwise lift the
# hiss with the quiet dialogue). Measured on a Three Stooges short: pauses
# drop ~4 dB with light, ~27 dB with strong, speech level unchanged; strong
# runs ~3x real time on a 2-core container, light ~12x.
CLEANUP = {
    "off": "",
    "light": "adeclick,afftdn=nr=12:nf=-35:tn=1,",
    "strong": "adeclick,afftdn=nr=20:nf=-30:tn=1,anlmdn=s=4,",
}


def audio_filter(cleanup="strong"):
    # Film mixes are quiet; squash them hard so the sound uses the full
    # +/-25 kHz, then 75 us FM pre-emphasis.
    return ("highpass=f=100,lowpass=f=7000," + CLEANUP[cleanup] +
            "acompressor=threshold=-28dB:ratio=6:attack=5:release=150:makeup=4,dynaudnorm=p=0.9:m=40,"
            "aemphasis=mode=production:type=75fm,volume=9dB,alimiter=limit=0.95")


class Source:
    """A clip decoded to frames: iterate for (grey frame, mu-law audio) pairs."""

    def __init__(self, inputs_video, inputs_audio, fps, brightness=0.0, contrast=1.0, duration=None, cleanup="strong",
                 crop=None, fill=False, smooth=False, blend=False):
        limit = ["-t", str(duration)] if duration else []
        base = ["ffmpeg", "-nostdin", "-loglevel", "error"]
        self.fps, self.smooth = fps, smooth
        # stderr off: cutting a clip short (skip, play now) makes ffmpeg complain about the closed pipe
        self.video = subprocess.Popen([*base, *inputs_video, *limit, "-vf", video_filter(fps, brightness, contrast, crop, fill, blend),
                                       "-an", "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # streamed alongside the video, so a feature film starts right away
        self.audio = subprocess.Popen([*base, *inputs_audio, *limit, "-vn", "-ac", "1", "-ar", str(RATE),
                                       "-af", audio_filter(cleanup), "-f", "mulaw", "-"], stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)

    @classmethod
    def file(cls, path, start=0.0, **kw):
        inputs = ["-ss", str(start), "-i", str(path)]
        return cls(inputs, inputs, **kw)

    def __iter__(self):
        n = 0
        while True:
            gray = self.video.stdout.read(W * H)
            if len(gray) < W * H:
                return
            want = audio_share(n, self.fps)
            yield dither(gray) if self.smooth else gray, self.audio.stdout.read(want).ljust(want, bytes([SILENCE]))
            n += 1

    def close(self):
        for p in (self.video, self.audio):
            p.kill()
            p.wait()


class TV:
    """The Wi-Fi link. TCP flow control paces us (the board stops reading when
    its queue is full); a small send buffer keeps us only a few frames ahead,
    so skips and the 'now playing' thumbnail stay close to what's on screen.
    Reconnects on its own and resends the settings commands. Keeps count of
    drops, and the board's own "#status" lines (firmware that answers command S)."""

    def __init__(self, host="tubetv.local", setup=b""):
        self.host, self.setup, self.sock = host, setup, None
        self.stats = {"connects": 0, "drops": 0, "since": None, "last_drop": None, "last_error": None}
        self.board, self.inbox = None, b""

    def _connect(self):
        s = socket.create_connection((self.host, PORT), timeout=10)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
        s.sendall(self.setup)
        self.sock, self.inbox = s, b""
        self.stats["connects"] += 1
        self.stats["since"] = time.time()

    def send(self, data):
        while True:
            try:
                if self.sock is None:
                    self._connect()
                self.sock.sendall(data)
                self._read()
                return
            except OSError as e:
                print(f"tv link: {e}; retrying", flush=True)
                self.stats.update(drops=self.stats["drops"] + 1, last_drop=time.time(), last_error=str(e), since=None)
                self.close()
                time.sleep(3)

    def _read(self):
        """Pick up whatever the board sent back, without waiting (select first:
        recv on a socket with a timeout would block until data comes)."""
        while select.select([self.sock], [], [], 0)[0]:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionResetError("board closed the connection")
            self.inbox += chunk
        *lines, self.inbox = self.inbox.split(b"\n")
        self.inbox = self.inbox[-512:]  # junk without newlines can't pile up
        for line in lines:
            words = line.decode("ascii", "replace").split()
            if words[:1] != ["#status"]:
                continue
            status = {"at": time.time()}
            for key, value in zip(words[1::2], words[2::2]):
                status[key] = int(value) if value.lstrip("-").isdigit() else value
            if isinstance(status.get("load"), str):  # picture/sound/inflate, % of a core
                status["load"] = [int(v) for v in status["load"].split("/") if v.isdigit()]
            if status.get("size") not in (None, f"{W}x{H}") and (not self.board or self.board.get("size") != status["size"]):
                print(f"board is {status['size']} but we send {W}x{H}: set CHANNEL4_SIZE={status['size']}", flush=True)
            if self.board and status.get("up", 0) < self.board.get("up", 0):
                print(f"board restarted: reset {status.get('reset')}, up {status.get('up')} s", flush=True)
            self.board = status

    def configure(self, setup):
        """New settings commands: sent now and again after any reconnect."""
        self.setup = setup
        self.send(setup)

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock = None
