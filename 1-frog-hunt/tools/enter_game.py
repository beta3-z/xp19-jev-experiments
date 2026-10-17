"""Press START while the title screen is showing.

Detection uses the START-button box question: "is this the title screen?" turned
out to be unreliable (0.12 on the real title screen, 0.7-0.8 on other runs).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_step as A  # noqa: E402
from froghunt import win32  # noqa: E402
from froghunt.imaging import Box  # noqa: E402

FALLBACK = (0.499, 0.555)
MIN_CONFIDENCE = 0.35

A.load_env(".env")
client = A.Client()
win32.enable_dpi_awareness()
window = win32.find_window("hidden frogs")
window.focus()
time.sleep(0.8)

for attempt in range(5):
    shot = window.grab()
    answers = client.ask_custom(A.to_png(shot), {"start": {"type": "box", "instructions": "the START button"}})
    answer = answers.get("start") or {}
    raw = answer.get("box")
    confidence = answer.get("confidence") or 0.0
    print(f"attempt {attempt + 1}: START box confidence {confidence:.2f}")
    if not isinstance(raw, dict) or confidence < MIN_CONFIDENCE:
        print("no confident START button - assuming the game is running")
        break
    box = Box.from_api(raw)
    x, y, width, height = window.client_rect()
    cx, cy = box.center()
    px, py = x + int(cx * width), y + int(cy * height)
    window.focus()
    win32.click(px, py)
    print(f"clicked START at {px},{py}")
    time.sleep(3.0)
