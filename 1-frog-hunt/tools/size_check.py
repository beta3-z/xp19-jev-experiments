"""Compare the size of Jev boxes with hand-drawn ones."""
import json
import statistics
from pathlib import Path

from froghunt.imaging import rect_iou

REPORT = Path(__file__).resolve().parent.parent / "runs" / "report"
ann = json.loads((REPORT / "annotations.json").read_text(encoding="utf-8"))
data = json.loads((REPORT / "data.json").read_text(encoding="utf-8"))


def size(box):
    return box[2] - box[0], box[3] - box[1]


cand_sizes, truth_sizes, ious = [], [], []
for step in data["steps"]:
    candidates = step.get("candidates") or []
    value = ann.get(step["frame"])
    if not candidates or not isinstance(value, dict):
        continue
    if not value.get("removed") and not value.get("added"):
        continue
    truth = [list(box) for box in value.get("boxes", [])]
    truth_sizes += [size(box) for box in truth]
    for candidate in candidates:
        cand_sizes.append(size(candidate["box_px"]))
        if truth:
            best = max(truth, key=lambda box: rect_iou(tuple(candidate["box_px"]), tuple(box)))
            ious.append(rect_iou(tuple(candidate["box_px"]), tuple(best)))

print(f"Jev boxes      n={len(cand_sizes)}  width {statistics.median(s[0] for s in cand_sizes):.0f}  "
      f"height {statistics.median(s[1] for s in cand_sizes):.0f}  (medians)")
print(f"hand boxes     n={len(truth_sizes)}  width {statistics.median(s[0] for s in truth_sizes):.0f}  "
      f"height {statistics.median(s[1] for s in truth_sizes):.0f}  (medians)")
for limit in (0.2, 0.3, 0.5, 0.7):
    print(f"IoU > {limit}: {sum(1 for v in ious if v > limit)}/{len(ious)}")
print(f"mean IoU {statistics.mean(ious):.2f}")

found = {0.2: 0, 0.3: 0, 0.5: 0}
total_truth = 0
for step in data["steps"]:
    candidates = step.get("candidates") or []
    value = ann.get(step["frame"])
    if not candidates or not isinstance(value, dict):
        continue
    if not value.get("removed") and not value.get("added"):
        continue
    for truth_box in value.get("boxes", []):
        total_truth += 1
        best = max((rect_iou(tuple(c["box_px"]), tuple(truth_box)) for c in candidates), default=0.0)
        for limit in found:
            if best > limit:
                found[limit] += 1
print()
print(f"frogs on frames {total_truth}")
for limit, count in found.items():
    print(f"frogs Jev covered (IoU > {limit}): {count}/{total_truth}  ({count / total_truth:.0%})")
