"""Capture the whole game world by edge-scrolling down and stitching frames.

Scrolling up past the top of the world returns the game to its menu, so the
capture starts by scrolling up to that menu, pressing START, and then walking
down one screen-ish step at a time. Frames are taken at the bottom edge; the
step is kept under 500 px because phase correlation wraps beyond ~720 px.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_step as A  # noqa: E402
from froghunt import win32  # noqa: E402
from froghunt.imaging import Box, estimate_view_shift  # noqa: E402
from panorama import stitch  # noqa: E402

OUT = Path("runs/world")
STEPS = int(sys.argv[1]) if len(sys.argv) > 1 else 14
STEP_PX = float(sys.argv[2]) if len(sys.argv) > 2 else 480.0
HOLD_MAX = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
BOTTOM_PX = 60.0
MIN_CONFIDENCE = 0.40

A.load_env(".env")
client = A.Client()
win32.enable_dpi_awareness()
window = win32.find_window("hidden frogs")
window.focus()
time.sleep(0.8)
OUT.mkdir(parents=True, exist_ok=True)

x, y, width, height = window.client_rect()
cx = x + width // 2
centre = (cx, y + height // 2)
edge = (cx, y + height - 4)
top = (cx, y + 4)


def start_button():
    shot = window.grab()
    answers = client.ask_custom(A.to_png(shot), {"start": {"type": "box", "instructions": "the START button"}})
    answer = answers.get("start") or {}
    return answer.get("box"), (answer.get("confidence") or 0.0)


print("scrolling up to the menu")
win32.move_to(*top)
raw, confidence = None, 0.0
if "--no-menu" not in sys.argv:
    for _ in range(24):
        time.sleep(1.0)
        raw, confidence = start_button()
        if confidence >= MIN_CONFIDENCE:
            break
    print(f"  START box confidence {confidence:.2f}")

    if confidence >= MIN_CONFIDENCE and isinstance(raw, dict):
        box = Box.from_api(raw)
        px, py = box.center()
        window.focus()
        win32.click(x + int(px * width), y + int(py * height))
        print("  clicked START")
        win32.move_to(*centre)
        time.sleep(3.5)
else:
    time.sleep(0.5)
    win32.move_to(*centre)
    print("  menu handling skipped, capturing from the current view")

frames = []
frame = window.grab()
frames.append(frame)
frame.save(OUT / "000.jpg", "JPEG", quality=90)

for step in range(1, STEPS + 1):
    if win32.user32.GetForegroundWindow() != window.hwnd:
        window.focus()
        time.sleep(0.4)
    win32.move_to(*edge)
    moved = 0.0
    previous = frame
    deadline = time.time() + HOLD_MAX
    while moved < STEP_PX and time.time() < deadline:
        time.sleep(0.15)
        current = window.grab()
        shift = estimate_view_shift(previous, current)
        if shift:
            moved += abs(shift[1])
        previous = current
    frame = window.grab()
    frames.append(frame)
    frame.save(OUT / f"{step:03d}.jpg", "JPEG", quality=90)
    print(f"step {step}: moved {moved:.0f} px")
    if moved < BOTTOM_PX:
        print("reached the bottom")
        break

win32.move_to(*centre)
canvas, origins = stitch(frames)
canvas.save(OUT / "world.jpg", "JPEG", quality=88)
print(f"world -> {OUT / 'world.jpg'} {canvas.size} from {len(frames)} frames")
