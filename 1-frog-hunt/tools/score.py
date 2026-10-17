"""Score Jev against the hand-made annotation.

Kept proposals count as correct, deleted ones as false positives, hand-added
boxes as frogs Jev never proposed.
"""
import json
from pathlib import Path

from PIL import Image, ImageDraw

from froghunt.imaging import rect_iou

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "runs" / "report"
ann = json.loads((REPORT / "annotations.json").read_text(encoding="utf-8"))
data = json.loads((REPORT / "data.json").read_text(encoding="utf-8"))


def iou(a, b):
    return rect_iou(tuple(a), tuple(b))


def center(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def inside(point, box):
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def truth_boxes(value):
    return [list(box) for box in value.get("boxes", [])]


def parse(candidates, value):
    if not isinstance(value, dict):
        return None
    removed = set(value.get("removed", []))
    added = [list(box) for box in value.get("added", [])]
    return removed, added


rows = []
offsets = []
shifts = []

for step in data["steps"]:
    candidates = step.get("candidates") or []
    if not candidates:
        continue
    parsed = parse(candidates, ann.get(step["frame"]))
    if parsed is None:
        continue
    removed, added = parsed
    if not removed and not added:
        print(f"{step['frame'][:12]:<13} untouched, skipped")
        continue
    kept = [i for i in range(len(candidates)) if i not in removed]
    truth = truth_boxes(ann[step["frame"]])
    clickable = sum(1 for c in candidates if any(inside(center(c["box_px"]), box) for box in truth))
    for c in candidates:
        hits = [box for box in truth if inside(center(c["box_px"]), box)]
        if hits:
            box = min(hits, key=lambda b: abs(center(b)[0] - center(c["box_px"])[0]))
            shifts.append((center(c["box_px"])[0] - center(box)[0],
                           center(c["box_px"])[1] - center(box)[1],
                           iou(c["box_px"], box)))
    covered_by_center = sum(1 for box in truth
                            if any(inside(center(c["box_px"]), box) for c in candidates))
    top1_click = inside(center(candidates[0]["box_px"]), truth[0]) if truth else False
    top1_click = any(inside(center(candidates[0]["box_px"]), box) for box in truth)
    covered = sum(1 for box in added if any(iou(c["box_px"], box) > 0.15 for c in candidates))
    for box in added:
        best = max(candidates, key=lambda c: iou(c["box_px"], box))
        if iou(best["box_px"], box) > 0.15:
            offsets.append(((best["box_px"][1] + best["box_px"][3]) - (box[1] + box[3])) / 2)
    rows.append({
        "frame": step["frame"], "cand": len(candidates), "removed": len(removed),
        "added": len(added), "kept": len(kept), "covered": covered,
        "truth": len(truth), "clickable": clickable, "covered_center": covered_by_center,
        "top1": top1_click,
        "top3": any(inside(center(c["box_px"]), box)
                    for c in candidates[:3] for box in truth),
        "top5": any(inside(center(c["box_px"]), box)
                    for c in candidates[:5] for box in truth),
    })

print()
print(f"{'frame':<13}{'cand':>6}{'truth':>7}{'click':>7}{'found':>7}{'top1':>6}{'top3':>6}{'top5':>6}{'add~cand':>9}")
for row in rows:
    print(f"{row['frame'][:12]:<13}{row['cand']:>6}{row['truth']:>7}{row['clickable']:>7}"
          f"{row['covered_center']:>7}"
          f"{'yes' if row['top1'] else 'no':>6}{'yes' if row['top3'] else 'no':>6}"
          f"{'yes' if row['top5'] else 'no':>6}{row['covered']:>9}")

cand = sum(r["cand"] for r in rows)
kept = sum(r["kept"] for r in rows)
added = sum(r["added"] for r in rows)
truth = sum(r["truth"] for r in rows)
clickable = sum(r["clickable"] for r in rows)
covered_center = sum(r["covered_center"] for r in rows)
print("-" * 62)
print(f"frames scored        {len(rows)}")
print(f"Jev proposals        {cand}")
print(f"deleted as wrong     {cand - kept}")
print(f"hand-added frogs     {added}")
print(f"truth boxes          {truth}")
print(f"click lands on frog  {clickable}/{cand}  ({clickable / cand:.0%} of proposals)")
print(f"frogs with a click   {covered_center}/{truth}  ({covered_center / truth:.0%} of frogs)")
print(f"deleted by hand      {cand - kept}  (size corrections, not false positives)")
print(f"top-1 lands on frog  {sum(r['top1'] for r in rows)}/{len(rows)}")
print(f"frog within top-3    {sum(r['top3'] for r in rows)}/{len(rows)}")
print(f"frog within top-5    {sum(r['top5'] for r in rows)}/{len(rows)}")
if offsets:
    print(f"mean box offset      {sum(offsets) / len(offsets):+.0f} px (negative = above the frog)")
if shifts:
    mean_dx = sum(s[0] for s in shifts) / len(shifts)
    mean_dy = sum(s[1] for s in shifts) / len(shifts)
    mean_iou = sum(s[2] for s in shifts) / len(shifts)
    close = sum(1 for s in shifts if s[2] > 0.3)
    print(f"click vs frog centre {mean_dx:+.0f}, {mean_dy:+.0f} px, IoU {mean_iou:.2f} "
          f"({close}/{len(shifts)} overlap well)")

for row in rows[:2]:
    step = next(s for s in data["steps"] if s["frame"] == row["frame"])
    image = Image.open(ROOT / "screenshots" / row["frame"]).convert("RGB")
    draw = ImageDraw.Draw(image)
    for candidate in step["candidates"]:
        draw.rectangle(candidate["box_px"], outline=(235, 190, 40), width=4)
    kept_boxes = [c["box_px"] for i, c in enumerate(step["candidates"])
                  if i not in set(ann[row["frame"]]["removed"])]
    for box in kept_boxes + [list(b) for b in ann[row["frame"]]["added"]]:
        draw.rectangle(box, outline=(0, 190, 90), width=6)
    out = ROOT / "runs" / "grab" / f"truth_{row['frame'][-11:-4]}.jpg"
    image.resize((1600, 900)).save(out, "JPEG", quality=85)
    print(f"image -> {out}")
