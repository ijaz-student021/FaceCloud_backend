# The learned models: installed, verified, and off by default

FaceCloud ships two enhancement policies. The default, **`lightweight_v1`**, uses OpenCV
and Pillow only. The earlier learned models are **still in the backend, installed and
checksum-verified, but disabled** by that policy. Nothing falls back to them silently,
and each result records which policy produced it. This page is the answer to "where
are the models?" for a review or defence.

> History: on 2026-10-15 these models were briefly removed (commit `d61b96b`) and then
> restored the same day by request (`feat/keep-disabled-models`), so that they can be shown.
> Their sources and checksums are recorded below and in `docs/gpu/model_manifest.json`.

## What exists

| Model | What it does | File | Needs | Setting that turns it on |
|---|---|---|---|---|
| Zero-DCE | learned exposure curves for dark photos | `.runtime/models/zero_dce_epoch99.onnx` | OpenCV only | `ZERO_DCE_ENABLED` |
| EDSR x2 | super-resolution of small photos | `.runtime/models/EDSR_x2.pb` | OpenCV only | `AI_RESTORATION_ENABLED` |
| NAFNet (GoPro) | deblurring | `.runtime/models/NAFNet-GoPro-width64.pth` | PyTorch | `NAFNET_DEBLUR_ENABLED` + `HD_RESTORATION_ENABLED` |
| Real-ESRGAN x4+ | HD upscaling | `.runtime/models/RealESRGAN_x4plus.pth` | PyTorch | `HD_RESTORATION_ENABLED` |
| GFPGAN v1.4 | face restoration (identity-gated) | `.runtime/models/gfpgan/GFPGANv1.4.pth`, `gfpgan/weights/` (detector, parser) | PyTorch, isolated worker | `GFPGAN_ENABLED` (+ `GFPGAN_QUEUE_ENABLED`) |
| PureVision 1x | compression-artefact removal | `.runtime/models/PureVision_1x.pth` | PyTorch | `PUREVISION_ENABLED` |
| PureScale 2x | super-resolution | `.runtime/models/PureScale_2x.pth` | PyTorch | `PURESCALE_ENABLED` |
| Colourisation (SIGGRAPH17) | colourises black-and-white photos | `.runtime/models/colorization_siggraph17.pth` | PyTorch | `COLORIZATION_ENABLED` |

Always used, independent of the policy: **YuNet** (face detection) and **SFace** (identity).

Licences to mention: PureVision and PureScale are CC BY-NC-SA 4.0 (non-commercial);
GFPGAN is used under a private permission recorded outside the repository; colourisation
is BSD-2-Clause. The private curve model (`facecloud_curve_best_opencv.onnx`) has no
public source and is not installed on this machine.

## Showing it (for the panel)

1. **They are there and verified:** `GET /api/system/health/` (administrator) lists every
   model with `status: disabled`, `installed: true`, `checksum_verified: true` and the
   reason `disabled by the lightweight_v1 rendering policy`. Or from a shell:
   `python scripts/model_manifest.py verify` (12 files OK) and
   `python scripts/fetch_models.py --list`.
2. **They are off by default:** every result's `applied_parameters.render_policy` says
   `lightweight_v1`, and `learned_stages_allowed: false`.
3. **They work when switched on:** set `ENHANCEMENT_RENDER_POLICY=learned_v1` (the
   models whose own setting is on then run). Zero-DCE and EDSR need nothing else;
   NAFNet, Real-ESRGAN, GFPGAN, PureVision, PureScale and colourisation need PyTorch (the
   isolated Python 3.12 worker environment: `requirements-gfpgan.txt`, started with
   `scripts/start_gfpgan_worker.sh`). **That environment is not installed on this machine at
   the moment**; the OpenCV-only models can be shown immediately.
4. **Same photo, both policies:** `python scripts/compare_policies.py --image photo.jpg --darken 0.28 --out compare.jpg`
   runs the pipeline twice (one process per policy) and writes the two results side by side.
   Measured on a portrait darkened to 28 % brightness: the default leaves it dark in automatic
   mode (mean luminance 27 to 27, algorithm `opencv_adaptive_restoration_router_v2`); with
   `learned_v1` Zero-DCE lifts it (27 to 57, `zero_dce_hybrid_v1`).

If a file is missing or altered: `python scripts/fetch_models.py` downloads each one from its
public source and keeps it only if it matches the pinned SHA-256 (it never changes a pin).

## Why they are off

* Automatic mode must not decide, from pixels alone, that a dark photo should be brightened.
* Generative face restoration cannot be shown to keep the person, so identity checks gate it.
* Two checkpoints are non-commercial; the others add a second Python environment and queue.
* No GPU path (CUDA) has been validated.
* Result: faster, smaller, predictable default; the optional models remain an explicit choice.

## Sources and checksums

Pinned in `docs/gpu/model_manifest.json`. Public sources: Zero-DCE
(github.com/Li-Chongyi/Zero-DCE, exported with `scripts/export_zero_dce_onnx.py`), EDSR
(github.com/Saafke/EDSR_Tensorflow), NAFNet (megvii-research/NAFNet, GoPro docs),
Real-ESRGAN (xinntao/Real-ESRGAN v0.1.0), GFPGAN (TencentARC/GFPGAN v1.3.0), facexlib weights
(xinntao/facexlib v0.1.0, v0.2.2), PureVision and PureScale (limitlesslab/AI-upscaling-models),
colourisation (richzhang/colorization).
