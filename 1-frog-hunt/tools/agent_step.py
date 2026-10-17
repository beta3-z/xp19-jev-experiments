"""Decide the next action for one screenshot.

Screenshots only: no window, no clicks. This is the decision logic the live
loop will reuse, so it can be checked frame by frame first.
"""
import io
import json
import sys
import uuid

import numpy as np
from PIL import Image, ImageFilter

from froghunt.envfile import load_env
from froghunt.imaging import Box, Tile, default_grid, make_grid, prepare_tile
from froghunt.typesafe import Client, _multipart

MAX_SIDE = 1568
TITLE_QUESTION = "Is this the title screen of a game, with a START button?"
FROG_QUESTION = "Is there a frog in `screen`?"
MIN_TITLE = 0.40
MIN_FROG = 0.50
MIN_PRESENT = 0.40
TILE_OVERLAP = 0.12


def ask_noul(client: Client, png: bytes, question: str) -> float:
    ref = uuid.uuid4().hex
    body = {
        "model": client.model,
        "state": {"screen": ref},
        "assets": {ref: {"type": "image", "part": "image"}},
        "questions": {"q": {"type": "noul", "instructions": question}},
    }
    payload, boundary = _multipart(body, png)
    data = client._post(payload, boundary)
    return float((data.get("answers") or {}).get("q", {}).get("noul") or 0.0)


def to_png(image: Image.Image, max_side: int = MAX_SIDE) -> bytes:
    if max(image.size) > max_side:
        scale = max_side / max(image.size)
        image = image.resize((round(image.width * scale), round(image.height * scale)))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def ask_box(client: Client, image: Image.Image, target: str) -> tuple[float, list[int] | None, float | None]:
    """Locate one named thing on the whole frame; box comes back in pixels."""
    scale = min(1.0, MAX_SIDE / max(image.size))
    sent = image if scale == 1.0 else image.resize((round(image.width * scale), round(image.height * scale)))
    detection = client.ask(to_png(sent), target=target)
    if detection.box is None:
        return detection.present, None, detection.confidence
    box = detection.box
    px = [round(box.left * image.width), round(box.top * image.height),
          round(box.right * image.width), round(box.bottom * image.height)]
    return detection.present, px, detection.confidence


def mask_found(image: Image.Image) -> tuple[Image.Image, int]:
    """Paint already-found (green) frogs out so they cannot be picked again."""
    array = np.asarray(image.convert("RGB")).astype(int)
    green = (array[:, :, 1] > array[:, :, 0] + 25) & (array[:, :, 1] > array[:, :, 2] + 25)
    if green.sum() == 0:
        return image, 0
    mask = Image.fromarray((green * 255).astype("uint8")).filter(ImageFilter.MaxFilter(25))
    cleaned = image.convert("RGB").copy()
    cleaned.paste(Image.new("RGB", image.size, (247, 247, 242)), (0, 0), mask)
    return cleaned, int(green.sum())


def rank(client: Client, image: Image.Image, cols: int, rows: int) -> list[dict]:
    tiles = make_grid(cols, rows, TILE_OVERLAP)
    width, height = image.size
    candidates = []
    for tile in tiles:
        png = prepare_tile(image, tile, MAX_SIDE)
        detection = client.ask(png, tile=tile, target="frog")
        if detection.box is None or detection.present < MIN_PRESENT:
            continue
        box = tile.to_global(detection.box)
        px0, py0, px1, py1 = box.to_pixels(width, height)
        if px1 - px0 < 8 or py1 - py0 < 8:
            continue
        score = detection.present * (detection.confidence if detection.confidence is not None else 0.5)
        if box.area() > 0.5:
            score *= 0.4
        local = detection.box
        if local.left < 0.02 or local.top < 0.02 or local.right > 0.98 or local.bottom > 0.98:
            score *= 0.7
        candidates.append({
            "tile": tile.name,
            "present": round(detection.present, 3),
            "confidence": detection.confidence,
            "score": round(score, 3),
            "box_px": [int(v) for v in (px0, py0, px1, py1)],
        })
    return sorted(candidates, key=lambda c: c["score"], reverse=True)


def main(paths: list[str], cols: int | None = None, rows: int | None = None, crops: str | None = None) -> None:
    load_env(".env")
    client = Client()
    for path in paths:
        image = Image.open(path).convert("RGB")
        name = path.split("\\")[-1]
        title = ask_noul(client, to_png(image), TITLE_QUESTION)
        if title >= MIN_TITLE:
            start_p, start_box, _ = ask_box(client, image, "START button")
            print(json.dumps({"frame": name, "screen": "title", "title_p": round(title, 3),
                              "action": "start", "start_box": start_box, "start_p": round(start_p, 3)}))
            continue
        cleaned, green_px = mask_found(image)
        frog = ask_noul(client, to_png(cleaned), FROG_QUESTION)
        grid_cols, grid_rows = (cols, rows) if cols and rows else default_grid(*image.size)
        if frog < MIN_FROG:
            print(json.dumps({"frame": name, "screen": "level", "title_p": round(title, 3),
                              "frog_p": round(frog, 3), "green_px": green_px, "action": "scroll"}))
            continue
        candidates = rank(client, cleaned, grid_cols, grid_rows)
        best = candidates[0] if candidates else None
        print(json.dumps({
            "frame": name, "screen": "level", "title_p": round(title, 3),
            "frog_p": round(frog, 3), "green_px": green_px, "grid": [grid_cols, grid_rows],
            "action": "click" if best else "scroll",
            "box_px": best["box_px"] if best else None,
            "score": best["score"] if best else None,
            "top": candidates[:3],
        }))
        if crops and best:
            x0, y0, x1, y1 = best["box_px"]
            margin = 110
            crop = image.crop((max(0, x0 - margin), max(0, y0 - margin),
                               min(image.width, x1 + margin), min(image.height, y1 + margin)))
            crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
            crop.save(f"{crops}\\{name.replace('.jpg', '')}.png")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    options = {a.split("=")[0]: a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--") and "=" in a}
    main(
        args,
        cols=int(options["--cols"]) if "--cols" in options else None,
        rows=int(options["--rows"]) if "--rows" in options else None,
        crops=options.get("--crops"),
    )
