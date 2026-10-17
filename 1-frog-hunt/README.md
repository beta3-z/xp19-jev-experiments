# 100 Hidden Frogs × TypeSafe Jev

The script takes a screenshot of the game, cuts it into tiles, and asks Jev 1.14 "where is the frog here" **one tile per call**. It merges the answers into one bounding box, clicks it, and checks whether the game reacted. When no frogs remain on the current screen, it scrolls the scene and continues.

During a run the screen is only recorded. Boxes, the counter, and captions are baked into the video later, in post-production. This way the measurement does not pay for drawing, and the picture can be cleaner than a live overlay allows.

```
┌─ screenshot (2560×1440) ─┐
│  grid 3×2 + overlap      │     6 × POST /v1/systemone (in parallel)
│  ┌────┬────┬────┐        │     each: noul "is there a frog?" + box "frog"
│  │ r1c1│r1c2│r1c3│       │ ────────────────────────────────────────────────►
│  ├────┼────┼────┤        │
│  │ r2c1│r2c2│r2c3│       │ ◄────────────────────────────────────────────────
│  └────┴────┴────┘        │     {present: 0.91, box: {...}, confidence: 0.77}
└──────────────────────────┘
             │
             ▼  best tile → world coordinates → click → check → log
                                                                    │
                          screen recording + time log ──────────────┴──► video with boxes
```

## Why Python

About 90% of the task is plumbing: window capture, crop, scaling, click, frame recording, video assembly. In Python each operation takes 5–10 lines (Pillow + ctypes), and an iteration takes seconds. Go or Rust would give a binary and speed that has nowhere to go: the bottleneck is the network call to the model (~0.3–1 s), not the code.

## Requirements

- Windows (capture, clicks, and recording use the Win32 API)
- Python 3.11+ (`X | None` in annotations)
- `pip install -r requirements.txt`
- A TypeSafe API key: [console.typesafe.ai](https://console.typesafe.ai/home) → API keys
- The game in **windowed or borderless mode**: exclusive fullscreen under DWM often returns a black frame
- ffmpeg, only to get `video.mp4`. Without it the frames are still written; encode them manually

The key is read from `.env` next to `run.py` (the file is in `.gitignore`) or from an environment variable, which takes priority:

```powershell
Copy-Item .env.example .env
notepad .env          # TYPESAFE_API_KEY=ts_...
# or
setx TYPESAFE_API_KEY "ts_..."
```

## Quick start

```powershell
# 1. Key, box questions, and synthetic input (5 seconds)
python run.py doctor --check-input

# 2. Is the window found and captured?
python run.py grab --window "hidden frogs"

# 3. First run: one frog, no clicks. Check the frame in runs/<ts>/frames/
python run.py run --window "hidden frogs" --max-frogs 1

# 4. Run with recording and clicks
python run.py run --window "hidden frogs" --click --record --max-frogs 10 --counter-rect 20,20,140,40

# 5. Video: boxes over the recording
python run.py render runs/20261017-154000 --aspect 9:16
```

Without the API, the whole pipeline (recording and render included) runs on a synthetic world that is wider than the screen:

```powershell
python run.py selftest
```

## Recording and post-production boxes

`--record` starts a background thread. It captures the game window and writes JPEG frames to `film/` with a timestamp for each frame. The frame has no clicks or boxes: only the game, the cursor, and its movement.

Why not a live overlay: it costs ~40 ms per redraw, 2–3 redraws per round, which is 7–10% of the cycle time. Post-production costs nothing at run time, and the quality is higher: the box can be animated and the HUD made larger.

After the run, `render` reads `events.jsonl`, restores the state at each moment, and draws over the recording:

- red boxes of found frogs (only those on the current page, so old boxes do not hang in the air after a scroll);
- a green target box with a 220 ms "reveal" animation;
- the timer, `ROUND 06/10`, `found 4`, the status (`LOOKING` / `AIMING` / `HIT` / `NOTHING HERE` / `SCROLLING`), and the page number;
- an enlarged crop of the game counter, if `--counter-rect` is set.

Sync is exact, not by eye: the recording and the events use the same `perf_counter`, and `started_perf` is written to `run.json`. The offset between video and boxes is zero by construction.

The cursor is added to the frame separately. `BitBlt` does not capture it, so the frame is composited with the real cursor icon through `DrawIconEx`. Otherwise the video does not show where the click went.

**Recording cost.** Capture takes 24 ms at 1080p and 42 ms at 1440p, plus 4–7 ms for JPEG. At `--record-fps 8` this is ~20–25% of one core at 1080p and ~40% at 1440p. It does not affect the measurements (model latency is measured around the HTTP request). For less load, use `--record-fps 6` or `--no-record`.

## Logs: what to show when

All animation is built from `events.jsonl`, not from the video. It is line-delimited JSON written during the run. Each event has `t`, in seconds from the run start.

| Event | Written when | What it gives the picture |
| --- | --- | --- |
| `run_start` | start | settings: target, grid, `counter_rect` |
| `round_start` | frame captured | `cols`, `rows`, and **the geometry of each tile** |
| `call` | model reply received | `latency_ms` → request sent at `t − latency`; `present`, `confidence`, `box` |
| `pick` | decision made | when to show the target box |
| `click` | click sent | `x,y` and `box` → flash at the click point |
| `round_end` | result | whether it counted, page `origin` |
| `pan` | scene moved | new page, `origin` |

These give all four moments the viewer must see: **asked** (`call`), **decided** (`pick`), **clicked** (`click`), **counted** (`round_end`).

The link to the video is exact: events count time from `Hunter.started`, recorded frames store the absolute `perf_counter`, and `started_perf` in `run.json` joins them. There is no drift by construction, so you can rebuild the recording and the log at any pace and in any order without a new run.

```json
{"t": 0.129, "event": "call", "round": 1, "tile": "r2c2", "latency_ms": 19.5, "present": 0.914, "box": {...}}
{"t": 0.139, "event": "pick", "round": 1, "tile": "r2c1", "score": 0.783, "box": {...}}
{"t": 0.160, "event": "round_end", "round": 1, "found": true, "clicked": false, "origin": [0.0, 0.0]}
```

The tile geometry in `round_start` lets post-production draw "six looks in parallel" over the real recording. While `t` is less than the tile's reply time, its box is gray; after that, it is amber with a `0.42s` label, and the chosen frog gets a green outline. Without these fields the render would know only the final box, not the moment the model returned it.

## Live overlay (optional)

To see boxes during the run, use `--overlay`. It is a transparent click-through window (`WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST`): the mouse passes through it into the game.

Two traps:

- **The overlay hides before each screenshot** for a few milliseconds. Otherwise its boxes get into the frame, and the model starts looking for frogs inside the drawn boxes.
- `UpdateLayeredWindow` needs **premultiplied alpha**, but PIL stores straight alpha. Without `premultiply()` the boxes glow with a halo.

Measured at 2560×1440: compositing 10.8 ms, premultiply + upload 40.4 ms, `hide/show` near zero. Numpy premultiplication was *slower* than four PIL passes (60 vs 31 ms), so there is nothing to optimize there.

## Scrolling

The scene can be larger than the window:

1. While the screen has frogs, search it.
2. After `--miss-before-pan` empty looks in a row, scroll.
3. After a scroll, measure **how far the frame actually moved** with phase correlation. This gives the exact scene offset and detects the edge: the frame stopped moving → nowhere left to scroll.
4. When the X axis is exhausted, try Y. If Y hits the edge too, stop.

All finds are stored in **world coordinates** (scene + accumulated offset). So boxes do not drift after a scroll, and a frog found again is discarded by IoU.

Methods: `--pan wheel` (default), `drag`, `keys`, `none`. Without `numpy` there is no phase correlation: the script assumes the scene moved exactly `--pan-step` and does not detect the edge.

## Kill switch

Three independent ways to stop a run:

| Method | Works when |
| --- | --- |
| `F8` (set with `--kill-key`) | globally, game in focus |
| A `STOP` file in the run folder | always, even if the game holds the keyboard |
| `Ctrl+C` in the console | the terminal is in focus |

```powershell
"stop" > runs\20261017-154000\STOP
```

On stop, the mouse is force-released, the recording is closed, and `run.json` gets `stopped_by`. The recording is not lost: the video is built from what was recorded.

`python run.py doctor --check-input` moves the cursor 40 px through `SendInput` and checks that it moved. `BLOCKED` means synthetic input does not reach the desktop (another session, RDP, a service), and neither clicks nor the global hotkey will work.

## How it works

**Overlapping tiles.** Frogs are tens of pixels on a 1440p frame. If the model gets the whole screenshot, it will not see them after the resize to 1568 px. So the frame is cut into a grid, and each tile goes as a separate image scaled to 1568 px. Tiles overlap by 12%: without overlap, a frog on a border is cut in half and found twice as strips, never whole. A candidate that touches the tile edge gets a score penalty.

**Two questions in one call.** `box` has no "not found" answer: it always returns four edges. So `noul` ("is there a frog in `screen`?") goes with it, and a candidate with a low probability is discarded. Score = `noul × confidence`.

**One call, one frog.** `box` returns exactly one rectangle. Six tiles = six parallel calls, and the best one wins.

**Deduplication.** Found frogs are outlined on the next frame, and boxes with IoU > 0.25 are discarded. This is done in world coordinates, so it works after a scroll too.

**Click check.** The click goes through `SendInput` to the box center. If `--counter-rect` is set, the game counter is the judge: if it changed, the find counted. Otherwise the local area around the click is compared with a control area in the corner. If there is no reaction, the script tries 4 offsets of 12 px.

**Measurement.** The log has the round-trip of each call, total time, calls, tokens, and cost at $0.042/1M. `run.json` has the summary, including `pages`, `stopped_by`, and `started_perf` for sync with the recording.

## Tuning for a specific game

| Situation | What to do |
| --- | --- |
| No frogs found at all | Smaller tiles: `--cols 4 --rows 3` |
| Many false positives | Raise `--min-present 0.6` |
| Boxes are skewed or cut | Smaller tiles and more overlap (`make_grid(..., overlap=)` in code) |
| Clicks miss | Set `--counter-rect` and use smaller tiles |
| Scrolling does not work | Try `--pan drag` or `--pan keys` |

## Limitations

- **Image/box are TypeSafe preview features** ("dreamer-ready"): the format and accuracy can change between releases.
- Accuracy on small black-and-white graphics is unpredictable: that is what this test measures.
- Capture and input need an interactive desktop; `doctor --check-input` shows this.
- Recording uses 20–40% of a core, depending on resolution and fps.
- API cost: ~6 calls per frog, 1.5–3k tokens per image. 100 frogs cost a few cents.

## Video script

1. **0–3 s, hook.** Full game frame, the cursor is in no hurry. Caption: "I gave an AI a game where you must find 100 frogs. It sees one at a time."
2. **3–8 s.** 3×2 tiling: all six tiles light up at once, each gets `0.42s`.
3. **8–20 s.** Three or four finds in a row: the box appears, the cursor moves, click, the game counter goes up by itself.
4. **20–28 s.** The screen empties, `NOTHING HERE` appears, the frame moves, and a frog is found in the new place.
5. **Final.** `73/100`, `0.4 s per look`, `$0.06`, and an honest failure: a click that did not work.

## Structure

```
frog-hunt/
  run.py              entry point
  froghunt/
    win32.py          DPI, window lookup, GDI capture (+cursor), SendInput
    imaging.py        Box/Tile, overlapping grid, world coordinates, phase correlation
    recorder.py       screen recording to film/ with timestamps
    overlay.py        layered click-through window (optional live mode)
    hotkey.py         kill switch: F8 + STOP file
    typesafe.py       API client (multipart), MockClient
    envfile.py        .env reader (KEY=VALUE), no dependencies
    hunt.py           loop: screenshot → tiles → calls → pick → click → scroll → log
    render.py         log + recording → video; or reconstruction from still frames
    cli.py            grab / doctor / run / render / selftest
```
