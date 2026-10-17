"""Place the hand-made annotation into the world panorama.

Each annotated screenshot is located inside the panorama by normalised
correlation of its middle band (the HUD at the bottom never scrolls), then its
boxes are drawn on the panorama together with Jev's proposals.
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
PANORAMA = ROOT / "runs" / "world_scroll" / "world.jpg"
REPORT = ROOT / "runs" / "report"
OUT = ROOT / "runs" / "grab" / "world_map.jpg"

panorama = Image.open(PANORAMA).convert("RGB")
PANEL_W = panorama.width
ann = json.loads((REPORT / "annotations.json").read_text(encoding="utf-8"))
data = json.loads((REPORT / "data.json").read_text(encoding="utf-8"))

_gray = panorama.convert("L")


def band_of(shot: Image.Image) -> Image.Image:
    return shot.crop((0, int(shot.height * 0.18), shot.width, int(shot.height * 0.82)))


def prepare(band: Image.Image, scale: int):
    width = max(40, PANEL_W // scale)
    pan = np.asarray(_gray.resize((width, round(panorama.height * width / PANEL_W))), dtype=np.float32)
    small = np.asarray(band.convert("L").resize((width, round(band.height * width / band.width))), dtype=np.float32)
    small = small - small.mean()
    norm = float(np.sqrt((small ** 2).sum())) or 1.0
    return pan, small, norm


def scan(pan, small, norm, positions):
    height = small.shape[0]
    best_value, best_y = -2.0, positions[0]
    for y in positions:
        window = pan[y:y + height]
        window = window - window.mean()
        value = float((window * small).sum() / (norm * (float(np.sqrt((window ** 2).sum())) or 1.0)))
        if value > best_value:
            best_value, best_y = value, y
    return best_y, best_value


def locate(shot: Image.Image) -> tuple[int, float]:
    band = band_of(shot)
    pan8, small8, norm8 = prepare(band, 8)
    y8, _ = scan(pan8, small8, norm8, range(pan8.shape[0] - small8.shape[0] + 1))

    pan1, small1, norm1 = prepare(band, 1)
    centre = y8 * 8
    low = max(0, centre - 16)
    high = min(pan1.shape[0] - small1.shape[0], centre + 16)
    y1, score = scan(pan1, small1, norm1, range(low, high + 1))
    top = y1 - int(shot.height * 0.18) * PANEL_W / shot.width
    return int(top), score


drawn = 0
world_boxes: list[list[int]] = []
for step in data["steps"]:
    frame = step["frame"]
    value = ann.get(frame)
    if not isinstance(value, dict) or not value.get("boxes"):
        continue
    shot = Image.open(ROOT / "screenshots" / frame).convert("RGB")
    zoom = PANEL_W / shot.width
    offset, score = locate(shot)
    print(f"{frame}: panorama y={offset} confidence={score:+.3f} boxes={len(value['boxes'])}")

    painter = ImageDraw.Draw(panorama)
    if "--with-candidates" in sys.argv:
        for candidate in step.get("candidates") or []:
            x0, y0, x1, y1 = candidate["box_px"]
            painter.rectangle([x0 * zoom, offset + y0 * zoom, x1 * zoom, offset + y1 * zoom],
                              outline=(235, 190, 40), width=1)
    for box in value["boxes"]:
        x0, y0, x1, y1 = box
        world_boxes.append([round(x0 * zoom), round(offset + y0 * zoom),
                            round(x1 * zoom), round(offset + y1 * zoom)])
        painter.rectangle([x0 * zoom, offset + y0 * zoom, x1 * zoom, offset + y1 * zoom],
                          outline=(0, 190, 90), width=2)
        drawn += 1

panorama.save(OUT, "JPEG", quality=86)
(REPORT / "world.json").write_text(json.dumps({
    "image": "/panorama",
    "width": PANEL_W,
    "height": panorama.height,
    "boxes": world_boxes,
}, ensure_ascii=False), encoding="utf-8")
print(f"map -> {OUT} {panorama.size}, {drawn} boxes drawn, world.json written")
