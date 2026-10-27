"""Stream one video with sound to the ESP32 TV station, by hand.

    python3 tools/play.py clip.mp4 [start_seconds] [duration_seconds]
    python3 tools/play.py test [start] [duration]   # moving test pattern + a little tune

flags, anywhere:
    --usb            stream over USB serial instead of Wi-Fi (~3 fps at 256x192)
    --host=NAME      board's address on Wi-Fi (default tubetv.local)
    --fps=N          frame rate (default 24 on Wi-Fi, 3 on USB)
    --sound=w4|w2|w1|old|six   FM method, see rf-video.ino; w4 is the best on
                     the real set (clean sound, picture unharmed)
    --no-dither      static line pattern instead of per-field dither

The Channel 4 service on the station box streams to the same board, and a new stream
takes the TV over: stop it first (the web page's Stop button, or
`ssh root@station.lan systemctl stop channel4`).
Over USB, the board reports "#q N" (slots in use, of 3) and we send while N
plus what we've sent since that report is under 3.
"""
import re, sys, time
from tvlink import Source, TV, packet, setup_commands

NQ = 3
PORT = "/dev/cu.usbserial-0001"

flags = {a.split("=", 1)[0]: (a.split("=", 1) + [""])[1] for a in sys.argv[1:] if a.startswith("--")}
args = [a for a in sys.argv[1:] if not a.startswith("--")]
src = args[0]
start = float(args[1]) if len(args) > 1 else 0.0
dur = float(args[2]) if len(args) > 2 else None
usb = "--usb" in flags
fps = int(flags.get("--fps") or (3 if usb else 24))
setup = setup_commands(flags.get("--sound") or "w4", "--no-dither" not in flags)

if src == "test":
    source = Source(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30"],
                    ["-f", "lavfi", "-i", "aevalsrc=0.8*sin(2*PI*t*(330+110*floor(mod(t*2\\,4))))"],
                    fps=fps, duration=dur or 20)
else:
    source = Source.file(src, start, fps=fps, duration=dur)

if usb:
    import serial
    link = serial.Serial(PORT, 921600, timeout=0.05)
    time.sleep(2)  # opening the port may reset the board; let that boot finish
    link.reset_input_buffer()
    link.write(b"\xa5\x5aCH")  # restart clean
    out, t0 = b"", time.time()
    while b"#ready" not in out and time.time() - t0 < 5:
        out += link.read(256)
    if b"#ready" not in out:
        sys.exit("board didn't answer")
    link.write(setup)
    queued, sent_since, pending = NQ, 0, b""

    def send(data):
        global queued, sent_since, pending
        while queued + sent_since >= NQ:
            lines = (pending + link.read(256)).split(b"\n")
            pending = lines.pop()
            for line in lines:  # RF junk rides along on this line; match the tail
                m = re.search(rb"#q (\d)\r?$", line)
                if m:
                    queued, sent_since = int(m[1]), 0
        link.write(data)
        sent_since += 1
else:
    tv = TV(flags.get("--host") or "tubetv.local", setup)
    send = tv.send

frame_no, t0 = 0, time.time()
for gray, mulaw in source:
    send(packet(gray, mulaw, compress=not usb))
    frame_no += 1
    if frame_no % (5 * fps) == 0:
        print(f"frame {frame_no}, {frame_no / (time.time() - t0):.1f} fps", flush=True)
source.close()
print(f"sent {frame_no} frames")
