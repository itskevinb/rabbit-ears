"""Cut a reel of old commercials into single spots for the station's lineup.

    python3 tools/reel.py <archive.org item> <file> [--out=DIR] [--min=8] [--max=150]

Downloads a small copy of the reel (256 px wide), finds the gaps between spots
(black frames, or on a tape with none, a short silence at a hard cut), and writes to DIR (default ./reels/<item>):

  spots.json   one lineup entry per spot: {"title", "item", "file", "start", "end"}
  sheet.png    a contact sheet, one numbered frame from the middle of each spot,
               to name them by (the titles start out as "<item> #n")

Spots shorter than --min seconds join the one before; longer than --max are
flagged (two spots with no black between them, or a stretch that isn't an ad).
Paste the entries you want into the Commercials list with POST /api/lineup:
the station cuts each spot out of the reel when it first downloads it.
"""
import json, re, subprocess, sys, urllib.parse
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

args = [a for a in sys.argv[1:] if not a.startswith("--")]
flags = {a.split("=", 1)[0]: (a.split("=", 1) + [""])[1] for a in sys.argv[1:] if a.startswith("--")}
if len(args) != 2:
    sys.exit(__doc__)
item, name = args
out = Path(flags.get("--out") or Path("reels") / item)
out.mkdir(parents=True, exist_ok=True)
MIN, MAX = float(flags.get("--min") or 8), float(flags.get("--max") or 150)

small = out / "reel.mp4"
if not small.exists():
    url = f"https://archive.org/download/{urllib.parse.quote(item)}/{urllib.parse.quote(name)}"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", url, "-vf", "scale=256:-2",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-ac", "1", "-c:a", "aac",
                    str(small.with_suffix(".part.mp4"))], check=True)
    small.with_suffix(".part.mp4").rename(small)

length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                               str(small)], capture_output=True, text=True).stdout)
r = subprocess.run(["ffmpeg", "-nostdin", "-i", str(small), "-vf", "blackdetect=d=0.08:pic_th=0.95:pix_th=0.12",
                    "-an", "-f", "null", "-"], capture_output=True, text=True)
gaps = [(float(a) + float(b)) / 2 for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", r.stderr)]
if len(gaps) < length / 120:  # hardly any black (an off-air tape): silences that line up with a hard cut
    r = subprocess.run(["ffmpeg", "-nostdin", "-i", str(small), "-vf", "select='gt(scene,0.3)',showinfo",
                        "-af", "silencedetect=n=-40dB:d=0.2", "-f", "null", "-"], capture_output=True, text=True)
    scenes = [float(t) for t in re.findall(r"pts_time:([\d.]+)", r.stderr)]
    quiet = list(zip(map(float, re.findall(r"silence_start: ([\d.]+)", r.stderr)),
                     map(float, re.findall(r"silence_end: ([\d.]+)", r.stderr))))
    gaps = [next(t for t in scenes if a - 0.5 <= t <= b + 0.5) for a, b in quiet
            if any(a - 0.5 <= t <= b + 0.5 for t in scenes)]

# cut at each gap
cuts = [0.0] + sorted(t for t in gaps if 0.5 < t < length - 0.5) + [length]
spots = []
for a, b in zip(cuts, cuts[1:]):
    if spots and b - a < MIN:
        spots[-1][1] = b
    else:
        spots.append([a, b])
if len(spots) > 1 and spots[0][1] - spots[0][0] < MIN:
    spots[1][0] = spots.pop(0)[0]

entries = [{"title": f"{item} #{i}", "item": item, "file": name, "start": round(a, 2), "end": round(b, 2),
            **({"check": "long: two spots in one?"} if b - a > MAX else {})} for i, (a, b) in enumerate(spots, 1)]
(out / "spots.json").write_text(json.dumps(entries, indent=1))

# contact sheet: 6 across, each frame from the middle of its spot
tw, th, cols = 160, 120, 6
rows = -(-len(spots) // cols)
sheet = Image.new("L", (cols * tw, rows * (th + 14)), 20)
d = ImageDraw.Draw(sheet)
try:
    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 12)
except OSError:
    font = ImageFont.load_default()
for i, (a, b) in enumerate(spots):
    frame = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{(a + b) / 2:.2f}", "-i", str(small),
                            "-frames:v", "1", "-vf", f"scale={tw}:{th}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                           capture_output=True).stdout
    x, y = i % cols * tw, i // cols * (th + 14)
    if len(frame) == tw * th:
        sheet.paste(Image.frombytes("L", (tw, th), frame), (x, y + 14))
    d.text((x + 3, y + 1), f"#{i + 1}  {b - a:.0f}s", fill=255, font=font)
sheet.save(out / "sheet.png")
print(f"{len(spots)} spots in {length / 60:.1f} min -> {out}/spots.json, {out}/sheet.png "
      f"({sum('check' in e for e in entries)} longer than {MAX:.0f} s)")
