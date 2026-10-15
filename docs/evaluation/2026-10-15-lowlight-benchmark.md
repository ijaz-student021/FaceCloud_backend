# Low-light benchmark on the CPU, 2026-10-15

`scripts/benchmark_lowlight.py`: 12 real photographs (11 from an earlier held-out set, plus a portrait),
each darkened to 40%, 28% and 18% brightness with sensor-like noise (36 dark copies), enhanced under six
configurations and scored against the clean original. CPU only (Apple M1). Raw rows and the table:
`fix-validation/2026-10-15/lowlight_benchmark/`. The photos are not committed.

| Configuration | Exposure error (lower) | Structure SSIM (higher) | Colour error (lower) | Noise | Seconds |
|---|---|---|---|---|---|
| automatic (default policy) | 117.75 | 0.9599 | 9.36 | 0.89 | 0.90 |
| lightweight, level high | 101.04 | 0.9387 | 9.58 | 1.97 | 0.89 |
| lightweight + chroma pre-filter, high | 101.36 | **0.9542** | 9.06 | 1.50 | 1.11 |
| lightweight + NLM pre-filter, high | 101.37 | 0.9495 | 8.91 | **1.22** | 1.20 |
| balanced (Zero-DCE, CPU), medium | 95.49 | 0.9509 | 8.19 | 1.11 | 1.93 |
| balanced (Zero-DCE, CPU), high | **86.92** | 0.9418 | **7.84** | 1.11 | 1.82 |

Exposure error is the difference of mean luminance from the clean photo; structure SSIM compares
brightness-normalised luminance, so it measures detail and noise, not brightness.

## Reading it

* **Automatic mode keeps the highest structure score only because it does nothing** (the exposure
  error is the worst, 117.8): it withholds the lift by design.
* **The pre-filter pays off.** With the same lift, the chroma pre-filter raises structure SSIM from
  0.939 to 0.954, lowers estimated noise from 1.97 to 1.50 and the colour error from 9.58 to 9.06, for about
  0.2 s. NLM removes more noise (1.22) at a slightly lower structure score and does not scale to large photos
  (it is limited to 4 MP and falls back to the chroma filter). The default is `chroma`.
* **Zero-DCE on the CPU (`balanced_v1`) gives the best exposure and colour** (exposure error 86.9 against 101.0, colour
  error 7.84 against 9.58, noise 1.11) in about 1.8 s per photo, with no GPU and no PyTorch.
* **No configuration reaches the clean brightness.** The quality guards cap how far one pass may move a photo
  (the level's luminance-gain limit), so a photo at 40% or 28% brightness is lifted by about half of what it
  needs. This is the conservative design, and it is why a dark photo is improved, not "restored".

## Limits

12 photographs, synthetic darkening and noise (a real low-light capture has other defects), one machine.
The photos are mostly not portraits, so skin-tone behaviour was not measured here. Not a claim about any
photograph outside this set.
