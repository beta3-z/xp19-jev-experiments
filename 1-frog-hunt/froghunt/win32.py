"""Windows-only helpers: DPI awareness, window lookup, screen capture, clicks.

Only stdlib + Pillow. GDI BitBlt is used first (works for windowed and borderless
games under DWM); PrintWindow with PW_RENDERFULLCONTENT is the fallback for
windows that return a black frame.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes as w
from dataclasses import dataclass

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD),
        ("biWidth", w.LONG),
        ("biHeight", w.LONG),
        ("biPlanes", w.WORD),
        ("biBitCount", w.WORD),
        ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD),
        ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD),
        ("biClrImportant", w.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.POINTER(w.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.POINTER(w.ULONG)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


user32.GetDC.restype = w.HDC
user32.GetDC.argtypes = [w.HWND]
user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
user32.ReleaseDC.restype = ctypes.c_int
gdi32.CreateDCW.restype = w.HDC
gdi32.CreateDCW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = w.HDC
gdi32.CreateCompatibleDC.argtypes = [w.HDC]
gdi32.CreateCompatibleBitmap.restype = w.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
gdi32.SelectObject.restype = w.HGDIOBJ
gdi32.BitBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD]
gdi32.BitBlt.restype = w.BOOL
gdi32.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), w.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
gdi32.DeleteDC.argtypes = [w.HDC]
user32.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
user32.PrintWindow.restype = w.BOOL
user32.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.ClientToScreen.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = w.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = w.UINT
user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]


def enable_dpi_awareness() -> None:
    """Make screen coordinates physical pixels, not DPI-scaled ones."""
    if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
        return
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def virtual_screen_rect() -> tuple[int, int, int, int]:
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def _dib_to_image(hdc: int, hbm: int, width: int, height: int) -> Image.Image:
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = width
    bi.bmiHeader.biHeight = -height  # top-down
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0  # BI_RGB

    buf = ctypes.create_string_buffer(width * height * 4)
    lines = gdi32.GetDIBits(hdc, hbm, 0, height, buf, ctypes.byref(bi), DIB_RGB_COLORS)
    if lines == 0:
        raise OSError("GetDIBits failed")
    # BGRX drops the alpha channel during decode: one pass instead of decode + convert.
    return Image.frombuffer("RGB", (width, height), buf, "raw", "BGRX", 0, 1)


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hCursor", w.HANDLE), ("ptScreenPos", w.POINT)]


CURSOR_SHOWING = 0x00000001
DI_NORMAL = 0x0003
user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetCursorInfo.restype = w.BOOL
user32.DrawIconEx.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, w.HANDLE, ctypes.c_int, ctypes.c_int, w.UINT, w.HANDLE, w.UINT]
user32.DrawIconEx.restype = w.BOOL


def _draw_cursor(hdc: int, offset_x: int, offset_y: int) -> None:
    """Composite the real cursor into the captured bitmap (BitBlt never includes it)."""
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(info)):
        return
    if not info.flags & CURSOR_SHOWING or not info.hCursor:
        return
    user32.DrawIconEx(
        hdc,
        info.ptScreenPos.x - offset_x,
        info.ptScreenPos.y - offset_y,
        info.hCursor,
        0, 0, 0, None, DI_NORMAL,
    )


def _bitblt(hdc_src: int, sx: int, sy: int, width: int, height: int, *, draw_cursor: bool = False) -> Image.Image:
    hdc_mem = gdi32.CreateCompatibleDC(hdc_src)
    hbm = gdi32.CreateCompatibleBitmap(hdc_src, width, height)
    old = gdi32.SelectObject(hdc_mem, hbm)
    try:
        if not gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_src, sx, sy, SRCCOPY):
            raise OSError(f"BitBlt failed (err={ctypes.get_last_error()})")
        if draw_cursor:
            _draw_cursor(hdc_mem, sx, sy)
        return _dib_to_image(hdc_mem, hbm, width, height)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(hdc_mem)


def grab_screen(draw_cursor: bool = False) -> tuple[Image.Image, int, int]:
    """Capture every monitor. Returns (image, virtual_left, virtual_top)."""
    vx, vy, vw, vh = virtual_screen_rect()
    hdc = gdi32.CreateDCW("DISPLAY", None, None, None)
    if not hdc:
        raise OSError("CreateDC('DISPLAY') failed")
    try:
        return _bitblt(hdc, 0, 0, vw, vh, draw_cursor=draw_cursor), vx, vy
    finally:
        gdi32.DeleteDC(hdc)


def grab_region(x: int, y: int, width: int, height: int, draw_cursor: bool = False) -> Image.Image:
    """Capture one rectangle of the desktop. Cheaper than grabbing everything."""
    vx, vy, _, _ = virtual_screen_rect()
    hdc = gdi32.CreateDCW("DISPLAY", None, None, None)
    if not hdc:
        raise OSError("CreateDC('DISPLAY') failed")
    try:
        return _bitblt(hdc, x - vx, y - vy, width, height, draw_cursor=draw_cursor)
    finally:
        gdi32.DeleteDC(hdc)


@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str
    pid: int
    exe: str = ""

    def client_rect(self) -> tuple[int, int, int, int]:
        """(x, y, width, height) in physical screen pixels."""
        r = w.RECT()
        if not user32.GetClientRect(self.hwnd, ctypes.byref(r)):
            raise OSError("GetClientRect failed")
        pt = w.POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y, r.right - r.left, r.bottom - r.top

    def grab(self, draw_cursor: bool = False) -> Image.Image:
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            time.sleep(0.4)
        x, y, cw, ch = self.client_rect()
        if cw <= 0 or ch <= 0:
            raise OSError("window has no client area (minimised?)")
        img = grab_region(x, y, cw, ch, draw_cursor=draw_cursor)
        if _looks_blank(img):
            fallback = self._grab_printwindow(cw, ch)
            if fallback is not None and not _looks_blank(fallback):
                return fallback
        return img

    def _grab_printwindow(self, width: int, height: int) -> Image.Image | None:
        hdc = user32.GetDC(self.hwnd)
        if not hdc:
            return None
        hdc_mem = gdi32.CreateCompatibleDC(hdc)
        hbm = gdi32.CreateCompatibleBitmap(hdc, width, height)
        old = gdi32.SelectObject(hdc_mem, hbm)
        try:
            if not user32.PrintWindow(self.hwnd, hdc_mem, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
                return None
            return _dib_to_image(hdc_mem, hbm, width, height)
        finally:
            gdi32.SelectObject(hdc_mem, old)
            gdi32.DeleteObject(hbm)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(self.hwnd, hdc)

    def focus(self) -> None:
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            time.sleep(0.2)
        user32.SetForegroundWindow(self.hwnd)
        time.sleep(0.1)


def _looks_blank(img: Image.Image) -> bool:
    """True when a frame is essentially one flat colour (exclusive fullscreen)."""
    small = img.convert("L").resize((32, 32))
    pixels = list(small.getdata())
    spread = max(pixels) - min(pixels)
    mean = sum(pixels) / len(pixels)
    return spread < 3 and (mean < 3 or mean > 252)


def list_windows() -> list[Window]:
    found: list[Window] = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(Window(hwnd, buf.value, pid.value, _process_name(pid.value)))
        return True

    user32.EnumWindows(callback, 0)
    return found


def _process_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = w.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_window(pattern: str) -> Window:
    needle = pattern.lower()
    matches = [
        win
        for win in list_windows()
        if needle in win.title.lower() or needle in win.exe.lower()
    ]
    if not matches:
        titles = ", ".join(sorted({f"{x.title!r}" for x in list_windows() if x.title}))[:400]
        raise LookupError(f"no window matching {pattern!r}. Visible windows: {titles}")
    # Prefer the biggest window: the game, not a launcher or an overlay.
    def area(win: Window) -> int:
        try:
            _, _, w_, h_ = win.client_rect()
            return w_ * h_
        except OSError:
            return 0

    return max(matches, key=area)


def cursor_pos() -> tuple[int, int]:
    pt = w.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def click(x: int, y: int) -> None:
    """Send a real left click at absolute screen coordinates."""
    nx, ny = _absolute(x, y)
    events = (INPUT * 2)()
    for i, flags in enumerate(
        (
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
            MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
        )
    ):
        events[i].type = 0  # INPUT_MOUSE
        events[i].u.mi.dx = nx
        events[i].u.mi.dy = ny
        events[i].u.mi.dwFlags = flags
    user32.SendInput(2, events, ctypes.sizeof(INPUT))
    time.sleep(0.04)
    up = (INPUT * 1)()
    up[0].type = 0
    up[0].u.mi.dx = nx
    up[0].u.mi.dy = ny
    up[0].u.mi.dwFlags = MOUSEEVENTF_LEFTUP | MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    user32.SendInput(1, up, ctypes.sizeof(INPUT))


def _absolute(x: int, y: int) -> tuple[int, int]:
    vx, vy, vw, vh = virtual_screen_rect()
    return (
        int(round((x - vx) * 65535 / max(1, vw - 1))),
        int(round((y - vy) * 65535 / max(1, vh - 1))),
    )


def _mouse(flags: int, x: int | None = None, y: int | None = None, data: int = 0) -> None:
    event = INPUT()
    event.type = 0
    if x is not None and y is not None:
        event.u.mi.dx, event.u.mi.dy = _absolute(x, y)
        flags |= MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE
    event.u.mi.dwFlags = flags
    event.u.mi.mouseData = ctypes.c_uint(data).value
    user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))


def move_to(x: int, y: int) -> None:
    _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, x, y)


def wheel(notches: float, *, horizontal: bool = False) -> None:
    """Scroll by wheel notches. Positive is up/left, negative is down/right."""
    _mouse(MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, data=int(notches * 120))


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def drag(start: tuple[int, int], end: tuple[int, int], *, button: str = "middle", steps: int = 24) -> None:
    down, up = _BUTTONS[button]
    move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    _mouse(move, *start)
    time.sleep(0.06)
    _mouse(down)
    time.sleep(0.06)
    for step in range(1, steps + 1):
        t = step / steps
        x = int(round(start[0] + (end[0] - start[0]) * t))
        y = int(round(start[1] + (end[1] - start[1]) * t))
        _mouse(move, x, y)
        time.sleep(0.008)
    time.sleep(0.06)
    _mouse(up)
    time.sleep(0.06)


def press_key(vk: int, hold: float = 0.03) -> None:
    down = INPUT()
    down.type = 1
    down.u.ki.wVk = vk
    up = INPUT()
    up.type = 1
    up.u.ki.wVk = vk
    up.u.ki.dwFlags = KEYEVENTF_KEYUP
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    time.sleep(hold)
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def release_mouse() -> None:
    """Let go of every button — called by the kill switch so nothing sticks."""
    for _down, up in _BUTTONS.values():
        _mouse(up)
