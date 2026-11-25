CELLS = [
("md", """# 00 · FaceCloud AI: models, how the pipeline uses them, and the scorecard

This notebook covers three things:

1. **Which models does the system run, and what is each for?**
2. **Does the application really use them, and where do the per-image scores come from?** (read from the database, not typed in)
3. **How good is each one?** (the scorecard collects the numbers measured in notebooks 01-10)

> **About the models.** None of the neural models running in the application was fine-tuned: they are public pretrained models (one small fine-tuning *experiment* on Zero-DCE is documented in notebook 11 and was not installed), **selected, evaluated and integrated** for this use case. What the project engineered is the *adaptive pipeline around them*: the router that picks an operation per defect, the validator gates (SSIM, structure, identity, geometry) that accept or refuse every result, the identity safety checks, and the thresholds calibrated on the project's own photographs. Notebooks 01-10 measure the models exactly as the application runs them, on public stock photographs, with ground truth where it can be constructed."""),
("md", "## 1 · Model inventory (files on the server, switches in the configuration)"),
("code", '''import hashlib
models_dir = ROOT / ".runtime" / "models"
inventory = [
 ("YuNet", "face detection", "yunet_s_dynamic.onnx", "MIT", "always on", "01"),
 ("SFace", "face identity embedding", "face_recognition_sface_2021dec.onnx", "Apache-2.0", "always on", "02"),
 ("Zero-DCE", "low-light enhancement", "zero_dce_epoch99.onnx", "research (public weights)", str(settings.ZERO_DCE_ENABLED), "03"),
 ("NAFNet GoPro", "motion deblur", "NAFNet-GoPro-width64.pth", "MIT", str(settings.NAFNET_DEBLUR_ENABLED), "04"),
 ("NAFNet SIDD", "denoise", "NAFNet-SIDD-width64.pth", "MIT", str(settings.NAFNET_DENOISE_ENABLED) + " (tested, off)", "05"),
 ("Real-ESRGAN compact", "upscale / detail (default)", "realesr-general-x4v3.pth", "BSD-3-Clause", str(settings.HD_RESTORATION_ENABLED), "06"),
 ("Real-ESRGAN x4plus", "upscale (slow fallback)", "RealESRGAN_x4plus.pth", "BSD-3-Clause", str(settings.HD_RESTORATION_ENABLED), "06"),
 ("CodeFormer", "face restoration", "codeformer/codeformer.pth", "S-Lab (non-commercial)", str(settings.CODEFORMER_ENABLED), "07"),
 ("DDColor", "colorization", "ddcolor/ddcolor_paper.bin", "Apache-2.0", str(settings.DDCOLOR_ENABLED), "08"),
 ("SigLIP ViT-B/16", "semantic photo search", "siglip-vit-b-16/open_clip_model.safetensors", "Apache-2.0", str(settings.SEMANTIC_SEARCH_ENABLED), "09"),
 ("GFPGAN v1.4", "face restoration (alternative)", "gfpgan/GFPGANv1.4.pth", "Apache-2.0", str(settings.GFPGAN_ENABLED) + " (never used)", "-"),
]
rows = []
for name, task, filename, licence, enabled, nb in inventory:
    path = models_dir / filename
    rows.append({"model": name, "task": task, "file_MB": round(path.stat().st_size / 1e6, 1) if path.exists() else "missing", "licence": licence, "enabled": enabled, "weights": "pretrained (public)", "notebook": nb})
table(rows)'''),
("md", "## 2 · Proof that the pipeline uses them: engines run and accepted in the stored results"),
("code", '''from collections import Counter
from enhancement.models import ImageEnhancementResult
results = list(ImageEnhancementResult.objects.filter(status="completed"))
run, accepted = Counter(), Counter()
for r in results:
    for stage in (r.applied_parameters.get("adaptive") or {}).get("stages") or []:
        for engine in stage.get("models_run") or []:
            run[(stage["operation"], engine)] += 1
            if stage.get("status") == "accepted": accepted[(stage["operation"], engine)] += 1
keys = sorted(run, key=lambda k: -run[k])
print(f"{len(results)} completed enhancement results")
table([{"operation": op, "engine": eng, "ran": run[(op, eng)], "stage_accepted": accepted[(op, eng)], "accept_rate": f"{accepted[(op, eng)] / run[(op, eng)]:.0%}"} for op, eng in keys])
fig, ax = plt.subplots(figsize=(9, 0.38 * len(keys) + 1)); y = np.arange(len(keys))
ax.barh(y, [run[k] for k in keys], color=COLORS["input"], label="ran"); ax.barh(y, [accepted[k] for k in keys], color=COLORS["model"], label="stage accepted")
ax.set_yticks(y, [f"{op} / {eng}" for op, eng in keys], fontsize=8); ax.invert_yaxis(); ax.set_title("Engines run by the pipeline across all stored results"); ax.legend(); fig.tight_layout(); save(fig, "00_engine_usage.png")'''),
("md", "## 3 · Where the per-image scores come from (one stored result, traced end to end)"),
("code", '''import json
from enhancement.models import ImageEnhancementResult as R
demo = None
for only_public in (True, False):
    qs = R.objects.filter(status="completed").order_by("-id")
    if only_public: qs = qs.filter(image__original_name__icontains="unsplash")
    for r in qs:
        p = r.applied_parameters; stages = (p.get("adaptive") or {}).get("stages") or []
        if len(stages) >= 2 and p.get("quality_before") and p.get("quality_after"): demo = r; break
    if demo: break
p = demo.applied_parameters; before, after = p["quality_before"], p["quality_after"]
print(f"image #{demo.image_id} ({demo.image.original_name[:30]}), preset {p.get('requested_level')}/{p.get('style')}, {demo.processing_duration_ms / 1000:.1f}s total\\n")
print("quality_before ->", {k: before[k] for k in ("mean_luminance", "contrast_stddev", "laplacian_sharpness", "estimated_noise")}, "edge_sharpness", before["blur"]["edge_sharpness"])
for s in p["adaptive"]["stages"]:
    print(f"  stage {s['operation']:24s} models_run={s.get('models_run')} status={s.get('status')} chosen={s.get('chosen')} {s.get('seconds', 0):.1f}s")
print("quality_after  ->", {k: after[k] for k in ("mean_luminance", "contrast_stddev", "laplacian_sharpness", "estimated_noise")}, "edge_sharpness", after["blur"]["edge_sharpness"])
print("identity_verification ->", {k: (p.get("identity_verification") or {}).get(k) for k in ("similarity", "threshold", "status")}, "| quality_gate ->", p.get("quality_gate"))'''),
("code", '''b = [(r.applied_parameters.get("quality_before") or {}, r.applied_parameters.get("quality_after") or {}) for r in results if r.applied_parameters.get("quality_before") and r.applied_parameters.get("quality_after")]
fig, ax = plt.subplots(1, 4, figsize=(16, 3.6))
for axis, (key, title) in zip(ax, (("laplacian_sharpness", "Sharpness (Laplacian var)"), ("estimated_noise", "Noise estimate"), ("mean_luminance", "Mean brightness"), ("edge", "Edge sharpness"))):
    def getv(q, key=key):
        try: return float(q["blur"]["edge_sharpness"] if key == "edge" else q[key])
        except (KeyError, TypeError): return np.nan
    xs = np.array([getv(q0) for q0, q1 in b], float); ys = np.array([getv(q1) for q0, q1 in b], float); ok = np.isfinite(xs) & np.isfinite(ys); xs, ys = xs[ok], ys[ok]
    axis.scatter(xs, ys, s=8, alpha=0.5, color=COLORS["model"]); lim = [0, max(xs.max(), ys.max())]; axis.plot(lim, lim, "k--", lw=0.8); axis.set_xlabel("before"); axis.set_ylabel("after"); axis.set_title(title, fontsize=9)
fig.suptitle(f"Stored quality measurements before vs after enhancement ({len(b)} results; above the dashed line = increased)", y=1.04, fontsize=10); fig.tight_layout(); save(fig, "00_before_after_scores.png")
seconds = np.array([r.processing_duration_ms / 1000 for r in results]); print("processing time: median %.1fs, 90th percentile %.1fs" % (np.median(seconds), np.percentile(seconds, 90)))'''),
("md", "## 4 · The scorecard (numbers measured in notebooks 01-10)"),
("code", '''card = json.loads((OUT / "scorecard.json").read_text())
for model, entry in card.items():
    print(f"\\n== {model}  [{entry['task']}]  {entry['note']}")
    for k, v in entry["metrics"].items(): print(f"   {k:38s} {v}")'''),
("md", """## 5 · Summary
* **Models:** 11 pretrained models in use (plus an optional cloud API for the Pro preset). each was selected, benchmarked here and integrated. A small Zero-DCE fine-tuning experiment (notebook 11) did not beat the official weights and was left out.
* **Our contribution:** the adaptive router (which operation for which defect), the validator gates, the identity safety layer, the quality-control thresholds, the person-folder grouping, and the evidence that each step is judged before it is shown to the user.
* **Evaluation metrics used:** PSNR, SSIM, MAE (against ground truth), sharpness ratio, noise estimate, colorfulness and Lab chroma error, ROC-AUC / EER / true-and-false match rate (identity), recall under degradation (detection), average precision / precision@k (search), processing time.
* **Evaluated and removed:** 4x-UltraSharp (ran 11 times in the stored results, accepted twice, non-commercial licence) and Retinexformer (never beat Zero-DCE) were deleted from the server.
* **Known limits (measured above):** Real-ESRGAN scores lower than bicubic on PSNR/SSIM (it adds texture), NAFNet-SIDD is not better than non-local means on heavy Gaussian noise, DDColor is conservative, proxy labels were used for search."""),
]
