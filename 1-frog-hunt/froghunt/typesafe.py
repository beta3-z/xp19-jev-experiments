"""TypeSafe Jev client: one image in, one box + one probability out.

The API is a single endpoint. Images ride along as multipart file parts and are
referenced from the JSON body by an opaque string. Box questions need Jev 1.14
and the danger-zone header.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .imaging import Box, Rect, Tile, box_to_scene, rect_iou

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.14.0"
DANGER_HEADER = "TypeSafe-Danger-Zone-Images"
PRICE_PER_MTOKEN = 0.042  # USD per 1M input tokens


class TypeSafeError(RuntimeError):
    def __init__(self, status: int | None, body: str):
        self.status = status
        self.body = body
        super().__init__(f"TypeSafe HTTP {status}: {body[:600]}")


@dataclass
class Detection:
    present: float
    box: Box | None
    confidence: float | None
    latency_ms: float
    input_tokens: int = 0
    model: str = ""
    attempts: int = 1


class Client:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        timeout: float = 90.0,
        retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("Set TYPESAFE_API_KEY or pass --api-key")
        self.model = model
        self.base_url = base_url or os.environ.get("TYPESAFE_BASE_URL", DEFAULT_URL)
        self.timeout = timeout
        self.retries = retries

    def note_found(self, box: Box) -> None:  # parity with MockClient
        pass

    def ask(self, image_png: bytes, *, tile: Tile | None = None, target: str = "frog") -> Detection:
        ref = uuid.uuid4().hex
        body = {
            "model": self.model,
            "state": {"screen": ref},
            "assets": {ref: {"type": "image", "part": "image"}},
            "questions": {
                "target_present": {
                    "type": "noul",
                    "instructions": f"Is there a {target} in `screen`?",
                },
                "target": {
                    "type": "box",
                    "instructions": f"the {target} in `screen`",
                },
            },
        }
        payload, boundary = _multipart(body, image_png)

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            started = time.perf_counter()
            try:
                data = self._post(payload, boundary)
                latency_ms = (time.perf_counter() - started) * 1000
                return _parse(data, latency_ms, attempt)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                last_error = TypeSafeError(exc.code, detail)
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    raise last_error from None
            except urllib.error.URLError as exc:
                last_error = TypeSafeError(
                    None, f"{exc.reason}. If this is a TLS error, the machine needs working outbound HTTPS."
                )
            except TimeoutError:
                last_error = TypeSafeError(None, f"request timed out after {self.timeout}s")
            time.sleep(0.4 * (2 ** (attempt - 1)))
        assert last_error is not None
        raise last_error

    def _post(self, payload: bytes, boundary: str) -> dict:
        request = urllib.request.Request(
            self.base_url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
                "User-Agent": "froghunt/0.1",
                DANGER_HEADER: "true",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def _multipart(body: dict, image_png: bytes) -> tuple[bytes, str]:
    boundary = f"----froghunt{uuid.uuid4().hex}"
    out = bytearray()

    def part(name: str, data: bytes, *, filename: str | None = None, content_type: str | None = None) -> None:
        out.extend(f"--{boundary}\r\n".encode())
        disposition = f'form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        out.extend(f"Content-Disposition: {disposition}\r\n".encode())
        if content_type:
            out.extend(f"Content-Type: {content_type}\r\n".encode())
        out.extend(b"\r\n")
        out.extend(data)
        out.extend(b"\r\n")

    # `request` must be the first part.
    part("request", json.dumps(body).encode(), content_type="application/json")
    part("image", image_png, filename="tile.png", content_type="image/png")
    out.extend(f"--{boundary}--\r\n".encode())
    return bytes(out), boundary


def _parse(data: dict, latency_ms: float, attempts: int) -> Detection:
    answers = data.get("answers") or {}
    present_answer = answers.get("target_present") or {}
    box_answer = answers.get("target") or {}
    present = float(present_answer.get("noul", 0.0))

    box: Box | None = None
    raw_box = box_answer.get("box")
    if isinstance(raw_box, dict):
        try:
            candidate = Box.from_api(raw_box)
            if candidate.valid():
                box = candidate.clamp()
        except (KeyError, TypeError, ValueError):
            box = None

    usage = data.get("usage") or {}
    return Detection(
        present=present,
        box=box,
        confidence=box_answer.get("confidence"),
        latency_ms=latency_ms,
        input_tokens=int(usage.get("input_tokens") or 0),
        model=str(data.get("model") or ""),
        attempts=attempts,
    )


@dataclass
class MockClient:
    """Offline stand-in so the pipeline can be exercised without an API key.

    Truths live in world pixels; `view()` reports the current viewport origin
    and size, so panning is simulated honestly.
    """

    truths: list[Rect] = field(default_factory=list)
    view: Callable[[], tuple[float, float, int, int]] = lambda: (0.0, 0.0, 0, 0)
    seed: int = 7
    miss_rate: float = 0.0
    latency: tuple[float, float] = (0.05, 0.15)
    found: list[Rect] = field(default_factory=list)
    model: str = "mock-jev"

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def note_found(self, box: Box) -> None:
        origin_x, origin_y, width, height = self.view()
        self.found.append(box_to_scene(box, (origin_x, origin_y), (width, height)))

    def ask(self, image_png: bytes, *, tile: Tile | None = None, target: str = "frog") -> Detection:
        started = time.perf_counter()
        time.sleep(self._rng.uniform(*self.latency))
        assert tile is not None, "MockClient needs the tile geometry"
        origin_x, origin_y, width, height = self.view()
        tx0, ty0, tx1, ty1 = box_to_scene(tile.box, (origin_x, origin_y), (width, height))
        span_x = max(1.0, tx1 - tx0)
        span_y = max(1.0, ty1 - ty0)

        def to_tile(rect: Rect) -> Box:
            return Box(
                (rect[0] - tx0) / span_x,
                (rect[1] - ty0) / span_y,
                (rect[2] - tx0) / span_x,
                (rect[3] - ty0) / span_y,
            ).clamp()

        def jitter(box: Box, amount: float = 0.012) -> Box:
            # Move the centre and resize a little. Jittering each edge on its own
            # can invert a small box and turn it into a sliver.
            cx, cy = box.center()
            cx += self._rng.uniform(-amount, amount)
            cy += self._rng.uniform(-amount, amount)
            half_w = max(0.004, (box.right - box.left) / 2 * self._rng.uniform(0.9, 1.1))
            half_h = max(0.004, (box.bottom - box.top) / 2 * self._rng.uniform(0.9, 1.1))
            return Box(cx - half_w, cy - half_h, cx + half_w, cy + half_h).clamp()

        inside = [
            truth
            for truth in self.truths
            if tx0 <= (truth[0] + truth[2]) / 2 <= tx1
            and ty0 <= (truth[1] + truth[3]) / 2 <= ty1
            and not any(rect_iou(truth, seen) > 0.25 for seen in self.found)
        ]

        latency_ms = (time.perf_counter() - started) * 1000
        if inside and self._rng.random() > self.miss_rate:
            pick = min(inside, key=lambda r: abs((r[0] + r[2]) / 2 - (tx0 + tx1) / 2))
            return Detection(
                present=self._rng.uniform(0.82, 0.98),
                box=jitter(to_tile(pick)),
                confidence=self._rng.uniform(0.62, 0.95),
                latency_ms=latency_ms,
                input_tokens=1300,
                model=self.model,
            )
        return Detection(
            present=self._rng.uniform(0.02, 0.2),
            box=jitter(Box(0.3, 0.3, 0.5, 0.5), 0.2),
            confidence=self._rng.uniform(0.1, 0.4),
            latency_ms=latency_ms,
            input_tokens=1300,
            model=self.model,
        )
