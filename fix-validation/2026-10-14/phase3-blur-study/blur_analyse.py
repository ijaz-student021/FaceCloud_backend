"""Pick blur thresholds on the calibration half; report both halves."""
import collections, json, sys
import numpy as np

rows = json.load(open(sys.argv[1]))
reviewed_blurred = set(json.load(open(sys.argv[2]))) if len(sys.argv) > 2 else set()
for r in rows:
    if r["variant"] in ("original", "native") and r["file"] in reviewed_blurred:
        r["label"] = "reviewed_blurred"

cal = [r for r in rows if r["split"] == "calibration"]
ev = [r for r in rows if r["split"] == "evaluation"]
print("photos: calibration", len({r["file"] for r in cal}), "evaluation", len({r["file"] for r in ev}))

# Uncertain: edge strength below which nothing is judged. Take it from flat/dark content:
# the 1st percentile of sharp-variant edge strength on calibration, floored.
sharp_cal = [r for r in cal if r["label"] == "sharp"]
print("sharp edge_strength percentiles (cal):", np.percentile([r["edge_strength"] for r in sharp_cal], [0.5, 1, 5, 50]).round(2))

def stats(subset, key="edge"):
    by = collections.defaultdict(list)
    for r in subset:
        by[(r["variant"], r["label"])].append(r[key])
    return by

for name, subset in (("calibration", cal), ("evaluation", ev)):
    print(f"\n{name}: edge_sharpness by variant  (p1  p5  median  p95  p99)")
    for (variant, label), values in sorted(stats(subset).items(), key=lambda kv: np.median(kv[1]), reverse=True):
        p = np.percentile(values, [1, 5, 50, 95, 99])
        print(f"  {variant:22} {label:16} n={len(values):4d}  " + "  ".join(f"{v:.3f}" for v in p))

MIN_STRENGTH = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
def verdict(r, severe, mild):
    if r["edge_strength"] < MIN_STRENGTH:
        return "uncertain"
    return "severe" if r["edge"] < severe else "mild" if r["edge"] < mild else "sharp"

# Thresholds from calibration only. Mild: below the 1st percentile of sharp variants
# (so at most ~1 % of sharp photos get any blur verdict). Severe: midway between the
# p95 of severe-labelled and the p5 of mild-labelled on calibration.
sharp_vals = [r["edge"] for r in sharp_cal if r["edge_strength"] >= MIN_STRENGTH]
mild_vals = [r["edge"] for r in cal if r["label"] == "mild"]
severe_vals = [r["edge"] for r in cal if r["label"] == "severe"]
mild_t = float(np.percentile(sharp_vals, 1))
severe_t = float((np.percentile(severe_vals, 95) + np.percentile(mild_vals, 5)) / 2)
severe_t = min(severe_t, mild_t)
print(f"\nchosen on calibration: mild below {mild_t:.3f}, severe below {severe_t:.3f}, uncertain below strength {MIN_STRENGTH}")

for name, subset in (("calibration", cal), ("evaluation", ev)):
    table = collections.defaultdict(collections.Counter)
    for r in subset:
        table[r["label"]][verdict(r, severe_t, mild_t)] += 1
    print(f"\n{name}: label -> verdicts")
    for label, counts in table.items():
        total = sum(counts.values())
        print(f"  {label:16} n={total:4d}  " + "  ".join(f"{k}={v} ({v/total:.1%})" for k, v in sorted(counts.items())))
    per_variant = collections.defaultdict(collections.Counter)
    for r in subset:
        per_variant[r["variant"]][verdict(r, severe_t, mild_t)] += 1
    print("  per variant:")
    for variant, counts in per_variant.items():
        total = sum(counts.values())
        print(f"    {variant:22} " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
json.dump({"mild_below": mild_t, "severe_below": severe_t, "min_edge_strength": MIN_STRENGTH},
          open("blur_thresholds.json", "w"))
