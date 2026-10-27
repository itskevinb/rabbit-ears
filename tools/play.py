"""Stream a video with sound to the ESP32 TV station (rf-video firmware).

    python3 tools/play.py clip.mp4 [start_seconds] [duration_seconds]
    python3 tools/play.py test [start] [duration]   # moving test pattern + a little tune

flags, anywhere:
    --usb            stream over USB serial instead of Wi-Fi (caps at ~10 fps)
    --host=NAME      board's address on Wi-Fi (default tubetv.local)
    --fps=N          frame rate (default 30 on Wi-Fi, 10 on USB)
    --sound=w4|w2|w1|old|six   FM method, see rf-video.ino; w4 is the best on
                     the real set (clean sound, picture unharmed)
    --no-dither      static line pattern instead of per-field dither

ffmpeg turns the clip into 128x96 4-bit grey and mono mu-law audio at
15625 Hz (one sample per TV line) with 75 us FM pre-emphasis. Each packet is
one frame plus its share of the audio, so picture and sound stay in sync.
Over Wi-Fi, TCP paces us: the board stops reading when its queue is full.
Over USB, the board reports "#q N" (slots in use, of 4) and we send while N
plus what we've sent since that report is under 4.
"""
import re, socket, struct, subprocess, sys, time

W, H, RATE, NQ = 128, 96, 15625, 4
PORT = "/dev/cu.usbserial-0001"

flags = {a.split("=", 1)[0]: (a.split("=", 1) + [""])[1] for a in sys.argv[1:] if a.startswith("--")}
args = [a for a in sys.argv[1:] if not a.startswith("--")]
src = args[0]
start = args[1] if len(args) > 1 else "0"
dur = args[2] if len(args) > 2 else None
usb = "--usb" in flags
fps = int(flags.get("--fps") or (10 if usb else 30))
sound = flags.get("--sound") or "w4"

if src == "test":
    vin = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30"]
    ain = ["-f", "lavfi", "-i", "aevalsrc=0.8*sin(2*PI*t*(330+110*floor(mod(t*2\\,4))))"]
    dur = dur or "20"
else:
    vin = ["-ss", start, "-i", src]
    ain = ["-ss", start, "-i", src]
limit = ["-t", dur] if dur else []

# normalize stretches each scene to the full grey range (old films are often
# tinted, which comes out murky in grey), smoothed over ~3 s so it doesn't pump.
vf = (f"fps={fps},scale={W}:{H}:force_original_aspect_ratio=decrease,"
      f"normalize=smoothing={3 * fps}:independence=0,"
      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,format=gray")
video = subprocess.Popen(["ffmpeg", "-loglevel", "error", *vin, *limit, "-vf", vf, "-an",
                          "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
# Film mixes are quiet; squash them hard so the sound uses the full +/-25 kHz.
af = ("highpass=f=100,lowpass=f=7000,"
      "acompressor=threshold=-28dB:ratio=6:attack=5:release=150:makeup=4,dynaudnorm=p=0.9:m=40,"
      "aemphasis=mode=production:type=75fm,volume=9dB,alimiter=limit=0.95")
audio = subprocess.run(["ffmpeg", "-loglevel", "error", *ain, *limit, "-vn", "-ac", "1",
                        "-ar", str(RATE), "-af", af, "-f", "mulaw", "-"],
                       stdout=subprocess.PIPE, check=True).stdout

cmd = lambda letter: bytes([0xA5, 0x5A, ord("C"), ord(letter)])
setup = (cmd({"old": "O", "six": "N", "w1": "W", "w2": "X", "w4": "Y"}[sound])
         + cmd("d" if "--no-dither" in flags else "D"))

if usb:
    import serial
    link = serial.Serial(PORT, 921600, timeout=0.05)
    time.sleep(2)  # opening the port may reset the board; let that boot finish
    link.reset_input_buffer()
    link.write(cmd("H"))  # restart clean
    out, t0 = b"", time.time()
    while b"#ready" not in out and time.time() - t0 < 5:
        out += link.read(256)
    if b"#ready" not in out:
        sys.exit("board didn't answer")
    link.write(setup)
    queued, sent_since, pending = NQ, 0, b""

    def send(packet):
        global queued, sent_since, pending
        while queued + sent_since >= NQ:
            lines = (pending + link.read(256)).split(b"\n")
            pending = lines.pop()
            for line in lines:  # RF junk rides along on this line; match the tail
                m = re.search(rb"#q (\d)\r?$", line)
                if m:
                    queued, sent_since = int(m[1]), 0
                elif b"#load" in line:
                    print(line[line.index(b"#load"):].decode(errors="replace"), flush=True)
        link.write(packet)
        sent_since += 1
else:
    host = flags.get("--host") or "tubetv.local"
    link = socket.create_connection((host, 5000), timeout=10)
    link.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    link.sendall(setup)
    send = link.sendall

frame_no, t0 = 0, time.time()
while True:
    pic = video.stdout.read(W * H)
    if len(pic) < W * H:
        break
    # 4-bit grey, two pixels per byte, left pixel in the high nibble
    packed = bytes((pic[i] >> 4) << 4 | pic[i + 1] >> 4 for i in range(0, W * H, 2))
    a0, a1 = frame_no * RATE // fps, (frame_no + 1) * RATE // fps
    snd = audio[a0:a1].ljust(a1 - a0, b"\xff")  # 0xFF = mu-law zero
    send(bytes([0xA5, 0x5A, ord("V"), ord("2")]) + struct.pack("<H", len(snd)) + packed + snd)
    frame_no += 1
    if frame_no % (5 * fps) == 0:
        print(f"frame {frame_no}, {frame_no / (time.time() - t0):.1f} fps", flush=True)
print(f"sent {frame_no} frames")
