CELLS = [
("md", """# 06 · Real-ESRGAN: enlargement and detail

**What it is.** Two pretrained Real-ESRGAN networks (Wang et al., 2021, BSD-3-Clause): the **compact** `realesr-general-x4v3` (4.7 MB, the default) and the heavy **RRDB x4plus** (64 MB, a slow fallback).

**How it is scored (ground truth).** A sharp public photograph is shrunk to 1/4 size, then enlarged back by (a) plain bicubic interpolation, (b) the compact network, (c) x4plus, and (d) the compact network **blended with bicubic exactly as the application does** (`REALESRGAN_OUTPUT_BLEND`). Compared with the original: PSNR / SSIM / MAE and **sharpness ratio** (Laplacian variance relative to the original, 1.0 = as sharp as the truth), plus time.

**Read this before the numbers.** PSNR and SSIM reward being *close to the pixel values*, which smooth interpolation does very well; a GAN-trained enlarger deliberately adds texture, so it scores *lower* on PSNR/SSIM and *higher* on sharpness. That is a known property of this model family, so both kinds of metric are reported and neither is hidden."""),
("code", '''from enhancement.hd_restoration import upscale_with_general, upscale_with_realesrgan
blend = float(settings.REALESRGAN_OUTPUT_BLEND); print("application blend with bicubic:", blend)
ids = sorted(public_stock_ids(), key=lambda pk: -sharpness(photo(pk, 800)))[:5]
rows, shown = [], []
for pk in ids:
    truth = photo(pk, 800); small = cv2.resize(truth, (truth.shape[1] // 4, truth.shape[0] // 4), interpolation=cv2.INTER_AREA)
    fit = lambda a: cv2.resize(a, (truth.shape[1], truth.shape[0]), interpolation=cv2.INTER_CUBIC) if a.shape != truth.shape else a
    bicubic = cv2.resize(small, (truth.shape[1], truth.shape[0]), interpolation=cv2.INTER_CUBIC)
    general, t_general = timed(lambda: fit(first(upscale_with_general(small, output_scale=4))))
    heavy, t_heavy = timed(lambda: fit(first(upscale_with_realesrgan(small, output_scale=4))))
    app = cv2.addWeighted(general, blend, bicubic, 1.0 - blend, 0)
    row = {"photo": pk, "sec_compact": round(t_general, 2), "sec_x4plus": round(t_heavy, 2)}
    for name, image in (("bicubic", bicubic), ("compact", general), ("x4plus", heavy), ("app", app)):
        row[f"psnr_{name}"] = round(psnr(truth, image), 2); row[f"ssim_{name}"] = round(ssim(truth, image), 3); row[f"mae_{name}"] = round(mae(truth, image), 2)
        row[f"sharp_{name}"] = round(sharpness(image) / sharpness(truth), 2)
    rows.append(row); shown.append((truth, bicubic, general, heavy, app))
table(rows, ["photo", "psnr_bicubic", "psnr_compact", "psnr_x4plus", "psnr_app", "ssim_bicubic", "ssim_compact", "ssim_x4plus", "ssim_app", "sharp_bicubic", "sharp_compact", "sharp_x4plus", "sharp_app"])'''),
("code", '''names = ["bicubic", "compact", "x4plus", "app"]; labels = ["bicubic", "compact (raw)", "x4plus", "compact + blend (as used)"]
cols = [COLORS["input"], COLORS["model"], COLORS["baseline"], COLORS["good"]]
fig, ax = plt.subplots(1, 4, figsize=(17, 3.8))
for axis, key, title in ((ax[0], "psnr", "PSNR dB (higher = closer to the original)"), (ax[1], "ssim", "SSIM (higher = closer)"), (ax[2], "sharp", "Sharpness ratio (1.0 = as sharp as the original)")):
    for i, (n, l, c) in enumerate(zip(names, labels, cols)):
        axis.bar(np.arange(len(rows)) + (i - 1.5) * 0.2, [r[f"{key}_{n}"] for r in rows], 0.2, label=l, color=c)
    axis.set_xticks(range(len(rows)), [r["photo"] for r in rows]); axis.set_title(title, fontsize=9)
ax[2].axhline(1.0, color="k", lw=0.8); ax[0].legend(fontsize=7)
ax[3].bar(np.arange(len(rows)) - 0.2, [r["sec_compact"] for r in rows], 0.4, label="compact", color=COLORS["model"]); ax[3].bar(np.arange(len(rows)) + 0.2, [r["sec_x4plus"] for r in rows], 0.4, label="x4plus", color=COLORS["baseline"])
ax[3].set_xticks(range(len(rows)), [r["photo"] for r in rows]); ax[3].set_title("Seconds per photo (200x150 -> 800x600)", fontsize=9); ax[3].legend(fontsize=8)
fig.tight_layout(); save(fig, "06_realesrgan_scores.png")
for n in names: print(f"{n:8s} PSNR {np.mean([r['psnr_' + n] for r in rows]):6.2f} dB  SSIM {np.mean([r['ssim_' + n] for r in rows]):.3f}  MAE {np.mean([r['mae_' + n] for r in rows]):5.2f}  sharpness x{np.mean([r['sharp_' + n] for r in rows]):.2f}")'''),
("code", '''y, x0 = shown[0][0].shape[0] // 3, shown[0][0].shape[1] // 3
crop = lambda i: i[y:y + 200, x0:x0 + 260]
fig = montage([crop(i) for i in shown[0]], ["original", "bicubic", "compact", "x4plus", "compact + blend"], width=3.0); save(fig, "06_realesrgan_examples.png")
from collections import Counter
from enhancement.models import ImageEnhancementResult
run = Counter()
for result in ImageEnhancementResult.objects.filter(status="completed"):
    for stage in (result.applied_parameters.get("adaptive") or {}).get("stages") or []:
        if stage["operation"] in ("super_resolution", "detail_restoration"):
            for engine in stage.get("models_run") or []: run[f"{stage['operation']}/{engine}"] += 1
print("engines used in the live results:", dict(run))
save_json("06_realesrgan.json", {"rows": rows, "pipeline_runs": dict(run)})'''),
("md", "## Scorecard (evaluation metrics)"),
("code", '''metrics = {"images": len(rows)}
for n in names:
    for k in ("psnr", "ssim", "mae", "sharp"):
        metrics[f"{k}_{n}"] = float(np.mean([r[f"{k}_{n}"] for r in rows]))
metrics["seconds_compact"] = float(np.mean([r["sec_compact"] for r in rows])); metrics["seconds_x4plus"] = float(np.mean([r["sec_x4plus"] for r in rows]))
print(scorecard("Real-ESRGAN", "x4 upscale / detail", metrics, "downscale-then-restore with ground truth, 5 public photos; compact+blend is what the app uses"))'''),
("md", """## Verdict (what the numbers above actually say)
* Against pixel-level ground truth, plain bicubic scores best on PSNR/SSIM/MAE and the networks score lower: they add texture that is plausible but not identical to the original.
* The raw networks recover far more sharpness than bicubic (sharpness ratio about 0.2-0.35 against 0.01; compare the crops) but at lower PSNR/SSIM. The application blends the compact output with bicubic (`REALESRGAN_OUTPUT_BLEND`): the blended version keeps PSNR almost at the bicubic level while adding a little sharpness, i.e. it is the *conservative* setting, trading detail for fidelity (no invented texture on faces).
* The compact network is about twice as fast as x4plus here (and much faster on larger inputs), which is why it is the default.
* The pipeline's own judge (SSIM / structure / identity gates) decides per photograph whether the enlarged version is kept."""),
]
