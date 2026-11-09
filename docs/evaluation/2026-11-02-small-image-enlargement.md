# Enlarging very small photographs during preprocessing (2026-11-02)

Status: measured on three of the owner's stored photographs (54, 56, 44), each reduced to 150 px (enlarged 4 times) and to 230 px (enlarged 2 times),
on one Apple M1 laptop (8 GB, PyTorch on MPS). **On by default** (the owner asked that a photograph is not refused for its size but made larger): `SMALL_IMAGE_ENLARGE_ENABLED=False` turns it off.

## The problem

A photograph whose shorter side is under 240 px (`QUALITY_REJECT_MIN_EDGE`) is refused by the quality check as `resolution_too_low`. A rejected photograph
is never enhanced, so a small upload was lost before any model could help it. With the setting on, preprocessing enlarges it (2 or 4 times, the
smallest that reaches 480 px, the line below which the enhancement engine would enlarge it again) and every later stage sees the enlarged copy. The
upload is never changed; `ImagePreprocessingResult.enlargement` records the engine, the scale and the original size, and the quality decision carries
`measurements.enlarged_from`.

A picture whose shorter side is under a quarter of that line (60 px with the defaults) is left alone: 4 times is the most that is done, so it would still be
refused as too small and the only result of enlarging it would be invented pixels. This rule was found when the setting became the default and the
existing preprocessing tests, which use 10 to 30 px pictures, were enlarged too.

## How the enlargement is done (and why)

Real-ESRGAN (`RealESRGAN_x4plus`, BSD-3-Clause, already used by the enhancement engine) when `HD_RESTORATION_ENABLED` is on, blended with a cubic
enlargement by `REALESRGAN_OUTPUT_BLEND` (0.35) exactly as the engine does; OpenCV Lanczos otherwise, or when the model is missing, fails, returns the wrong size or
returns something that no longer looks like the input. Measured on the six cases below (ground truth = the same photograph at the larger size, PSNR in dB, higher is
closer to the truth):

| 150 px, x4 | bicubic | Lanczos | model 0.35 | model 0.6 | model 1.0 |
|---|---|---|---|---|---|
| photo 54 | 30.59 | 30.68 | **30.89** | 30.02 | 27.79 |
| photo 56 | 29.98 | 30.07 | **30.75** | 30.71 | 29.70 |
| photo 44 | 40.93 | **41.06** | 40.47 | 39.00 | 36.41 |

| 230 px, x2 | bicubic | Lanczos | model 0.35 | model 0.6 | model 1.0 |
|---|---|---|---|---|---|
| photo 54 | 34.22 | 34.36 | **34.98** | 34.00 | 31.33 |
| photo 56 | 34.19 | 34.33 | **34.81** | 33.82 | 31.22 |
| photo 44 | 44.54 | **44.68** | 43.60 | 41.64 | 38.76 |

* The differences are small (at most about 0.8 dB) and not the same for every photograph: the model at 0.35 is closest to the truth on two of three, and
  OpenCV Lanczos on the heavily compressed photo 44. Neither dominates, so the default is the model with the engine's own blend and OpenCV as the fallback,
  not a stronger blend.
* A larger blend makes the picture sharper and moves it away from the truth (photo 54 at x4: frame sharpness 7 with Lanczos, 33 at 0.35, 206 at 1.0, the truth
  235; PSNR 30.7, 30.9, 27.8). More sharpness is not more fidelity, and a quality score must not be bought with invented detail.
* At x4 on photo 56 the model at 0.35 and 0.6 made the face detector find a **second, doubtful face** (confidence 0.62 and 0.60) that is not there; Lanczos
  and the model at 1.0 did not. A model enlargement can invent plausible structure.
* The face detector already found the one face in all six small photographs (confidence 0.89 to 0.95): these portraits did not need enlarging to be detected.
  Whether enlarging helps a small face inside a small picture was not measured.
* Time: Real-ESRGAN took about 0.9 to 3.1 seconds per enlargement here (PyTorch on the M1 GPU, first load included); OpenCV is instant.

The safety guard: the model's result, shrunk back to the input size, must stay within `SMALL_IMAGE_ENLARGE_MIN_CONSISTENCY_DB` (30 dB) of the input. Measured:
real model output 32.1 to 48.6 dB (all blends, all cases), bicubic 40.5 to 55.0, noise of sigma 40 22.5 to 28.1. So 30 dB separates a real result from garbage.

## What it does to the quality verdict (the product's own `calculate_scores` and `make_decision`, 150 px uploads)

| photograph | as uploaded | enlarged (Lanczos) | enlarged (model 0.35) |
|---|---|---|---|
| photo 54, face | **rejected** `resolution_too_low` (overall 81.0) | **accepted** (68.8) | **accepted** (73.0) |
| photo 56, face | **rejected** `resolution_too_low` (77.3) | **accepted** (72.2) | **accepted** (72.6) |
| photo 44, face found | **rejected** `resolution_too_low` (59.2) | `needs_review` score below 60 (55.2) | `needs_review` score below 60 (55.3) |
| photo 44 treated as having **no face** | **rejected** `resolution_too_low` | **rejected** `severe_blur` | **rejected** `severe_blur` |

Read it honestly:

* For a photograph **with a face** the size is no longer a reason to refuse it, and the verdict falls back on the measures that matter: two of the three become
  `accepted`; the soft one (44) goes to review, which is what a soft photograph should do.
* For a photograph **without a face** the frame's own sharpness counts (blur is only forgiven when there is a face), and an enlargement of a tiny photograph is
  soft: the same photograph is now refused as `severe_blur` instead of `resolution_too_low`. Enlarging cannot add detail that was not photographed, and the
  rule is not loosened to hide that.
* The overall score of a photograph can **go down** after enlarging (81.0 to 68.8 for photo 54): a tiny photograph measures as sharp, a large soft copy as soft.
  Its resolution score rises only because the pixel count does: that is why the decision carries `enlarged_from`.

## Limits and what was not verified

* Three photographs of one account, reduced by `INTER_AREA`; real tiny uploads (thumbnails, screenshots, messaging-app copies) are compressed differently.
* PSNR and SSIM are not what a person sees; LPIPS was not available. Nobody looked at the outputs side by side for this note.
* One machine; the memory and time of Real-ESRGAN on larger inputs is the enhancement engine's, unchanged (the enlargement only runs on photographs under 240 px).
* No Celery worker and no PostgreSQL timing of the upload path.
* A photograph already enlarged here is not enlarged again by the enhancement engine (the target, 480 px, is its own "low resolution" line).
