"""Dry run over the saved screenshots, then write a self-contained report page.

Every model answer is kept, so the page can show all candidate boxes and the
numbers behind the decision. The same shape will be reused for a live run.
"""
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_step as A  # noqa: E402

SHOTS = [
    "20261017190823_1.jpg",
    "20261017190837_1.jpg",
    "20261017190904_1.jpg",
    "20261017190918_1.jpg",
    "20261017190934_1.jpg",
    "20261017190941_1.jpg",
    "20261017190946_1.jpg",
    "20261017190951_1.jpg",
]
SCREENSHOTS = Path("screenshots")
OUT = Path("runs/report")
FRAMES = OUT / "frames"
MAX_WIDTH = 1600
GREEN = (0, 190, 90)
YELLOW = (235, 190, 40)
MAGENTA = (220, 60, 200)
INK = (20, 20, 20)


class Meter:
    """Counts calls and tokens without changing the client interface."""

    def __init__(self, client):
        self._client = client
        self.calls = 0
        self.tokens = 0

    def __getattr__(self, name):
        return getattr(self._client, name)

    def ask(self, *args, **kwargs):
        detection = self._client.ask(*args, **kwargs)
        self.calls += 1
        self.tokens += detection.input_tokens
        return detection

    def _post(self, payload, boundary):
        data = self._client._post(payload, boundary)
        self.calls += 1
        self.tokens += int((data.get("usage") or {}).get("input_tokens") or 0)
        return data


FONT = ImageFont.load_default(size=22)
SMALL = ImageFont.load_default(size=18)


def badge(draw, text, origin, colour):
    x, y = origin
    box = draw.textbbox((x + 10, y + 8), text, font=FONT)
    draw.rectangle([x, y, box[2] + 10, box[3] + 8], fill=colour)
    draw.text((x + 10, y + 8), text, fill=(255, 255, 255), font=FONT)


def draw_step(original, step, index):
    image = original.copy()
    draw = ImageDraw.Draw(image)
    for candidate in step.get("candidates", []):
        x0, y0, x1, y1 = candidate["box_px"]
        draw.rectangle([x0, y0, x1, y1], outline=YELLOW, width=3)
        draw.text((x0 + 4, y0 + 4), f"{candidate['score']:.2f} {candidate['tile']}", fill=YELLOW, font=SMALL)
    box = step.get("box_px") or step.get("start_box")
    if box:
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        colour = MAGENTA if step["action"] == "start" else GREEN
        draw.rectangle([x0, y0, x1, y1], outline=colour, width=6)
        draw.line([cx - 26, cy, cx + 26, cy], fill=colour, width=4)
        draw.line([cx, cy - 26, cx, cy + 26], fill=colour, width=4)
    if step["action"] == "scroll":
        badge(draw, "SCROLL DOWN", (24, 24), (200, 90, 40))
    else:
        label = f"STEP {index + 1}  {step['action'].upper()}"
        badge(draw, label, (24, 24), (40, 40, 40))
    return image


def shrink(image, width=MAX_WIDTH):
    height = round(image.height * width / image.width)
    return image.resize((width, height), Image.LANCZOS)


def build() -> dict:
    A.load_env(".env")
    client = Meter(A.Client())
    steps = []
    for index, name in enumerate(SHOTS):
        path = SCREENSHOTS / name
        original = Image.open(path).convert("RGB")
        started = time.perf_counter()
        before = client.calls, client.tokens

        title_p = A.ask_noul(client, A.to_png(original), A.TITLE_QUESTION)
        step = {"frame": name, "title_p": round(title_p, 3), "action": None}
        if title_p >= A.MIN_TITLE:
            start_p, start_box, start_conf = A.ask_box(client, original, "START button")
            step.update(action="start", start_p=round(start_p, 3), start_conf=start_conf, start_box=start_box)
        else:
            cleaned, green_px = A.mask_found(original)
            frog_p = A.ask_noul(client, A.to_png(cleaned), A.FROG_QUESTION)
            step.update(frog_p=round(frog_p, 3), green_px=green_px, grid=[5, 4])
            if frog_p < A.MIN_FROG:
                step["action"] = "scroll"
                step["candidates"] = []
            else:
                candidates = A.rank(client, cleaned, 5, 4)
                step["candidates"] = candidates
                step["action"] = "click" if candidates else "scroll"
                if candidates:
                    step["box_px"] = candidates[0]["box_px"]
                    step["score"] = candidates[0]["score"]

        step["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        step["calls"] = client.calls - before[0]
        step["tokens"] = client.tokens - before[1]
        overlay = draw_step(original, step, index)
        FRAMES.mkdir(parents=True, exist_ok=True)
        relative = f"frames/step_{index:02d}.jpg"
        shrink(overlay).save(OUT / relative, "JPEG", quality=84)
        step["image"] = relative
        steps.append(step)
        print(f"step {index} {name}: {step['action']} title={step['title_p']} "
              f"frog={step.get('frog_p')} calls={step['calls']} tokens={step['tokens']}")

    return {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "dry run over screenshots",
        "model": client.model,
        "calls": client.calls,
        "tokens": client.tokens,
        "cost_usd": round(client.tokens / 1_000_000 * 0.042, 4),
        "steps": steps,
    }


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    data_path = OUT / "data.json"
    if "--render-only" in sys.argv and data_path.exists():
        data = json.loads(data_path.read_text(encoding="utf-8"))
    else:
        data = build()
        data_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    template = (Path(__file__).resolve().parent / "report_template.html").read_text(encoding="utf-8")
    (OUT / "index.html").write_text(template.replace("__DATA__", json.dumps(data, ensure_ascii=False)),
                                    encoding="utf-8")
    print(f"report -> {OUT / 'index.html'}")
