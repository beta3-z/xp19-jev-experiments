"""The hunt loop: screenshot -> tiles -> parallel Jev calls -> one frog -> click.

Coordinates have two levels:
  * viewport pixels — what is on screen right now;
  * scene pixels   — a virtual canvas that the viewport slides over when the
    game scrolls. Everything Jev finds is stored in scene pixels, so boxes stay
    glued to the artwork instead of to the screen.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageStat

from . import win32
from .imaging import (
    AMBER,
    GREEN,
    MAX_SIDE,
    RED,
    WHITE,
    Box,
    Rect,
    Tile,
    box_to_scene,
    default_grid,
    draw_marker,
    estimate_view_shift,
    load_font,
    make_grid,
    prepare_tile,
    rect_iou,
    scene_to_viewport,
    text,
)
from .typesafe import PRICE_PER_MTOKEN, Detection


@dataclass
class Settings:
    target: str = "frog"
    max_frogs: int = 3
    total_frogs: int = 100
    already_found: int = 0
    min_present: float = 0.40
    min_confidence: float = 0.0
    max_iou: float = 0.25
    cols: int | None = None
    rows: int | None = None
    click: bool = False
    click_delay: float = 0.30
    mask: bool = True
    delay_s: float = 0.15
    stop_on_miss: bool = False
    miss_before_pan: int = 2
    max_pages: int = 30
    pan_step: float = 0.55
    pan_settle_s: float = 0.55
    tile_overlap: float = 0.12
    counter_rect: tuple[int, int, int, int] | None = None
    max_runtime_s: float = 0.0
    max_side: int = MAX_SIDE


@dataclass
class Found:
    index: int
    rect: Rect


@dataclass
class Candidate:
    tile: Tile
    detection: Detection
    box: Box
    rect: Rect
    score: float


@dataclass
class RoundResult:
    index: int
    found: bool
    page: int = 1
    tile: str = ""
    box: dict | None = None
    present: float = 0.0
    confidence: float | None = None
    latency_ms: float = 0.0
    score: float = 0.0
    clicked: bool = False
    click_confirmed: bool | None = None
    tiles_asked: int = 0
    at_s: float = 0.0
    origin: tuple[float, float] = (0.0, 0.0)


@dataclass
class RunSummary:
    model: str
    started_at: str
    elapsed_s: float = 0.0
    rounds: int = 0
    found: int = 0
    misses: int = 0
    pages: int = 0
    calls: int = 0
    input_tokens: int = 0
    cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    started_perf: float = 0.0
    stopped_by: str = ""
    results: list[RoundResult] = field(default_factory=list)


class Hunter:
    def __init__(
        self,
        client,
        grab: Callable[[], Image.Image],
        out_dir: Path,
        settings: Settings | None = None,
        window: win32.Window | None = None,
        overlay=None,
        kill=None,
        pan: Callable[[str, int], None] | None = None,
    ):
        self.client = client
        self.grab = grab
        self.out = Path(out_dir)
        self.settings = settings or Settings()
        self.window = window
        self.overlay = overlay
        self.kill = kill
        self.pan = pan

        self.found: list[Found] = []
        self.origin_x = 0.0
        self.origin_y = 0.0
        self.pages = 1
        self._axis = "x"
        self._direction = 1
        self._viewport = (0, 0)
        self._counter_image: Image.Image | None = None
        self._active_tiles: dict[str, Tile] = {}

        self.started = time.perf_counter()
        self.calls = 0
        self.tokens = 0
        self.latencies: list[float] = []
        self.results: list[RoundResult] = []
        self.stop_reason = ""

        (self.out / "shots").mkdir(parents=True, exist_ok=True)
        (self.out / "frames").mkdir(parents=True, exist_ok=True)
        self._events = (self.out / "events.jsonl").open("w", encoding="utf-8")

    # -- plumbing --------------------------------------------------------
    @property
    def _origin(self) -> tuple[float, float]:
        return (self.origin_x, self.origin_y)

    def _log(self, event: str, **fields) -> None:
        record = {"t": round(time.perf_counter() - self.started, 3), "event": event, **fields}
        self._events.write(json.dumps(record) + "\n")
        self._events.flush()

    def close(self) -> None:
        self._events.close()

    def _stopped(self) -> bool:
        if self.kill is not None and self.kill.triggered:
            self.stop_reason = "kill switch"
            return True
        if self.settings.max_runtime_s and time.perf_counter() - self.started > self.settings.max_runtime_s:
            self.stop_reason = "max runtime"
            return True
        return False

    def _grab(self) -> Image.Image:
        """Capture without the overlay baked into the frame."""
        if self.overlay is not None and self.overlay.alive:
            self.overlay.hide()
        try:
            shot = self.grab()
        finally:
            if self.overlay is not None and self.overlay.alive:
                self.overlay.show()
        self._viewport = shot.size
        return shot

    # -- main loop -------------------------------------------------------
    def run(self) -> RunSummary:
        self._log("run_start", settings=asdict(self.settings))
        print(f"model {getattr(self.client, 'model', '?')} · target={self.settings.target!r}")

        try:
            misses = 0
            index = 0
            while len(self.found) < self.settings.max_frogs:
                if self._stopped():
                    self._announce("STOPPED")
                    break
                index += 1
                result = self._round(index)
                self.results.append(result)
                self._report(result)

                if result.found:
                    misses = 0
                else:
                    misses += 1
                    if self.settings.stop_on_miss:
                        break
                    if misses >= self.settings.miss_before_pan:
                        if not self._advance():
                            print("whole scene covered - nothing left to scroll to")
                            self.stop_reason = "scene exhausted"
                            break
                        misses = 0
                time.sleep(self.settings.delay_s)
        finally:
            if not self.stop_reason:
                self.stop_reason = "max frogs"
            elapsed = time.perf_counter() - self.started
            summary = RunSummary(
                model=str(getattr(self.client, "model", "")),
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                elapsed_s=round(elapsed, 2),
                rounds=len(self.results),
                found=sum(1 for r in self.results if r.found),
                misses=sum(1 for r in self.results if not r.found),
                pages=self.pages,
                calls=self.calls,
                input_tokens=self.tokens,
                cost_usd=round(self.tokens / 1_000_000 * PRICE_PER_MTOKEN, 5),
                avg_latency_ms=round(sum(self.latencies) / len(self.latencies), 1) if self.latencies else 0.0,
                started_perf=round(self.started, 6),
                stopped_by=self.stop_reason,
                results=self.results,
            )
            (self.out / "run.json").write_text(json.dumps(asdict(summary), indent=2), encoding="utf-8")
            self._log("run_end", elapsed_s=summary.elapsed_s, found=summary.found, stopped_by=self.stop_reason)
            self.close()
        return summary

    def _announce(self, status: str) -> None:
        if self.overlay is None or not self.overlay.alive:
            return
        self.overlay.draw(self._compose(status, None, None))
        time.sleep(0.7)

    def _show(self, status: str, current: Box | None = None, target: tuple[float, float] | None = None, flash: bool = False) -> None:
        """Paint the overlay, but only pay for composition when it is visible."""
        if self.overlay is None or not self.overlay.alive:
            return
        try:
            self.overlay.draw(self._compose(status, current, target, flash))
        except Exception as exc:
            print(f"  overlay error: {exc}")

    # -- one look --------------------------------------------------------
    def _round(self, index: int) -> RoundResult:
        shot = self._grab()
        width, height = shot.size
        counter_before = self._counter_crop(shot)

        work = shot.copy()
        if self.settings.mask:
            for found in self.found:
                viewport = scene_to_viewport(found.rect, self._origin, (width, height))
                if viewport is not None:
                    ring = Box.from_pixels(*viewport, width, height)
                    draw_marker(ImageDraw.Draw(work), ring, work.size, label=str(found.index))

        cols, rows = self._grid(width, height)
        tiles = make_grid(cols, rows, self.settings.tile_overlap)
        self._active_tiles = {tile.name: tile for tile in tiles}

        self._show("looking")
        self._log(
            "round_start",
            round=index,
            cols=cols,
            rows=rows,
            found=len(self.found),
            page=self.pages,
            # Geometry for the post-production overlay: no need to re-derive the grid.
            tiles={tile.name: [round(tile.box.left, 4), round(tile.box.top, 4), round(tile.box.right, 4), round(tile.box.bottom, 4)] for tile in tiles},
        )
        detections: dict[str, Detection] = {}

        with ThreadPoolExecutor(max_workers=len(tiles)) as pool:
            # Encode and request in the same worker: the first tile is on the wire
            # while the others are still being resized.
            futures = {pool.submit(self._ask_one, work, tile): tile for tile in tiles}
            for future in as_completed(futures):
                tile = futures[future]
                try:
                    detection = future.result()
                except Exception as exc:  # one dead tile must not kill the round
                    self._log("call_error", round=index, tile=tile.name, error=str(exc))
                    print(f"  ! {tile.name}: {exc}")
                    continue
                detections[tile.name] = detection
                self.calls += 1
                self.tokens += detection.input_tokens
                self.latencies.append(detection.latency_ms)
                self._log(
                    "call",
                    round=index,
                    tile=tile.name,
                    latency_ms=round(detection.latency_ms, 1),
                    present=round(detection.present, 3),
                    confidence=detection.confidence,
                    box=detection.box.as_dict() if detection.box else None,
                )

        candidates = self._rank(detections)
        if not candidates:
            composed = self._compose("nothing here", None, None)
            self._push(composed)
            self._save_frame(index, shot, composed)
            self._log("round_end", round=index, found=False, page=self.pages)
            return RoundResult(index=index, found=False, page=self.pages, tiles_asked=len(detections), at_s=round(time.perf_counter() - self.started, 2), origin=self._origin)

        best = candidates[0]
        centre = best.box.center()
        self._show("target", best.box, centre)
        self._log("pick", round=index, page=self.pages, tile=best.tile.name, score=round(best.score, 3), box=best.box.as_dict())

        clicked = False
        confirmed: bool | None = None
        if self.settings.click and self.window is not None:
            time.sleep(self.settings.click_delay)
            if self._stopped():
                return RoundResult(index=index, found=False, page=self.pages, at_s=round(time.perf_counter() - self.started, 2), origin=self._origin)
            clicked, confirmed = self._click_and_check(best.box, shot, counter_before)

        self.found.append(Found(index=len(self.found) + 1, rect=best.rect))
        if hasattr(self.client, "note_found"):
            self.client.note_found(best.box)

        verdict = "hit" if confirmed else ("miss" if confirmed is False else "clicked")
        composed = self._compose(f"{verdict}" if clicked else "found", best.box, centre, flash=clicked)
        self._push(composed)
        self._save_frame(index, shot, composed)
        self._log(
            "round_end",
            round=index,
            found=True,
            page=self.pages,
            tile=best.tile.name,
            box=best.box.as_dict(),
            present=round(best.detection.present, 3),
            confidence=best.detection.confidence,
            latency_ms=round(best.detection.latency_ms, 1),
            score=round(best.score, 3),
            clicked=clicked,
            click_confirmed=confirmed,
            origin=self._origin,
        )
        return RoundResult(
            index=index,
            found=True,
            page=self.pages,
            tile=best.tile.name,
            box=best.box.as_dict(),
            present=round(best.detection.present, 3),
            confidence=best.detection.confidence,
            latency_ms=round(best.detection.latency_ms, 1),
            score=round(best.score, 3),
            clicked=clicked,
            click_confirmed=confirmed,
            tiles_asked=len(detections),
            at_s=round(time.perf_counter() - self.started, 2),
            origin=self._origin,
        )

    def _grid(self, width: int, height: int) -> tuple[int, int]:
        if self.settings.cols and self.settings.rows:
            return self.settings.cols, self.settings.rows
        return default_grid(width, height)

    def _ask_one(self, work: Image.Image, tile: Tile) -> Detection:
        png = prepare_tile(work, tile, self.settings.max_side)
        return self.client.ask(png, tile=tile, target=self.settings.target)

    def _rank(self, detections: dict[str, Detection]) -> list[Candidate]:
        candidates: list[Candidate] = []
        for name, detection in detections.items():
            if detection.box is None or detection.present < self.settings.min_present:
                continue
            if detection.confidence is not None and detection.confidence < self.settings.min_confidence:
                continue
            tile = self._active_tiles[name]
            box = tile.to_global(detection.box)
            # A sliver is unclickable and usually an artefact: reject it outright.
            px0, py0, px1, py1 = box.to_pixels(*self._viewport)
            if px1 - px0 < 8 or py1 - py0 < 8:
                continue
            rect = box_to_scene(box, self._origin, self._viewport)
            if any(rect_iou(rect, found.rect) > self.settings.max_iou for found in self.found):
                continue
            score = detection.present * (detection.confidence if detection.confidence is not None else 0.5)
            if box.area() > 0.5:  # a tile-sized answer is not a real box
                score *= 0.4
            local = detection.box
            if local.left < 0.02 or local.top < 0.02 or local.right > 0.98 or local.bottom > 0.98:
                score *= 0.7  # touches the tile border: probably a clipped frog
            candidates.append(Candidate(tile=tile, detection=detection, box=box, rect=rect, score=score))
        return sorted(candidates, key=lambda c: c.score, reverse=True)

    # -- scrolling -------------------------------------------------------
    def _advance(self) -> bool:
        if self.pan is None:
            return False
        if self.pages >= self.settings.max_pages:
            return False
        if self._pan_once():
            return True
        if self._axis == "x":
            self._axis, self._direction = "y", 1
            if self._pan_once():
                return True
        return False

    def _pan_once(self) -> bool:
        assert self.pan is not None
        if self._stopped():
            return False
        before = self._grab()
        self._show("scrolling")
        self.pan(self._axis, self._direction)
        time.sleep(self.settings.pan_settle_s)
        after = self._grab()

        shift = estimate_view_shift(before, after)
        if shift is None:
            distance = self.settings.pan_step * (before.width if self._axis == "x" else before.height)
            shift = (distance * self._direction, 0.0) if self._axis == "x" else (0.0, distance * self._direction)
            print("  (numpy missing - assuming the scroll moved as commanded)")

        dx, dy = shift
        if abs(dx) + abs(dy) < 8:
            self._log("pan_edge", axis=self._axis)
            return False

        self.origin_x += dx
        self.origin_y += dy
        self.pages += 1
        self._log("pan", axis=self._axis, dx=round(dx, 1), dy=round(dy, 1), origin=(round(self.origin_x, 1), round(self.origin_y, 1)))
        print(f"  scrolled {dx:+.0f},{dy:+.0f} px -> scene origin {self.origin_x:.0f},{self.origin_y:.0f}")
        return True

    # -- clicking --------------------------------------------------------
    def _click_and_check(self, box: Box, before: Image.Image, counter_before: Image.Image | None) -> tuple[bool, bool | None]:
        assert self.window is not None
        x, y, width, height = self.window.client_rect()
        cx, cy = box.center()
        px, py = x + int(cx * width), y + int(cy * height)
        restore = win32.cursor_pos()
        changed = False
        try:
            self.window.focus()
            for dx, dy in ((0, 0), (-12, 0), (12, 0), (0, -12), (0, 12)):
                if self._stopped():
                    break
                win32.click(px + dx, py + dy)
                time.sleep(0.30)
                after = self._grab()
                if counter_before is not None:
                    counter_after = self._counter_crop(after)
                    changed = counter_after is not None and _mean_abs_diff(counter_before, counter_after) > 2.0
                else:
                    changed = self._changed(before, after, box, width, height)
                self._log("click", x=px + dx, y=py + dy, changed=changed, box=box.as_dict())
                if changed:
                    break
        finally:
            win32.user32.SetCursorPos(*restore)
        return True, changed

    @staticmethod
    def _changed(before: Image.Image, after: Image.Image, box: Box, width: int, height: int) -> bool:
        if before.size != after.size:
            return True
        x0, y0, x1, y1 = box.to_pixels(*before.size)
        pad_x = max(24, (x1 - x0) // 2)
        pad_y = max(24, (y1 - y0) // 2)
        region = (max(0, x0 - pad_x), max(0, y0 - pad_y), min(before.width, x1 + pad_x), min(before.height, y1 + pad_y))
        control = (0, 0, max(8, width // 10), max(8, height // 10))
        delta = _mean_abs_diff(before.crop(region), after.crop(region))
        noise = _mean_abs_diff(before.crop(control), after.crop(control))
        return delta > max(3.0, noise * 2.5 + 1.5)

    def _counter_crop(self, shot: Image.Image) -> Image.Image | None:
        rect = self.settings.counter_rect
        if not rect:
            return None
        x, y, width, height = rect
        box = (max(0, x), max(0, y), min(shot.width, x + width), min(shot.height, y + height))
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            return None
        crop = shot.crop(box)
        self._counter_image = crop
        return crop

    # -- drawing ---------------------------------------------------------
    def _compose(self, status: str, current: Box | None, target: tuple[float, float] | None, flash: bool = False) -> Image.Image:
        width, height = self._viewport
        if width <= 0:
            return Image.new("RGBA", (1, 1))
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        k = max(0.55, width / 1920)
        px = lambda v: int(v * k)  # noqa: E731

        for found in self.found:
            viewport = scene_to_viewport(found.rect, self._origin, (width, height))
            if viewport is None:
                continue
            draw.rectangle(viewport, outline=(*RED, 235), width=max(2, px(3)))
            if viewport[2] - viewport[0] > px(46):
                text(draw, (viewport[0] + 6, viewport[1] + 4), f"#{found.index}", load_font(px(22)), fill=(*RED, 255))

        if current is not None:
            x0, y0, x1, y1 = current.to_pixels(width, height)
            colour = (255, 255, 255, 255) if flash else (*GREEN, 255)
            draw.rectangle([x0, y0, x1, y1], outline=colour, width=max(3, px(5)))
            if target is not None:
                tx, ty = target[0] * width, target[1] * height
                arm = px(18)
                draw.line([(tx - arm, ty), (tx + arm, ty)], fill=colour, width=max(2, px(2)))
                draw.line([(tx, ty - arm), (tx, ty + arm)], fill=colour, width=max(2, px(2)))

        total = self.settings.total_frogs
        count = self.settings.already_found + len(self.found)
        plate = [px(22), px(18), px(22) + px(360), px(18) + px(150)]
        draw.rounded_rectangle(plate, radius=px(16), fill=(6, 6, 8, 175))
        text(draw, (px(42), px(30)), f"{count}/{total}", load_font(px(64)), fill=(*WHITE, 255))
        text(draw, (px(44), px(104)), f"round {len(self.results) + 1} · page {self.pages}", load_font(px(26)), fill=(200, 200, 210, 255))

        clock = _clock(time.perf_counter() - self.started)
        text(draw, (width - px(40), px(24)), clock, load_font(px(54)), fill=(*WHITE, 255), anchor="ra")

        if self._counter_image is not None:
            crop = self._counter_image
            scale = px(70) / max(1, crop.height)
            resized = crop.resize((max(1, int(crop.width * scale)), px(70)), Image.LANCZOS)
            ox = width - px(40) - resized.width
            oy = px(90)
            draw.rectangle([ox - 3, oy - 3, ox + resized.width + 3, oy + resized.height + 3], outline=(*AMBER, 220), width=2)
            canvas.paste(resized, (ox, oy))

        text(draw, (px(42), height - px(46)), status.upper(), load_font(px(30)), fill=(*AMBER, 255))
        text(draw, (width - px(40), height - px(46)), "F8  STOP", load_font(px(26)), fill=(230, 230, 235, 210), anchor="ra")
        return canvas

    def _push(self, composed: Image.Image) -> None:
        if self.overlay is None or not self.overlay.alive:
            return
        try:
            self.overlay.draw(composed)
        except Exception as exc:
            print(f"  overlay error: {exc}")

    def _save_frame(self, index: int, shot: Image.Image, composed: Image.Image) -> None:
        # JPEG keeps a 100-round run small and writes ~10x faster than PNG.
        shot.save(self.out / "shots" / f"{index:03d}_raw.jpg", "JPEG", quality=92)
        merged = Image.alpha_composite(shot.convert("RGBA"), composed).convert("RGB")
        merged.save(self.out / "frames" / f"{index:03d}_ann.jpg", "JPEG", quality=92)

    def _report(self, result: RoundResult) -> None:
        elapsed = time.perf_counter() - self.started
        if result.found:
            mark = "OK " if result.click_confirmed in (True, None) else "?? "
            click = "" if not result.clicked else f" click={'hit' if result.click_confirmed else 'miss'}"
            print(
                f"[{elapsed:6.2f}s] {mark}#{result.index} {result.tile} page {result.page} "
                f"p={result.present:.2f} conf={result.confidence or 0:.2f} {result.latency_ms:.0f}ms{click}"
            )
        else:
            print(f"[{elapsed:6.2f}s]  .  #{result.index} nothing above threshold (page {result.page})")


def _mean_abs_diff(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        return 255.0
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    return sum(ImageStat.Stat(diff).mean) / 3


def _clock(seconds: float) -> str:
    minutes, rest = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):02d}:{rest:05.2f}"
