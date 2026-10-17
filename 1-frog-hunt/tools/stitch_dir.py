"""Stitch an already-recorded frame folder into a panorama, splitting at cuts."""
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from froghunt.imaging import estimate_view_shift  # noqa: E402

folder = Path(sys.argv[1])
out = Path(sys.argv[2]) if len(sys.argv) > 2 else folder / "world.jpg"
cut = float(sys.argv[3]) if len(sys.argv) > 3 else 420.0

frames = [Image.open(path).convert("RGB") for path in sorted(folder.glob("*.jpg")) if path.name != out.name]
print(f"frames {len(frames)}")

segments = []
current = [frames[0]]
origins = [(0.0, 0.0)]
for previous, frame in zip(frames, frames[1:]):
    shift = estimate_view_shift(previous, frame)
    if shift is None or abs(shift[0]) > cut or abs(shift[1]) > cut:
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
canvas.save(out, "JPEG", quality=88)
print(f"world -> {out} {canvas.size} from {len(best_frames)} frames")
