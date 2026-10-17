# The model set: installed, verified, and off by default

FaceCloud's enhancement is OpenCV and Pillow by default (`lightweight_v1`). The learned models below are
**installed and checksum-verified but switched off** until a setting turns them on, and the output of each is
judged by the same quality, face-fidelity and identity guards as any other candidate. Each result records which
policy and models produced it. This page answers "where are the models, and what do they do?" for a review or
defence.

## The pipeline

```
Original image
   -> quality analysis (low light? noise? blur? low resolution? faces?)
   -> low light      Retinexformer (fallback: Zero-DCE, then an OpenCV lift)
   -> noise          NAFNet-SIDD   (fallback: edge-aware non-local means)
   -> blur           NAFNet-GoPro  (motion deblurring)
   -> resolution     Real-ESRGAN   (the only model that changes the pixel grid)
   -> faces          CodeFormer    (alternative: GFPGAN), identity-gated
   -> identity check SFace by default, ArcFace selectable; faces found by YuNet, RetinaFace selectable
   -> final output (OpenCV + Pillow for everything classical)
```

Automatic mode only runs a stage when the analysis measured its defect, and never lifts a dark photo on its own
(a dark photo may be meant to be dark; the lift is the user's choice, by level, style or slider).

## What exists

| Role | Model | File | Size | Setting that turns it on | Needs |
|---|---|---|---|---|---|
| Low light | Retinexformer (NTIRE) | `.runtime/models/Retinexformer_NTIRE.pth` | 6 MB | `RETINEXFORMER_ENABLED` | PyTorch, einops |
| Low light (fallback) | Zero-DCE | `.runtime/models/zero_dce_epoch99.onnx` | 0.3 MB | `ZERO_DCE_ENABLED` | OpenCV only |
| Denoise | NAFNet-SIDD width 64 | `.runtime/models/NAFNet-SIDD-width64.pth` | 464 MB | `NAFNET_DENOISE_ENABLED` | PyTorch |
| Deblur | NAFNet-GoPro width 64 | `.runtime/models/NAFNet-GoPro-width64.pth` | 272 MB | `NAFNET_DEBLUR_ENABLED` + `HD_RESTORATION_ENABLED` | PyTorch |
| Super-resolution | Real-ESRGAN x4plus | `.runtime/models/RealESRGAN_x4plus.pth` | 67 MB | `HD_RESTORATION_ENABLED` | PyTorch |
| Face restoration | CodeFormer | `.runtime/models/codeformer/codeformer.pth` | 377 MB | `CODEFORMER_ENABLED` | PyTorch |
| Face restoration (alternative) | GFPGAN v1.4 | `.runtime/models/gfpgan/GFPGANv1.4.pth` + `gfpgan/weights/` | 349 MB | `GFPGAN_ENABLED` | PyTorch, isolated worker |
| Face detection | YuNet (default), RetinaFace ResNet-50 | `yunet_s_dynamic.onnx`, `gfpgan/weights/detection_Resnet50_Final.pth` | 0.2 / 109 MB | `FACE_DETECTOR_PROVIDER` = `yunet_v1` / `retinaface_v1` | OpenCV / facexlib (worker) |
| Identity | SFace (default), ArcFace `w600k_r50` | `face_recognition_sface_2021dec.onnx`, `.runtime/models/arcface/w600k_r50.onnx` | 39 / 174 MB | `IDENTITY_PROVIDER` = `sface_2021dec_v1` / `arcface_w600k_r50_v1` | OpenCV only |

Removed on 2026-10-15 (recorded, with sources and checksums, in `retired-capabilities.md`): EDSR, PureVision,
PureScale and colourisation.

`python scripts/model_manifest.py verify` checks every file against `docs/gpu/model_manifest.json`;
`python scripts/fetch_models.py` re-downloads a missing one from its public source and keeps it only if it matches
the pinned SHA-256 (it never changes a pin). The architectures of Retinexformer and CodeFormer are vendored in
`enhancement/archs/` with their licence headers and load the authors' checkpoints strictly.

## Four policies

| `ENHANCEMENT_RENDER_POLICY` | What runs | Needs |
|---|---|---|
| `lightweight_v1` (default) | OpenCV and Pillow only | nothing |
| `balanced_v1` | the same, plus **Zero-DCE on the CPU, only when a lift is requested** | the Zero-DCE file; no PyTorch, no GPU |
| `learned_v1` | every model whose own setting is on | PyTorch |
| `adaptive_v1` | the **adaptive engine** ([adaptive-engine.md](adaptive-engine.md)): analyse, run only the operations the defects call for, validate every candidate (quality, colour, artifacts, identity), return one image or keep the original | PyTorch, the models whose setting is on |

## The sliders

The editor sliders have a tone half (brightness, contrast, shadows, highlights, saturation, warmth, sharpness) and a
**model half**: low-light enhancement, noise reduction, deblur and face restoration. A model slider is 0-100 and
blends that model's result into the photo; 0 leaves it alone, 100 is the full candidate. They run in the order above,
before the tone controls, and each model's candidate is cached under its input's hash so moving one slider recomputes
only what comes after it. A slider whose model is off falls back to a classical OpenCV equivalent (low light, noise,
deblur) or is reported as not applied (face restoration). The models are therefore not decoration: they are what the
sliders move. Details and the mapping from the dashboard's sliders: `dashboard-sliders.md`. The sliders are explicit
requests, so they need only the model's own setting, not the rendering policy.

## Showing it (for the panel)

1. **They are there and verified:** `GET /api/system/health/` (administrator) lists every model as `healthy`
   or `disabled`, with `installed: true` and `checksum_verified: true`. Or: `python scripts/model_manifest.py verify`.
2. **They work:** `python scripts/build_model_sheet.py --images <photos> --out sheet_dir` runs every model on real
   photos against their clean originals and writes an HTML page, a contact sheet and the measured numbers
   (`docs/evaluation/2026-10-15-model-sheet.md` has the results from the project's own photos).
3. **They drive the sliders:** `GET /api/enhancements/editor/configuration/` says, per slider, which engine serves it
   and whether it is available.
4. **They are off by default:** every result's `applied_parameters.render_policy` says which policy produced it.

To switch the models on for a demonstration, set in `.env`: `RETINEXFORMER_ENABLED=True`,
`NAFNET_DENOISE_ENABLED=True`, `NAFNET_DEBLUR_ENABLED=True`, `HD_RESTORATION_ENABLED=True`,
`CODEFORMER_ENABLED=True`, and `ENHANCEMENT_RENDER_POLICY=learned_v1` for the automatic pipeline.

## Environments

PyTorch 2.14 is installed in the main environment (`venv`, Python 3.14) and in the isolated worker environment
(`.runtime/venvs/gfpgan`, Python 3.12). Every adapter above runs in either, except GFPGAN and the RetinaFace
detector, which need `facexlib`/`gfpgan` and so the worker. After building the worker environment from
`requirements-gfpgan.txt`, follow its OpenCV step: plain `opencv-python` overwrites the contrib build and breaks the
OpenCV super-resolution module. Devices: Apple MPS or CUDA when present, otherwise CPU; an explicitly configured
device that is missing is an error, never a silent move.

## Licences

Retinexformer, NAFNet and Real-ESRGAN are MIT/BSD. **CodeFormer (S-Lab License 1.0) and the pretrained ArcFace
weights are non-commercial research licences**: fine for this academic project, not for a commercial service.
The Zero-DCE licence is stated as approved by the project owner and is not on file here. See
`docs/model-licenses/model-set-2026-10.md`.

## Why they are off by default

* Automatic mode must not decide, from pixels alone, that a dark photo should be brightened.
* Generative restoration (CodeFormer, GFPGAN) cannot be proven to keep the person, so identity checks gate it.
* Two checkpoints are non-commercial, and the heavy ones need PyTorch (a GPU for acceptable speed).
* Result: a faster, smaller, predictable default; the models remain an explicit choice.
