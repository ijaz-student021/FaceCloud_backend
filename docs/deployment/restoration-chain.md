# Restoration chain: deblur, face restoration, upscale to HD

What it is, what a server needs to run it, how to check, and every setting. Evidence for what it does is in
`docs/evaluation/2026-10-17-restoration-chain.md`.

## What happens to a photograph

1. **Acceptance never turns on blur.** A photograph with a face is not rejected or sent to review for blur or a low resolution
   (`quality_control/services.py`). Sharpness is measured on the face (`enhancement/face_sharpness.py`), so a portrait-mode photograph
   (sharp face, blurred background) is not penalised, and a blurred subject in front of a sharp background is noticed. Only an image whose
   shorter edge is below `QUALITY_REJECT_MIN_EDGE` (240 px, "too small to process") or a photograph with **no** face and a severely blurred
   frame is rejected for these reasons. The quality decision records `sharpness_basis` (`face` or `frame`), `face_blur_warning`,
   `frame_laplacian_sharpness` in `measurements`.
2. **Enhancement.** With `ENHANCEMENT_RENDER_POLICY=adaptive_v1` the adaptive engine decides everything. With the default
   `lightweight_v1` (or `balanced_v1`) and `ENHANCEMENT_RESTORATION_ESCALATION=True` (default), a photograph whose **face is blurred or whose
   resolution is low** is sent through the same engine **if this server can run the stages it needs**; every other photograph takes the
   policy's own lightweight pipeline, unchanged. If the stages cannot run here the lightweight pipeline runs and the result says so
   (`hd_unavailable_reason`), it is never presented as restored.
3. **The chain** (each stage validated; a result that fails a safety check is discarded, never blended in):

| Stage (`applied_stages[].stage`) | Model | Used when | Default |
|---|---|---|---|
| `deblur` | NAFNet-GoPro (MIT) | the main face is strongly blurred (`ADAPTIVE_FACE_BLUR_HIGH`), or motion blur in the frame | on |
| `face_restore` | CodeFormer (S-Lab, **non-commercial**), else GFPGAN | the main face is degraded and large enough | **off** (licence) |
| `upscale` | Real-ESRGAN x4plus (BSD-3-Clause), at x`HD_RESTORATION_OUTPUT_SCALE` | the photograph is small (shorter edge under 480 px) | on |

The whole frame is enhanced; faces are never cropped out. The identity guard stays on: a face-restoration result is accepted only when the
face still matches the upload, and when identity cannot be verified (no consent, face too dark) the result says so and no generative face
restoration is applied.

## What a server must have

| Stage | File (default location) | Size | SHA-256 | Settings |
|---|---|---|---|---|
| deblur | `.runtime/models/NAFNet-GoPro-width64.pth` | 272 MB | `329d3ab4077b8d6b7ff61de376e483714667960bf85be027bf4335cda701196f` | `NAFNET_DEBLUR_ENABLED`, `NAFNET_MODEL_PATH`, `NAFNET_MODEL_SHA256` |
| upscale | `.runtime/models/RealESRGAN_x4plus.pth` | 67 MB | `4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1` | `HD_RESTORATION_ENABLED`, `REALESRGAN_MODEL_PATH`, `REALESRGAN_MODEL_SHA256`, `HD_RESTORATION_DEVICE`, `HD_RESTORATION_OUTPUT_SCALE` |
| face_restore | `.runtime/models/codeformer/codeformer.pth` (or GFPGAN) | | `1009e537...684b7` | `CODEFORMER_ENABLED`, `CODEFORMER_MODEL_PATH`, `GFPGAN_ENABLED`; the production guard refuses these unless `NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True` |

Also PyTorch in the worker environment (`requirements-gfpgan.txt` / the worker image), and YuNet (face detection) for the analysis.

```sh
python scripts/fetch_models.py --only nafnet,realesrgan     # downloads and checks each file against its pinned SHA-256
python manage.py check_restoration --verify                 # which stages can run here, and why not
python manage.py check_restoration --require deblur upscale # exit code 1 unless both can (for a deployment check)
```

`check_restoration` prints, per stage, whether it is switched on, whether the checkpoint is on disk, whether PyTorch imports, the device
(`cuda`, `mps` or `cpu`) and, with `--verify`, whether the checksum matches. `--json` gives the same as data.

## On a VPS

Real-ESRGAN is not practical on a CPU (a 0.3 MP photograph measured about 5 minutes on one Apple M1 CPU core set; see
`docs/deployment/hostinger-readiness-2026-10.md`). The defaults are on because the product's promise is HD; **on a CPU-only worker set
`HD_RESTORATION_ENABLED=False`** (the result then says `hd_unavailable_reason: ... switched off`) and keep `NAFNET_DEBLUR_ENABLED=True` if
the worker has about 2 GB free for it (NAFNet-GoPro: 3 to 4 s at 0.3 MP, 14 s at 1.2 MP on that CPU). A GPU worker runs everything; CUDA code
paths are untested here. Run inference workers at `--concurrency=1`; the engine's `ADAPTIVE_TIME_BUDGET_SECONDS` stops a run that cannot
finish and keeps the original.

## Every setting this feature reads

| Setting | Default | Meaning |
|---|---|---|
| `QUALITY_FACE_SHARPNESS_TARGET` | 100 | face detail that scores 100 (a sharp face is about 119) |
| `QUALITY_FACE_REGION_INSET` | 0.15 | the face box is shrunk by this share on every side before measuring |
| `QUALITY_FACE_MIN_CONFIDENCE` | 0.5 | faces below this are not "the" face for sharpness |
| `QUALITY_FACE_BLUR_WARNING` | 50 | face detail below this records `face_blur_warning` |
| `QUALITY_REJECT_MIN_EDGE` | 240 | a photograph with a shorter edge below this is "too small to process" (rejected) |
| `QUALITY_REVIEW_MIN_EDGE` | 480 | review for a small photograph **without** a face only |
| `ENHANCEMENT_RESTORATION_ESCALATION` | True | send photographs that need restoration through the chain under the lightweight policies |
| `NAFNET_DEBLUR_ENABLED` / `HD_RESTORATION_ENABLED` | True | the two permissively licensed stages |
| `CODEFORMER_ENABLED` / `GFPGAN_ENABLED` | False | non-commercial face restorers |
| `ADAPTIVE_FACE_BLUR_HIGH` | 25 | face detail below this is "strongly blurred" (deblur) |
| `ADAPTIVE_FACE_DEBLUR_MIN_DETAIL_GAIN` | 1.25 | a deblur chosen for a blurred face must raise the face's detail this many times |
| `ADAPTIVE_UPSCALE_LANDMARK_SHIFT_MAX` | 0.06 | largest landmark shift (share of face width) an upscaled candidate may show; other operations 0.04 |
| `HD_RESTORATION_OUTPUT_SCALE` | 2 | upscale factor (x2 keeps memory and time bounded) |
| `HD_RESTORATION_MAX_NATIVE_OUTPUT_PIXELS` / `HD_RESTORATION_MAX_OUTPUT_PIXELS` | 20,000,000 | output size bounds |
| `ADAPTIVE_MAX_OUTPUT_EDGE` | 4096 | the engine will not enlarge beyond this long edge |
| `ADAPTIVE_FACE_UPSCALE_BELOW` | 0 (off) | also upscale when the face's shorter edge is below this |
| `ADAPTIVE_TIME_BUDGET_SECONDS` | 180 | wall-clock budget per photograph |
| `ENHANCEMENT_MAX_INPUT_PIXELS` | 24,000,000 | larger inputs are reduced first |
