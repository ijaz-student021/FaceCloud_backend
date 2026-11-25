CELLS = [
("md", """# 04 · NAFNet (GoPro): motion-blur removal

**What it is.** NAFNet (*Nonlinear Activation Free Network*, Chen et al., ECCV 2022) trained on the GoPro motion-blur dataset (`NAFNet-GoPro-width64.pth`, 259 MB). Pretrained, MIT licence.

This notebook measures it with a **ground truth**: sharp public photographs are blurred artificially (known motion-blur kernel), restored by NAFNet and by a classical unsharp-mask baseline, and compared with the original sharp photograph (PSNR / SSIM, higher is better)."""),
("md", "## 1 · Synthetic motion blur → restoration"),
("code", '''from enhancement.hd_restoration import deblur_with_nafnet

def motion_blur(image, length=15, angle=20):
    kernel = np.zeros((length, length), np.float32); kernel[length // 2, :] = 1
    matrix = cv2.getRotationMatrix2D((length / 2 - 0.5, length / 2 - 0.5), angle, 1)
    kernel = cv2.warpAffine(kernel, matrix, (length, length)); kernel /= kernel.sum()
    return cv2.filter2D(image, -1, kernel)

def unsharp(image):
    return cv2.addWeighted(image, 1.8, cv2.GaussianBlur(image, (0, 0), 3), -0.8, 0)

sharp_ids = sorted(public_stock_ids(), key=lambda pk: -sharpness(photo(pk, 700)))[:6]
rows, shown = [], []
for pk in sharp_ids:
    truth = photo(pk, 700); blurred = motion_blur(truth)
    restored, seconds = timed(lambda: first(deblur_with_nafnet(blurred)))
    base = unsharp(blurred)
    rows.append({"photo": pk, "psnr_blurred": round(psnr(truth, blurred), 2), "psnr_unsharp": round(psnr(truth, base), 2), "psnr_nafnet": round(psnr(truth, restored), 2),
                 "ssim_blurred": round(ssim(truth, blurred), 3), "ssim_unsharp": round(ssim(truth, base), 3), "ssim_nafnet": round(ssim(truth, restored), 3), "seconds": round(seconds, 1)})
    shown.append((truth, blurred, base, restored))
table(rows)'''),
("code", '''x = np.arange(len(rows)); w = 0.27
fig, ax = plt.subplots(1, 2, figsize=(12, 3.8))
for axis, key, label in ((ax[0], "psnr", "PSNR dB (higher = closer to the sharp original)"), (ax[1], "ssim", "SSIM (higher = closer)")):
    axis.bar(x - w, [r[f"{key}_blurred"] for r in rows], w, label="blurred input", color=COLORS["input"])
    axis.bar(x, [r[f"{key}_unsharp"] for r in rows], w, label="unsharp mask", color=COLORS["baseline"])
    axis.bar(x + w, [r[f"{key}_nafnet"] for r in rows], w, label="NAFNet", color=COLORS["model"])
    axis.set_xticks(x, [r["photo"] for r in rows]); axis.set_title(label, fontsize=10); axis.legend(fontsize=8)
fig.tight_layout(); save(fig, "04_nafnet_gopro_scores.png")
print("mean PSNR  blurred %.2f | unsharp %.2f | NAFNet %.2f dB" % tuple(np.mean([r[k] for r in rows]) for k in ("psnr_blurred", "psnr_unsharp", "psnr_nafnet")))
print("mean SSIM  blurred %.3f | unsharp %.3f | NAFNet %.3f" % tuple(np.mean([r[k] for r in rows]) for k in ("ssim_blurred", "ssim_unsharp", "ssim_nafnet")))'''),
("code", '''pick = shown[:3]
images = [im for q in pick for im in q]; titles = [t for i in range(len(pick)) for t in ("sharp original", "motion-blurred", "unsharp mask", "NAFNet")]
fig = montage(images, titles, columns=4, width=3.0); fig.set_size_inches(12, 2.2 * len(pick)); save(fig, "04_nafnet_gopro_examples.png")'''),
("md", "## 2 · Is it used by the application? (database)"),
("code", '''from collections import Counter
from enhancement.models import ImageEnhancementResult
run, accepted = Counter(), Counter()
for result in ImageEnhancementResult.objects.filter(status="completed"):
    for stage in (result.applied_parameters.get("adaptive") or {}).get("stages") or []:
        if stage["operation"] == "motion_deblurring":
            for engine in stage.get("models_run") or []:
                run[engine] += 1; accepted[engine] += stage.get("status") == "accepted"
print("motion_deblurring stage, engines run / accepted:", dict(run), dict(accepted))
save_json("04_nafnet_gopro.json", {"rows": rows, "pipeline_runs": dict(run), "pipeline_accepted": dict(accepted)})'''),
("md", "## Scorecard (evaluation metrics)"),
("code", '''metrics = {"images": len(rows)}
for k, idx in (("blurred", 1), ("unsharp", 2), ("nafnet", 3)):
    metrics[f"psnr_{k}_db"] = float(np.mean([r[f"psnr_{k}"] for r in rows])); metrics[f"ssim_{k}"] = float(np.mean([r[f"ssim_{k}"] for r in rows]))
    metrics[f"mae_{k}"] = float(np.mean([mae(q[0], q[idx]) for q in shown]))
metrics["mean_seconds"] = float(np.mean([r["seconds"] for r in rows]))
metrics["psnr_gain_over_input_db"] = metrics["psnr_nafnet_db"] - metrics["psnr_blurred_db"]
print(scorecard("NAFNet GoPro", "motion deblur", metrics, "synthetic motion blur with ground truth, 6 public photos"))'''),
("md", """## 3 · Verdict
NAFNet recovers detail that a classical sharpen cannot (higher PSNR/SSIM against the true sharp image). On real photographs the pipeline still *judges* its output (structure, identity, halo gates) because a deblurring network can invent texture. A heavily blurred photograph is refused up front (`too_blurry_to_enhance`) instead of being "fixed"."""),
]
