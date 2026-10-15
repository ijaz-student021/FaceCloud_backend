# Denoising: NAFNet-SIDD against OpenCV, on three kinds of noise

Reproduce: `python scripts/evaluate_denoising.py --images <photos> --out <dir>` (16 phone photographs at 640 px, the real NAFNet-SIDD
checkpoint, Apple M1). All noise is **synthetic**; there was no real smartphone noise pair available, so nothing here shows how the model
performs on real phone noise. Cases: Gaussian (sigma 6, 12, 20); `phone_*`, shot plus read noise in linear light re-encoded as JPEG quality
85; `lifted_*`, a photograph darkened by 8 or 16 in linear light and brightened by the real Retinexformer (8 photographs), i.e. the grain a
low-light lift actually leaves. "Face texture kept" is the face region's structure energy over the clean photograph's (1.0 keeps it).

| Case | Method | PSNR | SSIM | LPIPS | Face texture kept | Grain left |
|---|---|---|---|---|---|---|
| Gaussian 6 | none | 32.58 | 0.953 | 0.142 | 1.04 | 4.9 |
| | NAFNet-SIDD, full | 33.17 | 0.958 | 0.134 | 0.81 | 1.2 |
| | **NAFNet-SIDD, 65 % blend** | **35.06** | **0.970** | **0.071** | 0.88 | 2.4 |
| | OpenCV edge-aware NLM | 34.55 | 0.960 | 0.112 | 1.01 | 1.6 |
| Gaussian 12 | none | 26.66 | 0.850 | 0.356 | 1.14 | 8.4 |
| | NAFNet-SIDD, full | 29.90 | 0.920 | 0.241 | 0.74 | 0.9 |
| | **NAFNet-SIDD, 65 % blend** | **31.18** | **0.934** | **0.183** | 0.86 | 3.4 |
| | OpenCV edge-aware NLM | 29.36 | 0.919 | 0.230 | 1.08 | 3.1 |
| Gaussian 20 | none | 22.34 | 0.699 | 0.585 | 1.30 | 13.1 |
| | NAFNet-SIDD, full | 23.88 | 0.767 | 0.499 | 0.94 | 5.2 |
| | NAFNet-SIDD, 65 % blend | 24.52 | 0.779 | 0.477 | 1.04 | 7.9 |
| | OpenCV edge-aware NLM | **24.70** | **0.817** | **0.418** | 1.20 | 6.3 |
| Phone-like, light (0.002) | none | 34.04 | 0.934 | 0.191 | 1.07 | 4.6 |
| | NAFNet-SIDD, full | 33.11 | 0.956 | 0.138 | 0.84 | 1.7 |
| | **NAFNet-SIDD, 65 % blend** | **34.82** | **0.961** | **0.116** | 0.91 | 2.6 |
| | OpenCV edge-aware NLM | 34.60 | 0.946 | 0.161 | 1.03 | 1.6 |
| Phone-like, heavy (0.008) | none | 28.51 | 0.817 | 0.423 | 1.17 | 9.1 |
| | **NAFNet-SIDD, full** | **31.29** | **0.910** | **0.221** | 0.89 | 3.2 |
| | NAFNet-SIDD, 65 % blend | 31.27 | 0.895 | 0.276 | 0.98 | 5.3 |
| | OpenCV edge-aware NLM | 30.17 | 0.887 | 0.308 | 1.10 | 3.5 |
| After a lift (8x darker) | none | 19.66 | 0.743 | 0.336 | 1.28 | 4.0 |
| | NAFNet-SIDD, full | 19.50 | 0.759 | 0.389 | 1.00 | 2.1 |
| | NAFNet-SIDD, 65 % blend | 19.61 | 0.758 | 0.356 | 1.09 | 2.7 |
| | OpenCV edge-aware NLM | 19.55 | 0.734 | 0.381 | 1.20 | 1.5 |
| After a lift (16x darker) | none | 20.19 | 0.677 | 0.405 | 1.39 | 5.9 |
| | NAFNet-SIDD, full | 20.18 | 0.716 | 0.450 | 1.05 | 3.2 |
| | NAFNet-SIDD, 65 % blend | 20.26 | 0.707 | 0.418 | 1.16 | 4.0 |
| | OpenCV edge-aware NLM | 20.16 | 0.698 | 0.423 | 1.29 | 2.5 |

## What it shows

* **NAFNet-SIDD helps on moderate and heavy noise**: +3.2 dB and LPIPS 0.356 -> 0.241 on Gaussian 12, +2.8 dB and LPIPS 0.423 -> 0.221 on
  heavy phone-like noise, clearly ahead of OpenCV there. On very heavy Gaussian noise (sigma 20) OpenCV is ahead on the pixel metrics.
* **At full strength it smooths faces**: face texture kept is 0.74-0.89 against OpenCV's 1.0-1.1. A 65 % blend recovers part of it
  (0.86-0.98) and is *better* than full strength on light-to-moderate noise (Gaussian 6: LPIPS 0.071 against 0.134). This supports
  the engine proposing two variants (blend 1.0 and 0.65) and letting the validator prefer the smaller change; it does not support
  one fixed strength.
* **Light noise should be left alone or blended**: full-strength NAFNet lowers PSNR on light phone-like noise (34.04 -> 33.11). The router
  denoises only from the "moderate" level (about Gaussian sigma 10), so this case is not routed.
* **A denoise after a low-light lift does not help** (PSNR unchanged, LPIPS worse for NAFNet: 0.336 -> 0.389). SIDD-trained denoising
  smudges lift grain. A rule "denoise after every strong lift" was therefore **not added**. The visible grain left by a lift is a known limit.

## What it does not show

Real smartphone noise (none was available), other cameras, other skin tones, or a human preference. The `phone_*` model is a
simulation. The table is the reason to prefer the two-variant design and not a validation of NAFNet-SIDD on real photographs;
before any claim about real noise, run `scripts/evaluate_denoising.py` on pairs from a licensed real-noise dataset (SIDD or DND
under their terms) kept apart from anything used to set thresholds.

## Analyzer change that came out of it

The 3x3 median-residual noise estimate cannot see grain a few pixels wide (after a strong lift it read 2.0 for a result with clearly
visible grain). `analyzer.flat_noise` measures the fine-scale residual over flat regions only; its thresholds are the existing,
already-calibrated "moderate" and "high" definitions translated by measurement (Gaussian sigma 10 reads 7.25 and sigma 25 reads 15.9;
clean photographs read 1.9 on average, maximum 4.3). The router and the validator use the larger of the two measures (`noise.level`,
1.0 = moderate). Correlated grain is still under-read (grain of sigma 9.8 reads 6.0), so this narrows the blind spot and does not close it.
