"""Turn a run folder into a video: survey animation, box reveal, stats card.

Two sources:
  * `film/` — frames recorded during the run. Boxes are drawn on top of the
    real footage, in post, using the event timeline. This is the default when
    `run --record` was used.
  * `shots/` — one still per round. A reconstruction with a survey animation,
    used when there is no recording.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .imaging import (
    AMBER,
    DIM,
    GREEN,
    RED,
    WHITE,
    cover_canvas,
    fit_canvas,
    load_font,
    scrim,
    text,
)

GaussianBlur = ImageFilter.GaussianBlur

SIZES = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

Rect = tuple[int, int, int, int]


@dataclass
class RoundView:
    index: int
    raw: Image.Image
    calls: list[dict] = field(default_factory=list)
    found: bool = False
    box: dict | None = None
    tile: str | None = None
    confidence: float | None = None
    start_t: float = 0.0
    end_t: float = 0.0


def load_run(out_dir: Path) -> tuple[dict, list[RoundView]]:
    out_dir = Path(out_dir)
    events = [
        json.loads(line)
        for line in (out_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rounds: dict[int, RoundView] = {}
    for event in events:
        kind = event.get("event")
        if kind == "round_start":
            index = event["round"]
            rounds[index] = RoundView(
                index=index,
                raw=Image.open(out_dir / "shots" / f"{index:03d}_raw.jpg").convert("RGB"),
                start_t=event["t"],
            )
        elif kind == "call" and event.get("round") in rounds:
            rounds[event["round"]].calls.append(event)
        elif kind == "round_end" and event.get("round") in rounds:
            view = rounds[event["round"]]
            view.found = bool(event.get("found"))
            view.box = event.get("box")
            view.tile = event.get("tile")
            view.confidence = event.get("confidence")
            view.end_t = event["t"]
    run = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))
    return run, sorted(rounds.values(), key=lambda r: r.index)


def render(
    out_dir: Path,
    *,
    aspect: str = "16:9",
    fps: int = 30,
    scan_s: float = 0.9,
    reveal_s: float = 0.7,
    hold_s: float = 0.5,
    size: tuple[int, int] | None = None,
) -> Path | None:
    out_dir = Path(out_dir)
    run, views = load_run(out_dir)
    size = size or SIZES.get(aspect, SIZES["16:9"])
    frames_dir = out_dir / "video_frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)

    counter = 0

    def emit(canvas: Image.Image) -> None:
        nonlocal counter
        canvas.save(frames_dir / f"{counter:05d}.jpg", "JPEG", quality=92)
        counter += 1

    for _ in range(int(1.8 * fps)):
        emit(_intro(size, run))

    total = len(views)
    for position, view in enumerate(views):
        canvas, rect = _base(view.raw, size)
        found_so_far = sum(1 for v in views[:position] if v.found)
        clock = _clock(view.end_t)

        scan_frames = max(2, int(scan_s * fps))
        slowest = max((c["latency_ms"] for c in view.calls), default=1.0) or 1.0
        for i in range(scan_frames):
            progress = i / (scan_frames - 1)
            emit(_scan_frame(canvas.copy(), rect, view, progress, slowest, position + 1, total, found_so_far, clock))

        reveal_frames = max(2, int(reveal_s * fps))
        for i in range(reveal_frames):
            progress = i / (reveal_frames - 1)
            emit(_reveal_frame(canvas.copy(), rect, view, progress, position + 1, total, found_so_far, clock))

        for _ in range(max(1, int(hold_s * fps))):
            emit(_reveal_frame(canvas.copy(), rect, view, 1.0, position + 1, total, found_so_far, clock))

    for _ in range(int(2.6 * fps)):
        emit(_outro(size, run))

    video = out_dir / "video.mp4"
    encoder = find_ffmpeg()
    if encoder is None:
        print(f"frames written to {frames_dir} ({counter}). No ffmpeg found; install ffmpeg or use the frames elsewhere.")
        return None
    subprocess.run(
        [
            encoder,
            "-y",
            "-loglevel",
            "error",
            "-framerate",
            str(fps),
            "-i",
            str(frames_dir / "%05d.jpg"),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(video),
        ],
        check=True,
    )
    return video


def find_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        str(Path.home() / "scoop" / "shims" / "ffmpeg.exe"),
    ):
        if Path(candidate).exists():
            return candidate
    return None


# -- frames --------------------------------------------------------------

def _base(raw: Image.Image, size: tuple[int, int]) -> tuple[Image.Image, Rect]:
    scale = min(size[0] / raw.width, size[1] / raw.height)
    width, height = round(raw.width * scale), round(raw.height * scale)
    rect = ((size[0] - width) // 2, (size[1] - height) // 2, width, height)

    if width * height < 0.8 * size[0] * size[1]:
        # Vertical/square cut: blurred cover behind the whole scene beats black bars.
        canvas = cover_canvas(raw, size).filter(GaussianBlur(34))
        canvas = Image.blend(canvas, Image.new("RGB", size, (0, 0, 0)), 0.45)
        canvas.paste(raw.resize((width, height), Image.LANCZOS), (rect[0], rect[1]))
    else:
        canvas = fit_canvas(raw, size)
    return canvas, rect


def _map(box: dict, rect: Rect) -> Rect:
    ox, oy, width, height = rect
    return (
        ox + int(box["left"] * width),
        oy + int(box["top"] * height),
        ox + int(box["right"] * width),
        oy + int(box["bottom"] * height),
    )


def _scan_frame(
    canvas: Image.Image,
    rect: Rect,
    view: RoundView,
    progress: float,
    slowest: float,
    number: int,
    total: int,
    found_so_far: int,
    clock: str,
) -> Image.Image:
    overlay = Image.new("RGBA", canvas.size, (6, 6, 8, 150))
    layer = ImageDraw.Draw(overlay)
    ox, oy, width, height = rect
    cols, rows = _grid_of(view)
    for col in range(1, cols):
        x = ox + width * col // cols
        layer.line([(x, oy), (x, oy + height)], fill=(255, 255, 255, 45), width=1)
    for row in range(1, rows):
        y = oy + height * row // rows
        layer.line([(ox, y), (ox + width, y)], fill=(255, 255, 255, 45), width=1)

    for call in view.calls:
        col, row = _tile_colrow(call["tile"])
        x0 = ox + width * (col - 1) // cols
        y0 = oy + height * (row - 1) // rows
        x1 = ox + width * col // cols
        y1 = oy + height * row // rows
        answered_at = 0.15 + 0.7 * (call["latency_ms"] / slowest)
        if progress >= answered_at:
            layer.rectangle([x0, y0, x1, y1], outline=(120, 235, 150, 220), width=3)
            badge = f'{call["latency_ms"] / 1000:.2f}s'
            font = load_font(32)
            label_x, label_y = x0 + 14, y1 - 48
            box = layer.textbbox((label_x, label_y), badge, font=font)
            layer.rectangle([box[0] - 8, box[1] - 4, box[2] + 8, box[3] + 4], fill=(0, 0, 0, 170))
            text(layer, (label_x, label_y), badge, font, fill=(150, 240, 175), shadow=False)
        else:
            pulse = 55 + int(120 * abs(((progress * 5) % 1) - 0.5) * 2)
            layer.rectangle([x0 + 2, y0 + 2, x1 - 2, y1 - 2], outline=(255, 255, 255, pulse), width=2)

    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    return _hud(canvas, clock, number, total, found_so_far, f"{len(view.calls)} looks in parallel")


def _reveal_frame(
    canvas: Image.Image,
    rect: Rect,
    view: RoundView,
    progress: float,
    number: int,
    total: int,
    found_so_far: int,
    clock: str,
) -> Image.Image:
    if view.found:
        canvas = _hud(canvas, clock, number, total, found_so_far + 1, "frog located")
    else:
        canvas = _hud(canvas, clock, number, total, found_so_far, "no candidate")

    draw = ImageDraw.Draw(canvas)
    k = canvas.width / 1920
    if view.found and view.box:
        x0, y0, x1, y1 = _map(view.box, rect)
        ease = 1 - (1 - progress) ** 3
        pad = int((1 - ease) * 46 * k)
        ring = max(3, int((6 + 8 * ease) * k))
        draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], outline=GREEN, width=ring)
        if progress < 0.6:
            extra = int(14 * (1 - progress) * k)
            draw.rectangle([x0 - pad - extra, y0 - pad - extra, x1 + pad + extra, y1 + pad + extra], outline=GREEN, width=max(2, int(2 * k)))
        label = f"{view.confidence or 0:.2f} confidence"
        text(draw, (x0 - pad, max(10, y0 - pad - int(44 * k))), label, load_font(int(34 * k)), fill=GREEN)
    else:
        text(draw, (canvas.width // 2, canvas.height // 2), "nothing found", load_font(int(64 * k)), fill=RED, anchor="mm")
    return canvas


def _hud(canvas: Image.Image, clock: str, number: int, total: int, found: int, hint: str) -> Image.Image:
    canvas = scrim(canvas)
    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size
    k = width / 1920
    text(draw, (int(36 * k), int(24 * k)), clock, load_font(int(72 * k)), fill=WHITE)
    text(draw, (width - int(36 * k), int(28 * k)), f"ROUND {number:02d}/{total:02d}", load_font(int(44 * k)), fill=WHITE, anchor="ra")
    text(draw, (width - int(36 * k), int(86 * k)), f"found {found}", load_font(int(30 * k)), fill=AMBER, anchor="ra")
    text(draw, (int(36 * k), height - int(52 * k)), hint, load_font(int(30 * k)), fill=(200, 200, 210))
    return canvas


def _intro(size: tuple[int, int], run: dict) -> Image.Image:
    canvas = Image.new("RGB", size, DIM)
    draw = ImageDraw.Draw(canvas)
    width, height = size
    text(draw, (width // 2, height // 2 - 120), "JEV vs 100 HIDDEN FROGS", load_font(int(width * 0.055)), fill=WHITE, anchor="mm")
    text(draw, (width // 2, height // 2 + 10), "one screenshot · one frog at a time", load_font(int(width * 0.026)), fill=(190, 190, 200), anchor="mm")
    text(draw, (width // 2, height // 2 + 90), f"model: {run.get('model') or 'jev-1.14.0'}", load_font(int(width * 0.022)), fill=GREEN, anchor="mm")
    return canvas


def _outro(size: tuple[int, int], run: dict) -> Image.Image:
    canvas = Image.new("RGB", size, DIM)
    draw = ImageDraw.Draw(canvas)
    width, height = size
    found = run.get("found", 0)
    rounds = run.get("rounds", 0)
    text(draw, (width // 2, height // 2 - 170), f"{found}/{rounds}", load_font(int(width * 0.13)), fill=GREEN, anchor="mm")
    text(draw, (width // 2, height // 2 - 40), "frogs found", load_font(int(width * 0.035)), fill=WHITE, anchor="mm")
    lines = [
        f"{run.get('elapsed_s', 0):.1f}s total · {run.get('avg_latency_ms', 0):.0f}ms per look",
        f"{run.get('calls', 0)} API calls · {run.get('input_tokens', 0):,} tokens",
        f"${run.get('cost_usd', 0):.4f}",
    ]
    for position, line in enumerate(lines):
        text(draw, (width // 2, height // 2 + 60 + position * 46), line, load_font(int(width * 0.022)), fill=(200, 200, 210), anchor="mm")
    return canvas


# -- geometry helpers ----------------------------------------------------

def _grid_of(view: RoundView) -> tuple[int, int]:
    tiles = [c["tile"] for c in view.calls]
    if not tiles:
        return 1, 1
    return max(int(t[3:]) for t in tiles), max(int(t[1]) for t in tiles)


def _tile_colrow(name: str) -> tuple[int, int]:
    return int(name[3:]), int(name[1])


def _clock(seconds: float) -> str:
    minutes, rest = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):02d}:{rest:05.2f}"


# -- recorded footage ("record now, draw later") -------------------------

@dataclass
class Moment:
    t: float
    status: str
    round: int
    page: int
    origin: tuple[float, float]
    current: dict | None
    current_t: float
    found: list[dict]
    tiles: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    clicks: list = field(default_factory=list)


def film_available(out_dir: Path) -> bool:
    return (Path(out_dir) / "film" / "times.jsonl").exists()


def read_events(out_dir: Path) -> list[dict]:
    path = Path(out_dir) / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_timeline(events: list[dict]) -> list[Moment]:
    """Fold the event log into the on-screen state after every meaningful event."""
    moments: list[Moment] = []
    found: list[dict] = []
    current: dict | None = None
    current_t = 0.0
    status = "starting"
    round_no = 0
    page = 1
    origin: tuple[float, float] = (0.0, 0.0)
    tiles: dict = {}
    calls: list[dict] = []
    clicks: list[dict] = []

    for event in sorted(events, key=lambda e: e.get("t", 0.0)):
        kind = event.get("event")
        t = event.get("t", 0.0)
        if kind == "round_start":
            round_no = int(event.get("round", round_no))
            page = int(event.get("page", page))
            tiles = event.get("tiles") or {}
            calls = []
            status, current = "looking", None
        elif kind == "call":
            if event.get("round") == round_no:
                calls = calls + [event]
        elif kind == "pick":
            current, current_t, status = event.get("box"), t, "aiming"
        elif kind == "click":
            clicks = [c for c in clicks if t - c["t"] < 2.0] + [event]
        elif kind == "pan":
            origin = tuple(event.get("origin", origin))
            status = "scrolling"
        elif kind == "round_end":
            if event.get("found"):
                found.append({"box": event.get("box"), "origin": tuple(event.get("origin", origin))})
                current, current_t = event.get("box"), t
                status = "hit" if event.get("click_confirmed") else ("miss" if event.get("click_confirmed") is False else "found")
            else:
                status, current = "nothing here", None
        else:
            continue
        moments.append(Moment(t, status, round_no, page, origin, current, current_t, list(found), tiles, list(calls), list(clicks)))
    return moments


def state_at(moments: list[Moment], t: float) -> Moment | None:
    lo, hi = 0, len(moments) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if moments[mid].t <= t:
            best = moments[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def render_film(
    out_dir: Path,
    *,
    aspect: str = "16:9",
    fps: int = 15,
    size: tuple[int, int] | None = None,
) -> Path | None:
    """Draw boxes over the recorded footage. The footage stays untouched."""
    out_dir = Path(out_dir)
    run = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))
    moments = build_timeline(read_events(out_dir))
    size = size or SIZES.get(aspect, SIZES["16:9"])

    times = [json.loads(line) for line in (out_dir / "film" / "times.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if not times:
        raise RuntimeError("no recorded frames in film/times.jsonl")

    started = float(run.get("started_perf") or 0.0)
    if started <= 0:
        started = times[0]["t"] - (moments[0].t if moments else 0.0)
    source_t = [entry["t"] - started for entry in times]
    total_source = len(times)

    counter_rect = None
    for event in read_events(out_dir):
        if event.get("event") == "run_start":
            counter_rect = (event.get("settings") or {}).get("counter_rect")
            break

    frames_dir = out_dir / "video_frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)

    duration = source_t[-1] - source_t[0]
    count = max(1, int(duration * fps))
    rounds_total = max(1, run.get("rounds", 1))
    cache: dict[int, tuple[Image.Image, Rect]] = {}
    emitted = 0

    for j in range(count):
        t_rel = source_t[0] + j / fps
        idx = _nearest_source(source_t, t_rel)
        if idx not in cache:
            raw = Image.open(out_dir / "film" / f"{idx:06d}.jpg").convert("RGB")
            cache.clear()
            cache[idx] = _base(raw, size)
        base, rect = cache[idx]
        canvas = base.copy()

        moment = state_at(moments, t_rel)
        if moment is not None:
            canvas = _draw_film_state(canvas, rect, moment, t_rel, counter_rect, rounds_total)
        else:
            canvas = _hud(canvas, _clock(max(0.0, t_rel)), 1, rounds_total, 0, "starting")

        canvas.save(frames_dir / f"{emitted:05d}.jpg", "JPEG", quality=92)
        emitted += 1

    video = out_dir / "video.mp4"
    encoder = find_ffmpeg()
    if encoder is None:
        print(f"frames written to {frames_dir} ({emitted}). No ffmpeg found; install ffmpeg or use the frames elsewhere.")
        return None
    subprocess.run(
        [
            encoder, "-y", "-loglevel", "error", "-framerate", str(fps),
            "-i", str(frames_dir / "%05d.jpg"),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(video),
        ],
        check=True,
    )
    return video


def _nearest_source(source_t: list[float], t: float) -> int:
    lo, hi = 0, len(source_t) - 1
    if t <= source_t[0]:
        return 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if source_t[mid] <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _draw_activity(canvas: Image.Image, rect: Rect, moment: Moment, t: float) -> Image.Image:
    """Requests in flight and clicks, drawn from the log clock.

    A round is only visible in the log as a fan of `call` events that finish a
    few hundred milliseconds apart. Drawing their tiles from send to answer is
    what turns "the model found a frog" into "six looks happened in parallel".
    """
    calls = [c for c in moment.calls if c.get("tile") in moment.tiles]
    clicks = [c for c in moment.clicks if 0.0 <= t - c["t"] <= 0.5 and c.get("box")]
    if not calls and not clicks:
        return canvas

    newest = max((c["t"] for c in calls), default=-10.0)
    scanning = bool(calls) and t <= newest + 0.35
    if not scanning and not clicks:
        return canvas

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    k = canvas.width / 1920

    if scanning:
        for call in calls:
            tile = moment.tiles[call["tile"]]
            x0, y0, x1, y1 = _map({"left": tile[0], "top": tile[1], "right": tile[2], "bottom": tile[3]}, rect)
            if t >= call["t"]:
                # Amber means "this tile answered"; green is reserved for the chosen frog.
                draw.rectangle([x0, y0, x1, y1], outline=(255, 196, 92, 120), width=2)
                age = t - call["t"]
                if age <= 0.6:
                    alpha = int(235 * (1 - age / 0.6))
                    label = f'{call["latency_ms"] / 1000:.2f}s'
                    font = load_font(int(28 * k))
                    lx, ly = x0 + int(12 * k), y0 + int(10 * k)
                    bounds = draw.textbbox((lx, ly), label, font=font)
                    draw.rounded_rectangle(
                        [bounds[0] - int(8 * k), bounds[1] - int(4 * k), bounds[2] + int(8 * k), bounds[3] + int(4 * k)],
                        radius=int(6 * k),
                        fill=(10, 10, 12, int(alpha * 0.75)),
                    )
                    draw.text((lx, ly), label, font=font, fill=(255, 205, 120, alpha))
            else:
                draw.rectangle([x0, y0, x1, y1], outline=(255, 255, 255, 45), width=2)

    for click in clicks:
        age = t - click["t"]
        progress = age / 0.5
        box = click["box"]
        x0, y0, x1, y1 = _map(box, rect)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        radius = int((18 + 70 * progress) * k)
        draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            outline=(255, 255, 255, int(220 * (1 - progress))),
            width=max(2, int(3 * k)),
        )

    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


def _draw_film_state(
    canvas: Image.Image,
    rect: Rect,
    moment: Moment,
    t: float,
    counter_rect,
    rounds_total: int,
) -> Image.Image:
    canvas = _draw_activity(canvas, rect, moment, t)
    draw = ImageDraw.Draw(canvas)
    k = canvas.width / 1920

    for position, entry in enumerate(moment.found, start=1):
        if tuple(entry["origin"]) != moment.origin or not entry.get("box"):
            continue  # found on a page we have since scrolled away from
        x0, y0, x1, y1 = _map(entry["box"], rect)
        draw.rectangle([x0, y0, x1, y1], outline=RED, width=max(2, int(3 * k)))

    if moment.current:
        x0, y0, x1, y1 = _map(moment.current, rect)
        age = max(0.0, t - moment.current_t)
        ease = min(1.0, age / 0.22)
        pad = int((1 - ease) * 46 * k)
        ring = max(3, int(6 * k))
        draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], outline=GREEN, width=ring)
        if ease < 1.0:
            extra = int((1 - ease) * 16 * k)
            draw.rectangle([x0 - pad - extra, y0 - pad - extra, x1 + pad + extra, y1 + pad + extra], outline=GREEN, width=max(2, int(2 * k)))

    found_now = len(moment.found)
    hint = f"PAGE {moment.page} · {moment.status.upper()}" if moment.page > 1 else moment.status.upper()
    canvas = _hud(canvas, _clock(max(0.0, t)), max(1, moment.round), rounds_total, found_now, hint)

    if counter_rect and len(counter_rect) == 4:
        x, y, width, height = (int(v) for v in counter_rect)
        if 0 <= x < canvas.width and 0 <= y < canvas.height:
            crop = canvas.crop((x, y, min(canvas.width, x + width), min(canvas.height, y + height)))
            if crop.width > 4 and crop.height > 4:
                scale = int(70 * k) / crop.height
                resized = crop.resize((max(1, int(crop.width * scale)), int(70 * k)), Image.LANCZOS)
                ox = canvas.width - int(40 * k) - resized.width
                oy = int(90 * k)
                ImageDraw.Draw(canvas).rectangle(
                    [ox - 3, oy - 3, ox + resized.width + 3, oy + resized.height + 3], outline=AMBER, width=2
                )
                canvas.paste(resized, (ox, oy))
    return canvas
