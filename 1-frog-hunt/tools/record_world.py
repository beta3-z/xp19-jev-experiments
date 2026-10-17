"""Record the world while it is scrolled by hand, then stitch the longest take.

Switching focus to the game scrolls the world to the bottom, so the take starts
with a menu reset. Frames are split at hard cuts and only the longest continuous
segment becomes the panorama.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

from froghunt import win32  # noqa: E402
from froghunt.imaging import estimate_view_shift  # noqa: E402

OUT = Path("runs/world_scroll")
COUNTDOWN = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
FPS = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0
WIDTH = 1280
CUT_PX = 420.0

win32.enable_dpi_awareness()
window = win32.find_window("hidden frogs")
if win32.user32.GetForegroundWindow() != window.hwnd:
    window.focus()
OUT.mkdir(parents=True, exist_ok=True)
for old in OUT.glob("*.jpg"):
    old.unlink()

print(f"recording starts in {COUNTDOWN:.0f}s - get the game to the start of the world", flush=True)
time.sleep(COUNTDOWN)

print("recording", flush=True)
frames = []
started = time.time()
index = 0
while time.time() - started < DURATION:
    frame = window.grab()
    small = frame.resize((WIDTH, round(WIDTH * frame.height / frame.width)))
    small.save(OUT / f"{index:04d}.jpg", "JPEG", quality=88)
    frames.append(small)
    index += 1
    print(f"\r{index} frames, {time.time() - started:4.0f}s of {DURATION:.0f}s", end="", flush=True)
    time.sleep(1.0 / FPS)
print()

segments = []
current = [frames[0]]
origins = [(0.0, 0.0)]
for previous, frame in zip(frames, frames[1:]):
    shift = estimate_view_shift(previous, frame)
    if shift is None or abs(shift[0]) > CUT_PX or abs(shift[1]) > CUT_PX:
        segments.append((current, origins))
        current = [frame]
        origins = [(0.0, 0.0)]
        continue
    origins.append((origins[-1][0] + shift[0], origins[-1][1] + shift[1]))
    current.append(frame)
segments.append((current, origins))

for number, (takes, _) in enumerate(segments):
    print(f"segment {number}: {len(takes)} frames")

best_frames, best_origins = max(segments, key=lambda item: len(item[0]))
left = min(x for x, _ in best_origins)
top = min(y for _, y in best_origins)
width = best_frames[0].width + round(max(x for x, _ in best_origins) - left)
height = best_frames[0].height + round(max(y for _, y in best_origins) - top)
canvas = Image.new("RGB", (width, height), (255, 255, 255))
for frame, (x, y) in zip(best_frames, best_origins):
    canvas.paste(frame, (round(x - left), round(y - top)))
canvas.save(OUT / "world.jpg", "JPEG", quality=88)
print(f"world -> {OUT / 'world.jpg'} {canvas.size} from {len(best_frames)} frames")
