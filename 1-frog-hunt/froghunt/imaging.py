"""Geometry, tiling and drawing helpers. No Windows-specific code here."""
from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

MAX_SIDE = 1568        # API limit: longest side of one image
MIN_SIDE = 280         # API limit: shortest side
MAX_PIXELS = 2_458_624  # API limit: 1568 * 1568

FONT_CANDIDATES = ["impact.ttf", "seguibl.ttf", "segoeuib.ttf", "arialbd.ttf"]
WHITE = (245, 245, 245)
RED = (232, 62, 62)
GREEN = (86, 220, 120)
DIM = (18, 18, 20)
AMBER = (255, 190, 60)


@dataclass(frozen=True)
class Box:
    """Bounding box in fractions of the image, origin top-left."""

    left: float
    top: float
    right: float
    bottom: float

    @classmethod
    def from_api(cls, data: dict) -> "Box":
        return cls(
            float(data["left"]),
            float(data["top"]),
            float(data["right"]),
            float(data["bottom"]),
        )

    @classmethod
    def from_pixels(cls, x0: int, y0: int, x1: int, y1: int, w: int, h: int) -> "Box":
        return cls(x0 / w, y0 / h, x1 / w, y1 / h).clamp()

    def as_dict(self) -> dict:
        return {
            "left": round(self.left, 5),
            "top": round(self.top, 5),
            "right": round(self.right, 5),
            "bottom": round(self.bottom, 5),
        }

    def valid(self) -> bool:
        return self.right > self.left and self.bottom > self.top

    def clamp(self) -> "Box":
        left = min(max(self.left, 0.0), 1.0)
        top = min(max(self.top, 0.0), 1.0)
        right = min(max(self.right, 0.0), 1.0)
        bottom = min(max(self.bottom, 0.0), 1.0)
        if right <= left:
            right = min(1.0, left + 1e-3)
        if bottom <= top:
            bottom = min(1.0, top + 1e-3)
        return Box(left, top, right, bottom)

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        b = self.clamp()
        x0 = int(round(b.left * width))
        y0 = int(round(b.top * height))
        x1 = int(round(b.right * width))
        y1 = int(round(b.bottom * height))
        x0 = max(0, min(x0, width - 1))
        y0 = max(0, min(y0, height - 1))
        x1 = max(x0 + 1, min(x1, width))
        y1 = max(y0 + 1, min(y1, height))
        return x0, y0, x1, y1

    def center(self) -> tuple[float, float]:
        return ((self.left + self.right) / 2, (self.top + self.bottom) / 2)

    def area(self) -> float:
        return max(0.0, self.right - self.left) * max(0.0, self.bottom - self.top)

    def iou(self, other: "Box") -> float:
        left = max(self.left, other.left)
        top = max(self.top, other.top)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        inter = max(0.0, right - left) * max(0.0, bottom - top)
        union = self.area() + other.area() - inter
        return inter / union if union > 0 else 0.0

    def expand(self, fraction: float) -> "Box":
        return Box(
            self.left - fraction,
            self.top - fraction,
            self.right + fraction,
            self.bottom + fraction,
        ).clamp()


@dataclass(frozen=True)
class Tile:
    name: str
    col: int
    row: int
    box: Box

    def to_global(self, local: Box) -> Box:
        """Map a box expressed in tile fractions to fractions of the full image."""
        width = self.box.right - self.box.left
        height = self.box.bottom - self.box.top
        return Box(
            self.box.left + local.left * width,
            self.box.top + local.top * height,
            self.box.left + local.right * width,
            self.box.top + local.bottom * height,
        ).clamp()


def make_grid(cols: int, rows: int, overlap: float = 0.12) -> list[Tile]:
    """Split into tiles that overlap slightly.

    Without the overlap a frog sitting on a tile boundary is cut in half: it is
    found twice as a sliver, never once as a whole, and the click lands on an
    edge. With the overlap at least one tile holds the target completely.
    """
    tiles: list[Tile] = []
    width = 1.0 / cols
    height = 1.0 / rows
    pad_x = width * overlap
    pad_y = height * overlap
    for row in range(rows):
        for col in range(cols):
            tiles.append(
                Tile(
                    name=f"r{row + 1}c{col + 1}",
                    col=col,
                    row=row,
                    box=Box(
                        col * width - pad_x,
                        row * height - pad_y,
                        (col + 1) * width + pad_x,
                        (row + 1) * height + pad_y,
                    ).clamp(),
                )
            )
    return tiles


def default_grid(width: int, height: int) -> tuple[int, int]:
    """Pick a grid whose tiles are roughly 700-900 source pixels wide."""
    cols = 1
    for candidate in range(1, 7):
        cols = candidate
        if width / candidate <= 900:
            break
    rows = max(1, math.ceil(cols * height / width))
    return cols, rows


def prepare_tile(image: Image.Image, tile: Tile, max_side: int = MAX_SIDE) -> bytes:
    """Crop one tile and scale it into the API's size window. Returns PNG bytes."""
    width, height = image.size
    crop = image.crop(tile.box.to_pixels(width, height))
    crop = crop.convert("RGB")
    cw, ch = crop.size

    scale = max_side / max(cw, ch)
    if cw * ch * scale * scale > MAX_PIXELS:
        scale = math.sqrt(MAX_PIXELS / (cw * ch))
    # Never below the API's minimum side, never above 4x (pure blur beyond that).
    scale = max(scale, MIN_SIDE / min(cw, ch))
    scale = min(scale, 4.0)

    if abs(scale - 1.0) > 0.01:
        crop = crop.resize((max(1, round(cw * scale)), max(1, round(ch * scale))), Image.LANCZOS)

    buf = io.BytesIO()
    # compress_level=3: ~2x faster than the default 6, ~10x faster than optimize=True,
    # and the difference in size is small for line art.
    crop.save(buf, "PNG", compress_level=3)
    return buf.getvalue()


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    value: str,
    font,
    fill=WHITE,
    anchor: str = "la",
    shadow: bool = True,
) -> None:
    if shadow:
        draw.text((xy[0] + 2, xy[1] + 2), value, font=font, fill=(0, 0, 0), anchor=anchor)
    draw.text(xy, value, font=font, fill=fill, anchor=anchor)


def draw_box(
    draw: ImageDraw.ImageDraw,
    box: Box,
    size: tuple[int, int],
    colour=GREEN,
    width: int = 4,
    padding: int = 0,
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box.to_pixels(*size)
    x0, y0, x1, y1 = x0 - padding, y0 - padding, x1 + padding, y1 + padding
    draw.rectangle([x0, y0, x1, y1], outline=colour, width=width)
    return x0, y0, x1, y1


def draw_crosshair(draw: ImageDraw.ImageDraw, xy: tuple[float, float], radius: int, colour=GREEN) -> None:
    x, y = xy
    draw.line([(x - radius, y), (x + radius, y)], fill=colour, width=3)
    draw.line([(x, y - radius), (x, y + radius)], fill=colour, width=3)


def draw_marker(draw: ImageDraw.ImageDraw, box: Box, size: tuple[int, int], label: str = "") -> None:
    """Red ring used to hide an already-found target from the next look."""
    x0, y0, x1, y1 = box.to_pixels(*size)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    radius = max(18, int(max(x1 - x0, y1 - y0) * 0.9))
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=RED, width=4)
    if label:
        font = load_font(max(16, radius // 2))
        text(draw, (cx, cy), label, font, fill=RED, anchor="mm")


_SCRIM_CACHE: dict[tuple, Image.Image] = {}


def scrim(canvas: Image.Image, top: int = 150, bottom: int = 110, alpha: int = 185) -> Image.Image:
    """Darken the top and bottom edges so white HUD text survives any artwork."""
    width, height = canvas.size
    key = (width, top, bottom, alpha)
    bands = _SCRIM_CACHE.get(key)
    if bands is None:
        bands = []
        for edge, size in (("top", top), ("bottom", bottom)):
            ramp = Image.new("L", (1, size))
            for y in range(size):
                fade = 1 - y / size if edge == "top" else y / size
                ramp.putpixel((0, y), int(alpha * (fade ** 1.35)))
            band = Image.new("RGBA", (width, size), (0, 0, 0, 0))
            band.putalpha(ramp.resize((width, size)))
            bands.append(band)
        _SCRIM_CACHE[key] = bands
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    overlay.paste(bands[0], (0, 0))
    overlay.paste(bands[1], (0, height - bottom))
    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


def fit_canvas(image: Image.Image, size: tuple[int, int], background=DIM) -> Image.Image:
    """Letterbox an image into a canvas without cropping it."""
    canvas = Image.new("RGB", size, background)
    scale = min(size[0] / image.width, size[1] / image.height)
    new = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
    canvas.paste(new, ((size[0] - new.width) // 2, (size[1] - new.height) // 2))
    return canvas


def cover_canvas(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Fill a canvas, cropping the overflow (better for vertical video)."""
    scale = max(size[0] / image.width, size[1] / image.height)
    new = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
    left = (new.width - size[0]) // 2
    top = (new.height - size[1]) // 2
    return new.crop((left, top, left + size[0], top + size[1]))


# -- scene coordinates ---------------------------------------------------
# Everything Jev finds is stored in "scene" pixels: a virtual canvas that the
# viewport slides over when the game scrolls. That keeps boxes glued to the
# artwork instead of to the screen.

Rect = tuple[float, float, float, float]


def box_to_scene(box: Box, origin: tuple[float, float], size: tuple[int, int]) -> Rect:
    x0, y0, x1, y1 = box.to_pixels(*size)
    return (origin[0] + x0, origin[1] + y0, origin[0] + x1, origin[1] + y1)


def scene_to_viewport(rect: Rect, origin: tuple[float, float], size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Scene rect -> viewport pixels, or None when it is off screen."""
    x0 = rect[0] - origin[0]
    y0 = rect[1] - origin[1]
    x1 = rect[2] - origin[0]
    y1 = rect[3] - origin[1]
    if x1 < 0 or y1 < 0 or x0 > size[0] or y0 > size[1]:
        return None
    return int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))


def rect_iou(a: Rect, b: Rect) -> float:
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    inter = max(0.0, right - left) * max(0.0, bottom - top)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def rect_center(rect: Rect) -> tuple[float, float]:
    return ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)


def estimate_view_shift(before: Image.Image, after: Image.Image, *, downscale: int = 2) -> tuple[float, float] | None:
    """How far the camera moved between two frames, in full-resolution pixels.

    Positive x means the viewport moved right. Returns None when numpy is
    missing or the two frames have nothing in common.
    """
    try:
        import numpy as np
    except ImportError:
        return None

    width = max(2, before.width // downscale)
    height = max(2, before.height // downscale)
    a = np.asarray(before.convert("L").resize((width, height)), dtype=np.float32)
    b = np.asarray(after.convert("L").resize((width, height)), dtype=np.float32)
    a -= a.mean()
    b -= b.mean()
    if float(np.abs(a).max()) < 1e-3 or float(np.abs(b).max()) < 1e-3:
        return None

    window = np.outer(np.hanning(height), np.hanning(width))
    cross = np.fft.rfft2(a * window) * np.conj(np.fft.rfft2(b * window))
    cross /= np.abs(cross) + 1e-9
    correlation = np.fft.irfft2(cross, s=a.shape)
    dy, dx = np.unravel_index(int(np.argmax(correlation)), correlation.shape)
    if dy > height // 2:
        dy -= height
    if dx > width // 2:
        dx -= width
    return float(dx * downscale), float(dy * downscale)

