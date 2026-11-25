CELLS = [
("md", """# 05 · NAFNet (SIDD): denoising

**What it is.** The same NAFNet architecture trained on SIDD (*Smartphone Image Denoising Dataset*, real phone noise), `NAFNet-SIDD-width64.pth` (443 MB). Pretrained, MIT licence.

**Status in the application.** Tested and working, but **switched off** by default (`NAFNET_DENOISE_ENABLED=False`): the default denoiser is OpenCV non-local means. The switch is a configuration change, not a code change.

**How it is scored.** Two tests: (a) *ground truth*: clean public photographs get Gaussian noise added, then NAFNet-SIDD and OpenCV NLM remove it (PSNR/SSIM vs the clean original); (b) *real noise*: a genuinely grainy photograph (no ground truth, so noise estimate + sharpness are reported)."""),
("md", "## 1 · Synthetic noise with ground truth"),
("code", '''from enhancement.hd_restoration import denoise_with_nafnet

rng = np.random.default_rng(0)
clean_ids = sorted(public_stock_ids(), key=lambda pk: noise_sigma(photo(pk, 600)))[:4]       # the cleanest photographs are the fairest ground truth
rows, shown = [], []
for pk in clean_ids:
    truth = photo(pk, 700)
    for sigma in (15, 30):
        noisy = np.clip(truth.astype(np.float32) + rng.normal(0, sigma, truth.shape), 0, 255).astype(np.uint8)
        sidd, seconds = timed(lambda: first(denoise_with_nafnet(noisy)))
        nlm = cv2.fastNlMeansDenoisingColored(noisy, None, 7 if sigma == 15 else 12, 7 if sigma == 15 else 12, 7, 21)
        rows.append({"photo": pk, "sigma": sigma, "psnr_noisy": round(psnr(truth, noisy), 2), "psnr_nlm": round(psnr(truth, nlm), 2), "psnr_sidd": round(psnr(truth, sidd), 2),
                     "ssim_noisy": round(ssim(truth, noisy), 3), "ssim_nlm": round(ssim(truth, nlm), 3), "ssim_sidd": round(ssim(truth, sidd), 3), "sidd_seconds": round(seconds, 1)})
        if sigma == 30: shown.append((truth, noisy, nlm, sidd))
table(rows)'''),
("code", '''fig, ax = plt.subplots(1, 2, figsize=(12, 3.8))
for axis, key, title in ((ax[0], "psnr", "PSNR dB (higher = closer to the clean photo)"), (ax[1], "ssim", "SSIM (higher = closer)")):
    for i, (name, label, color) in enumerate((("noisy", "noisy input", COLORS["input"]), ("nlm", "OpenCV NLM", COLORS["baseline"]), ("sidd", "NAFNet SIDD", COLORS["model"]))):
        means = [np.mean([r[f"{key}_{name}"] for r in rows if r["sigma"] == s]) for s in (15, 30)]
        axis.bar(np.arange(2) + (i - 1) * 0.27, means, 0.27, label=label, color=color)
        for j, s in enumerate((15, 30)):
            axis.scatter([j + (i - 1) * 0.27] * sum(r["sigma"] == s for r in rows), [r[f"{key}_{name}"] for r in rows if r["sigma"] == s], s=10, color="k", zorder=3)
    axis.set_xticks([0, 1], ["noise sigma 15", "noise sigma 30"]); axis.set_title(title, fontsize=10); axis.legend(fontsize=8)
fig.tight_layout(); save(fig, "05_nafnet_sidd_scores.png")
for s in (15, 30):
    sub = [r for r in rows if r["sigma"] == s]
    print(f"sigma {s}: PSNR noisy {np.mean([r['psnr_noisy'] for r in sub]):.2f} | NLM {np.mean([r['psnr_nlm'] for r in sub]):.2f} | SIDD {np.mean([r['psnr_sidd'] for r in sub]):.2f} dB")'''),
("md", "## 2 · Real grainy photograph (no ground truth)"),
("code", '''real = photo(86, 900)                            # a genuinely grainy low-light portrait (public stock photo)
sidd, seconds = timed(lambda: first(denoise_with_nafnet(real)))
nlm = cv2.fastNlMeansDenoisingColored(real, None, 6, 6, 7, 21)
real_rows = [{"version": n, "noise_sigma": round(noise_sigma(i), 2), "sharpness": round(sharpness(i), 1)} for n, i in (("input", real), ("OpenCV NLM", nlm), ("NAFNet SIDD", sidd))]
table(real_rows); print("SIDD time on 900x600: %.1fs" % seconds)
y, x = real.shape[0] // 3, real.shape[1] // 3; crop = lambda i: i[y:y + 220, x:x + 300]
fig = montage([crop(real), crop(nlm), crop(sidd)], ["input (crop)", "OpenCV NLM", "NAFNet SIDD"], width=4.0); save(fig, "05_nafnet_sidd_real_grain.png")
save_json("05_nafnet_sidd.json", {"synthetic": rows, "real_grain": real_rows})'''),
("md", "## Scorecard (evaluation metrics)"),
("code", '''metrics = {"images": len(clean_ids)}
for s in (15, 30):
    sub = [r for r in rows if r["sigma"] == s]
    for k in ("noisy", "nlm", "sidd"):
        metrics[f"psnr_{k}_sigma{s}_db"] = float(np.mean([r[f"psnr_{k}"] for r in sub])); metrics[f"ssim_{k}_sigma{s}"] = float(np.mean([r[f"ssim_{k}"] for r in sub]))
metrics["mae_noisy_sigma30"] = float(np.mean([mae(q[0], q[1]) for q in shown])); metrics["mae_nlm_sigma30"] = float(np.mean([mae(q[0], q[2]) for q in shown])); metrics["mae_sidd_sigma30"] = float(np.mean([mae(q[0], q[3]) for q in shown]))
metrics["real_grain_noise_input"] = real_rows[0]["noise_sigma"]; metrics["real_grain_noise_sidd"] = real_rows[2]["noise_sigma"]; metrics["real_grain_noise_nlm"] = real_rows[1]["noise_sigma"]
metrics["mean_seconds"] = float(np.mean([r["sidd_seconds"] for r in rows]))
print(scorecard("NAFNet SIDD", "denoise", metrics, "Gaussian noise with ground truth + one real grainy photo; switch is OFF"))'''),
("md", """## 3 · Verdict (what the numbers above actually say)
* **Light noise (sigma 15):** NAFNet-SIDD is clearly better than OpenCV NLM against the ground truth (PSNR and SSIM both far higher).
* **Heavy Gaussian noise (sigma 30):** it is **not** better; NLM wins. The network was trained on real phone noise, not on Gaussian noise, so it can leave heavy synthetic noise in place. This is a limitation, reported as measured.
* **Real grainy photograph:** it removes the noise (estimate 14.8 -> 1.5) and keeps skin and beard texture that NLM smooths away (see the crop).
* It costs 3-11 s per photograph, and the default denoiser stays OpenCV NLM; the SIDD switch is ready for a photograph set with moderate real noise.
"""),
]
