"""Stitch viewport frames into one scene image.

Frames must be consecutive captures of the same moving viewport; the offsets
come from phase correlation between neighbours.
"""
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from froghunt.imaging import estimate_view_shift  # noqa: E402


def load(folder: Path) -> list[Image.Image]:
    return [Image.open(path).convert("RGB") for path in sorted(folder.glob("*.jpg"))]


def stitch(frames: list[Image.Image]) -> tuple[Image.Image, list[tuple[float, float]]]:
    limit = max(frames[0].size) * 0.6
    origins = [(0.0, 0.0)]
    for previous, current in zip(frames, frames[1:]):
        shift = estimate_view_shift(previous, current) or (0.0, 0.0)
        if abs(shift[0]) > limit or abs(shift[1]) > limit:
            shift = (0.0, 0.0)
        origins.append((origins[-1][0] + shift[0], origins[-1][1] + shift[1]))
    left = min(x for x, _ in origins)
    top = min(y for _, y in origins)
    width = frames[0].width + (max(x for x, _ in origins) - left)
    height = frames[0].height + (max(y for _, y in origins) - top)
    canvas = Image.new("RGB", (round(width), round(height)), (255, 255, 255))
    for frame, (x, y) in zip(frames, origins):
        canvas.paste(frame, (round(x - left), round(y - top)))
    return canvas, origins


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    out = Path(sys.argv[2])
    frames = load(folder)
    canvas, origins = stitch(frames)
    canvas.save(out, "JPEG", quality=88)
    print(f"frames {len(frames)} -> {out} {canvas.size}")
    print("origins:", [(round(x), round(y)) for x, y in origins[:6]], "...",
          [(round(x), round(y)) for x, y in origins[-2:]])
