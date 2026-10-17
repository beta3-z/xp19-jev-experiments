"""Global kill switch: poll a key, set an event, never block the main loop.

GetAsyncKeyState reads the system-wide key table, so it works while the game
has focus without installing a keyboard hook (hooks can be flagged by anti-cheat
and are easier to leak).
"""
from __future__ import annotations

import ctypes
import threading
from pathlib import Path
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

VK = {
    "esc": 0x1B,
    "pause": 0x13,
    "scrolllock": 0x91,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def resolve_key(name: str) -> int:
    key = name.strip().lower()
    if key in VK:
        return VK[key]
    if len(key) == 1:
        return ord(key.upper())
    raise ValueError(f"unknown hotkey {name!r}; try one of {', '.join(VK)}")


class KillSwitch:
    """Three independent ways to stop a run.

    1. A global key (F8 by default), polled with GetAsyncKeyState.
    2. A stop file — `touch STOP` in the run folder, which works even when the
       game owns the keyboard and no hotkey can reach us.
    3. Ctrl+C in the console, handled by the CLI.
    """

    def __init__(
        self,
        key: str = "f8",
        on_stop: Callable[[], None] | None = None,
        poll_s: float = 0.03,
        stop_file: Path | None = None,
    ):
        self.vk = resolve_key(key)
        self.key_name = key.upper()
        self.event = threading.Event()
        self._on_stop = on_stop
        self._poll_s = poll_s
        self._stop_file = Path(stop_file) if stop_file else None
        self._thread: threading.Thread | None = None
        self._quit = threading.Event()

    def start(self) -> "KillSwitch":
        self._quit.clear()
        self._thread = threading.Thread(target=self._poll, name="kill-switch", daemon=True)
        self._thread.start()
        return self

    def _poll(self) -> None:
        was_down = False
        while not self._quit.is_set():
            down = bool(user32.GetAsyncKeyState(self.vk) & 0x8000)
            if down and not was_down:
                print(f"\n  {self.key_name} pressed - stopping")
                self.trigger()
            was_down = down
            if self._stop_file is not None and self._stop_file.exists():
                print(f"\n  stop file {self._stop_file.name} - stopping")
                try:
                    self._stop_file.unlink()
                except OSError:
                    pass
                self.trigger()
            self._quit.wait(self._poll_s)

    def trigger(self) -> None:
        if self.event.is_set():
            return
        self.event.set()
        if self._on_stop is not None:
            try:
                self._on_stop()
            except Exception:
                pass

    @property
    def triggered(self) -> bool:
        return self.event.is_set()

    def reset(self) -> None:
        self.event.clear()

    def stop(self) -> None:
        self._quit.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            self._thread = None
