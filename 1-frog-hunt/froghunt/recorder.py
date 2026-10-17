"""In-process screen recording.

A background thread grabs the game window at a fixed rate and writes JPEG
frames plus a timestamp for each. This is the "record the screen, draw the
overlay later" path: nothing is painted during the run, so the hunt loop pays
no composition cost and the measurement stays clean.

The timestamps are absolute `perf_counter()` values, the same clock the hunt
uses, so post-production lines the boxes up with the footage without guessing.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from PIL import Image


class Recorder:
    def __init__(self, grab: Callable[..., Image.Image], out_dir: Path, *, fps: float = 8.0, quality: int = 88):
        self.grab = grab
        self.dir = Path(out_dir) / "film"
        self.fps = max(1.0, float(fps))
        self.quality = quality
        self.frames = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._times = None
        self._error: Exception | None = None

    def start(self) -> "Recorder":
        self.dir.mkdir(parents=True, exist_ok=True)
        for stale in self.dir.glob("*.jpg"):
            stale.unlink()
        self._times = (self.dir / "times.jsonl").open("w", encoding="utf-8")
        self._thread = threading.Thread(target=self._loop, name="recorder", daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        interval = 1.0 / self.fps
        next_at = time.perf_counter()
        index = 0
        try:
            while not self._stop.is_set():
                captured_at = time.perf_counter()
                try:
                    frame = self.grab(draw_cursor=True)
                except Exception as exc:  # a dropped frame must not kill the recorder
                    self._error = exc
                    self._stop.wait(interval)
                    continue
                self._times.write(json.dumps({"i": index, "t": round(captured_at, 4)}) + "\n")
                self._times.flush()
                frame.save(self.dir / f"{index:06d}.jpg", "JPEG", quality=self.quality)
                self.frames = index + 1
                index += 1
                next_at += interval
                delay = next_at - time.perf_counter()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    next_at = time.perf_counter()
        finally:
            if self._times is not None:
                self._times.close()

    def stop(self) -> int:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        return self.frames

    @property
    def error(self) -> Exception | None:
        return self._error
