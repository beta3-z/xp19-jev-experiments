"""Command line entry point."""
from __future__ import annotations

import argparse
import io
import math
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from . import win32
from .envfile import load_env
from .hotkey import KillSwitch
from .hunt import Hunter, Settings
from .imaging import Box, Rect, estimate_view_shift, rect_iou
from .overlay import Overlay
from .recorder import Recorder
from .render import film_available, render, render_film
from .typesafe import Client, MockClient, TypeSafeError

ROOT = Path(__file__).resolve().parent.parent
ARROWS = {(0, 1): 0x27, (0, -1): 0x25, (1, 1): 0x28, (1, -1): 0x26}


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    load_env(ROOT / ".env")

    parser = argparse.ArgumentParser(prog="froghunt", description="Find hidden frogs with TypeSafe Jev")
    sub = parser.add_subparsers(dest="command", required=True)

    grab = sub.add_parser("grab", help="save a screenshot of the game window")
    grab.add_argument("--window", default="hidden frogs")
    grab.add_argument("-o", "--out", default="shot.png")

    doctor = sub.add_parser("doctor", help="check the API key and Box support on a synthetic image")
    doctor.add_argument("--check-input", action="store_true", help="also verify that synthetic mouse input works")
    _api_args(doctor)

    run = sub.add_parser("run", help="run the hunt loop")
    run.add_argument("--window", default="hidden frogs")
    run.add_argument("--image", help="use a saved screenshot instead of the live window")
    run.add_argument("--max-frogs", type=int, default=3)
    run.add_argument("--total-frogs", type=int, default=100)
    run.add_argument("--already-found", type=int, default=0, help="counter value before the run")
    run.add_argument("--cols", type=int)
    run.add_argument("--rows", type=int)
    run.add_argument("--click", action="store_true", help="click the box and check that the game reacted")
    run.add_argument("--click-delay", type=float, default=0.30)
    run.add_argument("--no-mask", action="store_true", help="do not ring already-found frogs")
    run.add_argument("--stop-on-miss", action="store_true")
    run.add_argument("--target", default="frog")
    run.add_argument("--min-present", type=float, default=0.40)
    run.add_argument("--mock", action="store_true", help="offline fake model, for pipeline testing")
    run.add_argument("--overlay", action=argparse.BooleanOptionalAction, default=False, help="draw boxes live (costs ~40ms per redraw)")
    run.add_argument("--record", action="store_true", help="record the screen to film/ and burn boxes in later")
    run.add_argument("--record-fps", type=float, default=8.0)
    run.add_argument("--kill-key", default="f8")
    run.add_argument("--pan", default="wheel", choices=["none", "wheel", "drag", "keys", "edge"])
    run.add_argument("--pan-axis", default="x", choices=["x", "y"], help="axis to scroll first")
    run.add_argument("--pan-hold", type=float, default=2.5, help="edge pan: max seconds to hold the cursor at the edge")
    run.add_argument("--detect-title", action=argparse.BooleanOptionalAction, default=False, help="press START when the title screen shows")
    run.add_argument("--pan-step", type=float, default=0.55, help="fraction of the viewport to scroll")
    run.add_argument("--pan-notches", type=float, default=3.0)
    run.add_argument("--miss-before-pan", type=int, default=2)
    run.add_argument("--max-pages", type=int, default=30)
    run.add_argument("--counter-rect", help="x,y,w,h of the in-game counter, in window coordinates")
    run.add_argument("--max-runtime", type=float, default=0.0, help="hard stop after N seconds")
    run.add_argument("--out")
    _api_args(run)

    video = sub.add_parser("render", help="turn a run folder into a video")
    video.add_argument("run_dir")
    video.add_argument("--aspect", default="16:9", choices=["16:9", "9:16", "1:1"])
    video.add_argument("--fps", type=int, default=15)
    video.add_argument("--film", choices=["auto", "on", "off"], default="auto")

    selftest = sub.add_parser("selftest", help="end-to-end check with a synthetic world, no API needed")
    selftest.add_argument("--out", default=str(ROOT / "runs" / "selftest"))

    args = parser.parse_args(argv)

    try:
        if args.command == "grab":
            return _cmd_grab(args)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "render":
            return _cmd_render(args)
        if args.command == "selftest":
            return _cmd_selftest(args)
    except (LookupError, OSError, RuntimeError, TypeSafeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 1


def _api_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-key", help="defaults to $TYPESAFE_API_KEY")
    parser.add_argument("--model", default="jev-1.14.0")
    parser.add_argument("--timeout", type=float, default=90.0)


def _cmd_grab(args) -> int:
    win32.enable_dpi_awareness()
    window = win32.find_window(args.window)
    image = window.grab()
    out = Path(args.out)
    image.save(out)
    x, y, width, height = window.client_rect()
    print(f"{window.title!r} at {x},{y} {width}x{height} -> {out} ({image.width}x{image.height})")
    return 0


def _cmd_doctor(args) -> int:
    key = args.api_key or os.environ.get("TYPESAFE_API_KEY")
    print(f"api key        {'--api-key' if args.api_key else ('TYPESAFE_API_KEY' if key else 'MISSING - put it in .env')}")
    if args.check_input:
        works = _input_probe()
        print(f"mouse input    {'OK - SendInput reaches the desktop' if works else 'BLOCKED - clicks/hotkeys will not work here'}")
        if not key:
            return 0 if works else 4
    client = Client(api_key=args.api_key, model=args.model, timeout=args.timeout)
    image, truth = _probe_image()
    buf = io.BytesIO()
    image.save(buf, "PNG")
    started = time.perf_counter()
    detection = client.ask(buf.getvalue(), target="frog")
    total_ms = (time.perf_counter() - started) * 1000

    print(f"model          {detection.model or args.model}")
    print(f"round trip     {total_ms:.0f} ms")
    print(f"present        {detection.present:.2f}")
    print(f"confidence     {detection.confidence}")
    print(f"box            {detection.box.as_dict() if detection.box else None}")
    print(f"truth          {truth.as_dict()}")
    if detection.box:
        print(f"iou            {detection.box.iou(truth):.2f}")
    print(f"input tokens   {detection.input_tokens}")
    ok = detection.present > 0.5 and detection.box is not None and detection.box.iou(truth) > 0.15
    print("verdict        " + ("OK - box questions work" if ok else "WEAK - check model/header/limits"))
    return 0 if ok else 3


def _input_probe() -> bool:
    """Move the cursor 40 px with SendInput and see whether it actually moved."""
    before = win32.cursor_pos()
    win32.move_to(before[0] + 40, before[1])
    time.sleep(0.15)
    after = win32.cursor_pos()
    win32.user32.SetCursorPos(*before)
    return after != before


def _cmd_run(args) -> int:
    out = Path(args.out) if args.out else ROOT / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    source = None
    if args.image:
        source = Image.open(args.image).convert("RGB")
        grab = lambda: source.copy()  # noqa: E731
        window = None
    else:
        win32.enable_dpi_awareness()
        window = win32.find_window(args.window)
        window.focus()
        grab = window.grab

    overlay = None
    if args.overlay and window is not None:
        try:
            overlay = Overlay(window.client_rect())
        except OSError as exc:
            print(f"overlay unavailable: {exc}")

    recorder = None
    if args.record and window is not None:
        recorder = Recorder(window.grab, out, fps=args.record_fps).start()
        print(f"recording {args.record_fps:g} fps -> {out / 'film'}")
        if overlay is not None:
            print("warning: --overlay is on, so the live boxes will be baked into the recording and drawn again in post")

    kill = None
    if window is not None:
        stop_file = out / "STOP"
        kill = KillSwitch(args.kill_key, on_stop=win32.release_mouse, stop_file=stop_file).start()
        print(f"kill switch: {args.kill_key.upper()}, or create {stop_file}")

    pan = _make_pan(window, args) if (window is not None and args.pan != "none") else None

    if args.mock:
        if window is not None:
            _, _, width, height = window.client_rect()
        else:
            width, height = source.size
        client = MockClient(
            truths=_random_truths(width, height, 8),
            view=lambda: (0.0, 0.0, width, height),
        )
    else:
        client = Client(api_key=args.api_key, model=args.model, timeout=args.timeout)

    settings = Settings(
        target=args.target,
        max_frogs=args.max_frogs,
        total_frogs=args.total_frogs,
        already_found=args.already_found,
        min_present=args.min_present,
        cols=args.cols,
        rows=args.rows,
        click=args.click,
        click_delay=args.click_delay,
        mask=not args.no_mask,
        stop_on_miss=args.stop_on_miss,
        miss_before_pan=args.miss_before_pan,
        max_pages=args.max_pages,
        pan_step=args.pan_step,
        counter_rect=_parse_rect(args.counter_rect),
        max_runtime_s=args.max_runtime,
        detect_title=args.detect_title,
        pan_axis=args.pan_axis,
    )

    try:
        hunter = Hunter(client, grab, out, settings, window, overlay, kill, pan)
        summary = hunter.run()
        _print_summary(summary, out)
    finally:
        if recorder is not None:
            frames = recorder.stop()
            print(f"recorded {frames} frames" + (f" (last error: {recorder.error})" if recorder.error else ""))
        if kill is not None:
            kill.stop()
        if overlay is not None:
            overlay.close()
    return 0


def _cmd_render(args) -> int:
    run_dir = Path(args.run_dir)
    use_film = args.film == "on" or (args.film == "auto" and film_available(run_dir))
    if args.film == "on" and not film_available(run_dir):
        print("no recording found in film/; falling back to the still-based render")
        use_film = False
    path = render_film(run_dir, aspect=args.aspect, fps=args.fps) if use_film else render(run_dir, aspect=args.aspect, fps=args.fps)
    if path:
        print(f"video -> {path}")
    return 0


def _cmd_selftest(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    world_width, world_height = 2400, 1200
    view_width, view_height = 1200, 1200
    world, truths = synthetic_scene(world_width, world_height, frogs=8, seed=4)
    world.save(out / "world.png")
    state = {"x": 0.0, "y": 0.0}

    def view() -> tuple[float, float, int, int]:
        x = min(max(state["x"], 0.0), world_width - view_width)
        y = min(max(state["y"], 0.0), world_height - view_height)
        return float(x), float(y), view_width, view_height

    def grab() -> Image.Image:
        x, y, _, _ = view()
        return world.crop((int(x), int(y), int(x) + view_width, int(y) + view_height))

    def pan(axis: str, direction: int) -> None:
        step = 600 * direction
        if axis == "x":
            state["x"] += step
        else:
            state["y"] += step

    mock = MockClient(truths=truths, view=view, latency=(0.01, 0.03), seed=11)
    recorder = Recorder(lambda draw_cursor=False: grab(), out, fps=8).start()
    hunter = Hunter(
        mock,
        grab,
        out,
        Settings(max_frogs=len(truths), miss_before_pan=1, pan_settle_s=0.02, total_frogs=len(truths)),
        None,
        None,
        None,
        pan,
    )
    summary = hunter.run()
    frames = recorder.stop()
    _print_summary(summary, out)
    print(f"recorded {frames} frames")

    hits = 0
    for truth in truths:
        best = max((_iou_rect(result, truth, view_width, view_height) for result in summary.results), default=0.0)
        hits += best > 0.25

    print(f"geometry check {hits}/{len(truths)} frogs matched, pages={summary.pages}")
    if summary.pages < 2:
        print("FAIL: expected the run to scroll at least once")

    if frames:
        path = render_film(out, aspect="16:9", fps=12, size=(960, 540))
    else:
        path = render(out, aspect="16:9", fps=12, scan_s=0.5, reveal_s=0.4, hold_s=0.3, size=(960, 540))
    if path:
        print(f"video -> {path}")
    return 0 if hits == len(truths) and summary.pages >= 2 and frames > 0 else 1


def _iou_rect(result, truth: Rect, view_width: int, view_height: int) -> float:
    if not result.box:
        return 0.0
    box = Box(**result.box)
    rect = (
        result.origin[0] + box.left * view_width,
        result.origin[1] + box.top * view_height,
        result.origin[0] + box.right * view_width,
        result.origin[1] + box.bottom * view_height,
    )
    return rect_iou(rect, truth)


def _print_summary(summary, out: Path) -> None:
    print("-" * 58)
    print(f" model      {summary.model}")
    print(f" found      {summary.found}/{summary.rounds}")
    print(f" elapsed    {summary.elapsed_s:.1f} s")
    print(f" calls      {summary.calls} ({summary.avg_latency_ms:.0f} ms average)")
    print(f" pages      {summary.pages}")
    print(f" stopped    {summary.stopped_by}")
    print(f" tokens     {summary.input_tokens:,} (${summary.cost_usd:.4f})")
    print(f" output     {out}")


def _parse_rect(value: str | None) -> tuple[int, int, int, int] | None:
    if not value:
        return None
    parts = [int(p) for p in value.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise ValueError("--counter-rect expects x,y,w,h")
    return tuple(parts)  # type: ignore[return-value]


def _make_pan(window, args):
    def pan(axis: str, direction: int) -> None:
        if args.pan == "wheel":
            win32.wheel(-args.pan_notches * direction, horizontal=(axis == "x"))
        elif args.pan == "drag":
            x, y, width, height = window.client_rect()
            cx, cy = x + width // 2, y + height // 2
            distance = int((width if axis == "x" else height) * args.pan_step) * direction
            end = (cx + (distance if axis == "x" else 0), cy + (0 if axis == "x" else distance))
            win32.drag((cx, cy), end, button="middle")
        elif args.pan == "edge":
            x, y, width, height = window.client_rect()
            span = width if axis == "x" else height
            wanted = span * args.pan_step
            if axis == "x":
                point = (x + 6 if direction < 0 else x + width - 6, y + height // 2)
            else:
                point = (x + width // 2, y + 6 if direction < 0 else y + height - 6)
            home = (x + width // 2, y + height // 2)
            win32.move_to(*point)
            moved = 0.0
            previous = window.grab()
            deadline = time.time() + args.pan_hold
            try:
                while moved < wanted and time.time() < deadline:
                    time.sleep(0.12)
                    current = window.grab()
                    shift = estimate_view_shift(previous, current)
                    if shift:
                        moved += abs(shift[0] if axis == "x" else shift[1])
                    previous = current
            finally:
                win32.move_to(*home)
        else:
            vk = ARROWS[(0 if axis == "x" else 1, 1 if direction > 0 else -1)]
            for _ in range(max(1, int(args.pan_notches))):
                win32.press_key(vk)
                time.sleep(0.04)

    return pan


def _random_truths(width: int, height: int, count: int, seed: int = 5) -> list[Rect]:
    rng = random.Random(seed)
    rects: list[Rect] = []
    for _ in range(count):
        side = rng.randint(24, 48)
        left = rng.randint(20, max(21, width - side - 20))
        top = rng.randint(20, max(21, height - side - 20))
        rects.append((left, top, left + side, top + side))
    return rects


def _probe_image() -> tuple[Image.Image, Box]:
    """A 640x640 frame with one obvious frog, for `doctor`."""
    image = Image.new("RGB", (640, 640), (236, 236, 230))
    draw = ImageDraw.Draw(image)
    for i in range(0, 640, 40):
        draw.line([(0, i), (640, i - 120)], fill=(210, 210, 205), width=2)
    box = Box(0.60, 0.34, 0.72, 0.47)
    x0, y0, x1, y1 = box.to_pixels(640, 640)
    draw.ellipse([x0, y0, x1, y1], fill=(30, 34, 30))
    draw.ellipse([x0 + 12, y0 + 8, x0 + 26, y0 + 22], fill=(236, 236, 230))
    draw.ellipse([x1 - 26, y0 + 8, x1 - 12, y0 + 22], fill=(236, 236, 230))
    draw.ellipse([x0 + 17, y0 + 13, x0 + 21, y0 + 17], fill=(10, 10, 10))
    draw.ellipse([x1 - 21, y0 + 13, x1 - 17, y0 + 17], fill=(10, 10, 10))
    return image, box


def synthetic_scene(width: int = 1600, height: int = 900, frogs: int = 6, seed: int = 4) -> tuple[Image.Image, list[Rect]]:
    """A busy black-and-white scene with a few planted frogs, in pixel coords."""
    rng = random.Random(seed)
    image = Image.new("RGB", (width, height), (238, 238, 232))
    draw = ImageDraw.Draw(image)

    for _ in range(int(width * height / 2000)):
        x, y = rng.randrange(width), rng.randrange(height)
        length = rng.randrange(20, 120)
        angle = rng.uniform(0, math.pi)
        x2 = x + int(length * math.cos(angle))
        y2 = y + int(length * math.sin(angle))
        shade = rng.randrange(90, 200)
        draw.line([(x, y), (x2, y2)], fill=(shade, shade, shade), width=rng.choice([1, 1, 2, 3]))
    for _ in range(int(width * height / 12000)):
        x, y = rng.randrange(width), rng.randrange(height)
        radius = rng.randrange(6, 28)
        shade = rng.randrange(110, 210)
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=(shade, shade, shade))

    truths: list[Rect] = []
    for _ in range(frogs):
        frog_w, frog_h = rng.randrange(34, 52), rng.randrange(26, 38)
        left = rng.randrange(30, max(31, width - frog_w - 30))
        top = rng.randrange(30, max(31, height - frog_h - 30))
        _draw_frog(draw, left, top, frog_w, frog_h)
        truths.append((left, top, left + frog_w, top + frog_h))
    return image, truths


def _draw_frog(draw: ImageDraw.ImageDraw, left: int, top: int, width: int, height: int) -> None:
    dark = (26, 30, 26)
    draw.ellipse([left, top + height * 0.25, left + width, top + height], fill=dark)
    for offset in (width * 0.16, width * 0.84):
        cx = left + offset
        cy = top + height * 0.28
        draw.ellipse([cx - width * 0.1, cy - width * 0.1, cx + width * 0.1, cy + width * 0.1], fill=dark)
    draw.line([(left, top + height * 0.8), (left - width * 0.16, top + height * 1.02)], fill=dark, width=3)
    draw.line([(left + width, top + height * 0.8), (left + width * 1.16, top + height * 1.02)], fill=dark, width=3)


if __name__ == "__main__":
    raise SystemExit(main())
