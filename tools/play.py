"""Stream a video with sound to the ESP32 TV station (rf-video firmware).

    python3 tools/play.py clip.mp4 [start_seconds] [duration_seconds]
    python3 tools/play.py test [start] [duration]   # moving test pattern + a little tune

ffmpeg turns the clip into 128x96 4-bit grey at 10 fps and mono audio at
7810 Hz (781 samples per frame, which the board plays at 7812.5 Hz) with
75 us FM pre-emphasis. The board reports "#q N" (packet slots in use, of 4);
we send while N plus what we've sent since that report is under 4.
"""
import re, subprocess, sys, time
import serial

W, H, FPS, AUD, NQ = 128, 96, 10, 781, 4
PORT = "/dev/cu.usbserial-0001"
MAGIC = bytes([0xA5, 0x5A, ord("V"), ord("1")])

src = sys.argv[1]
start = sys.argv[2] if len(sys.argv) > 2 else "0"
dur = sys.argv[3] if len(sys.argv) > 3 else None
if src == "test":
    vin = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10"]
    ain = ["-f", "lavfi", "-i", "aevalsrc=0.8*sin(2*PI*t*(330+110*floor(mod(t*2\\,4))))"]
    dur = dur or "20"
else:
    vin = ["-ss", start, "-i", src]
    ain = ["-ss", start, "-i", src]
limit = ["-t", dur] if dur else []

vf = (f"fps={FPS},scale={W}:{H}:force_original_aspect_ratio=decrease,"
      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,format=gray")
video = subprocess.Popen(["ffmpeg", "-loglevel", "error", *vin, *limit, "-vf", vf, "-an",
                          "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
# Film mixes are quiet; squash them hard so the sound uses the full +/-25 kHz.
af = ("highpass=f=120,lowpass=f=3400,"
      "acompressor=threshold=-28dB:ratio=6:attack=5:release=150:makeup=4,dynaudnorm=p=0.9:m=40,"
      "aemphasis=mode=production:type=75fm,volume=9dB,alimiter=limit=0.95")
audio = subprocess.run(["ffmpeg", "-loglevel", "error", *ain, *limit, "-vn", "-ac", "1",
                        "-ar", str(AUD * FPS), "-af", af, "-f", "s8", "-"],
                       stdout=subprocess.PIPE, check=True).stdout

port = serial.Serial(PORT, 921600, timeout=0.05)
time.sleep(2)  # opening the port may reset the board; let that boot finish
port.reset_input_buffer()
port.write(b"H")  # restart clean
out, t0 = b"", time.time()
while b"#ready" not in out and time.time() - t0 < 5:
    out += port.read(256)
if b"#ready" not in out:
    sys.exit("board didn't answer")

queued, sent_since, frame_no, pending = NQ, 0, 0, b""
while True:
    pic = video.stdout.read(W * H)
    if len(pic) < W * H:
        break
    # 4-bit grey, two pixels per byte, left pixel in the high nibble
    packed = bytes((pic[i] >> 4) << 4 | pic[i + 1] >> 4 for i in range(0, W * H, 2))
    snd = audio[frame_no * AUD:(frame_no + 1) * AUD].ljust(AUD, b"\0")
    while queued + sent_since >= NQ:
        lines = (pending + port.read(256)).split(b"\n")
        pending = lines.pop()
        for line in lines:  # RF junk rides along on this line; match the tail
            m = re.search(rb"#q (\d)\r?$", line)
            if m:
                queued, sent_since = int(m[1]), 0
            elif b"#load" in line:
                print(line[line.index(b"#load"):].decode(errors="replace"), flush=True)
    port.write(MAGIC + packed + snd)
    sent_since += 1
    frame_no += 1
    if frame_no % 50 == 0:
        print(f"frame {frame_no}", flush=True)
print(f"sent {frame_no} frames")
