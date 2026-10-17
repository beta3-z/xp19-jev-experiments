"""A click-through, always-on-top window that draws over the game.

Windows has exactly the right primitive for this: a layered window with
WS_EX_TRANSPARENT, painted with UpdateLayeredWindow so per-pixel alpha works
and mouse events fall through to the game.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w

from PIL import Image, ImageChops

from .win32 import BITMAPINFO, BITMAPINFOHEADER, gdi32, user32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
DIB_RGB_COLORS = 0

user32.CreateWindowExW.restype = w.HWND
user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p,
]
user32.UpdateLayeredWindow.argtypes = [
    w.HWND, w.HDC, ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE),
    w.HDC, ctypes.POINTER(w.POINT), w.DWORD, ctypes.c_void_p, w.DWORD,
]
user32.UpdateLayeredWindow.restype = w.BOOL
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [w.HWND]
gdi32.CreateDIBSection.argtypes = [
    w.HDC, ctypes.POINTER(BITMAPINFO), w.UINT,
    ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD,
]
gdi32.CreateDIBSection.restype = w.HBITMAP


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


def premultiply(image: Image.Image) -> bytes:
    """PIL uses straight alpha; UpdateLayeredWindow wants premultiplied BGRA."""
    image = image.convert("RGBA")
    red, green, blue, alpha = image.split()
    red = ImageChops.multiply(red, alpha)
    green = ImageChops.multiply(green, alpha)
    blue = ImageChops.multiply(blue, alpha)
    return Image.merge("RGBA", (red, green, blue, alpha)).tobytes("raw", "BGRA")


class Overlay:
    """Owns one layered window. All methods must be called from one thread."""

    def __init__(self, rect: tuple[int, int, int, int]):
        self._hwnd: w.HWND | None = None
        self._rect = rect
        self._hdc_src = None
        self._hbmp = None
        self._old = None
        self._bits = ctypes.c_void_p()
        self._width = 0
        self._height = 0
        self._visible = False
        self._screen = user32.GetDC(None)
        self._create(rect)

    # -- window ----------------------------------------------------------
    def _create(self, rect: tuple[int, int, int, int]) -> None:
        x, y, width, height = rect
        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "STATIC",
            "froghunt overlay",
            WS_POPUP | 0x10000000,  # WS_VISIBLE
            x, y, width, height,
            None, None, None, None,
        )
        if not self._hwnd:
            raise OSError(f"CreateWindowEx failed: {ctypes.get_last_error()}")
        self._hdc_src = gdi32.CreateCompatibleDC(self._screen)
        self._resize_bitmap(width, height)
        self._visible = True

    def _resize_bitmap(self, width: int, height: int) -> None:
        if self._hbmp:
            gdi32.SelectObject(self._hdc_src, self._old)
            gdi32.DeleteObject(self._hbmp)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        self._bits = ctypes.c_void_p()
        self._hbmp = gdi32.CreateDIBSection(self._hdc_src, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(self._bits), None, 0)
        if not self._hbmp:
            raise OSError("CreateDIBSection failed")
        self._old = gdi32.SelectObject(self._hdc_src, self._hbmp)
        self._width, self._height = width, height

    # -- drawing ---------------------------------------------------------
    def draw(self, image: Image.Image) -> None:
        if not self._hwnd:
            return
        if image.size != (self._width, self._height):
            self._resize_bitmap(*image.size)
            user32.SetWindowPos(self._hwnd, None, self._x, self._y, image.width, image.height, 0x0004 | 0x0010)
        data = premultiply(image)
        ctypes.memmove(self._bits, data, len(data))

        destination = w.POINT(self._x, self._y)
        size = w.SIZE(self._width, self._height)
        source = w.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            self._hwnd, self._screen,
            ctypes.byref(destination), ctypes.byref(size),
            self._hdc_src, ctypes.byref(source),
            0, ctypes.byref(blend), ULW_ALPHA,
        )
        self._pump()

    @property
    def _x(self) -> int:
        return self._rect[0]

    @property
    def _y(self) -> int:
        return self._rect[1]

    def move(self, rect: tuple[int, int, int, int]) -> None:
        x, y, width, height = rect
        self._rect = rect
        if not self._hwnd:
            return
        if (width, height) != (self._width, self._height):
            self._resize_bitmap(width, height)
        user32.SetWindowPos(self._hwnd, None, x, y, width, height, 0x0004 | 0x0010)

    def hide(self) -> None:
        if self._hwnd and self._visible:
            user32.ShowWindow(self._hwnd, SW_HIDE)
            self._visible = False

    def show(self) -> None:
        if self._hwnd and not self._visible:
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._visible = True

    def close(self) -> None:
        if not self._hwnd:
            return
        try:
            if self._hbmp:
                gdi32.SelectObject(self._hdc_src, self._old)
                gdi32.DeleteObject(self._hbmp)
            if self._hdc_src:
                gdi32.DeleteDC(self._hdc_src)
            user32.DestroyWindow(self._hwnd)
        finally:
            self._hwnd = None
            self._hbmp = None
            self._hdc_src = None

    @property
    def alive(self) -> bool:
        return self._hwnd is not None

    @staticmethod
    def _pump() -> None:
        message = w.MSG()
        while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):  # PM_REMOVE
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def __enter__(self) -> "Overlay":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
