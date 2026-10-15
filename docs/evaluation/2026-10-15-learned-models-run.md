# The learned models, run on one real photo, 2026-10-15

Environment: `.runtime/venvs/gfpgan` (Python 3.12.13, torch 2.14.0, torchvision 0.29.0, Apple M1, MPS available, no CUDA),
policy `learned_v1`, each model switched on in turn. The input was a phone portrait (600x800) given the defect that model is
meant for. `ZERO_DCE_ENABLED` is on in the local `.env`, so Zero-DCE also ran in every case. One photo, one run each:
this shows that each model **runs and its stage is accepted by the guards**, not how good it is on average.

| Model | Defect given | Seconds | Stage result | What the output looked like |
|---|---|---|---|---|
| Zero-DCE | darkened to 28 %, noise added | 1.4 | accepted | strong lift; visible grain |
| EDSR x2 | downscaled to 300x400 | 19.0 (includes model load) | accepted (after the OpenCV fix below) | 600x800, clean |
| Real-ESRGAN x4+ | downscaled to 300x400 | 8.6 | accepted | 600x800, clean |
| NAFNet (GoPro) | Gaussian blur, sigma 4 | 26.7 (runs with Real-ESRGAN) | accepted | still blurry: the model is trained for motion blur, not a Gaussian one |
| PureScale 2x | downscaled to 300x400 | 5.3 | accepted | 600x800, clean |
| PureVision 1x | JPEG quality 12 | 2.8 | accepted | blocks visibly reduced |
| GFPGAN v1.4 | 300x400 and lightly blurred | 8.2 (26.6 with EDSR) | accepted | difference barely visible on a small face |
| Colourisation | converted to grey | 3.8 | accepted | invents colour: the maroon shirt became bright red, so it is a guess, not a recovery |

## Problem found and fixed

EDSR failed in the new environment (`cv2.dnn_superres` had no `DnnSuperResImpl_create`). Cause: `opencv-python` and
`opencv-contrib-python-headless` were both installed (gfpgan and basicsr require the plain one) and overwrite each other's
`cv2` files. Removing the plain package and reinstalling the contrib one restored EDSR; the gfpgan stack still loads. The
steps are now in `requirements-gfpgan.txt`.

## What this does not show

* Quality on a range of photos: one portrait, one run per model, no scoring.
* Speed on a GPU or in production: the times are the M1 with MPS or CPU, including some model load time.
* GFPGAN identity gating was not exercised on a hard case.
