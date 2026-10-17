# Model attribute audit — 2026-10-14

Sources: adapter code at `464e5ae` [static], `docs/gpu/model_manifest.json`,
`.runtime/model_validation/2026-10-14-final/` [CPU]/[MPS], loader timing probe
[MPS]. Device = what actually ran on the Apple M1. **No CUDA run exists.**

## 1. Attributes per model

| Attribute | Zero-DCE (public) | EDSR x2 | NAFNet | Real-ESRGAN | PureVision | PureScale |
|---|---|---|---|---|---|---|
| Purpose / defect | Exposure curves for low light | 2x super-resolution for low-res | Motion deblur | 4x SR (used at 2x) | JPEG artefact removal | 2x SR |
| Path | `.runtime/models/zero_dce_epoch99.onnx` | `.runtime/models/EDSR_x2.pb` | `.runtime/models/NAFNet-GoPro-width64.pth` | `.runtime/models/RealESRGAN_x4plus.pth` | `.runtime/models/PureVision_1x.pth` | `.runtime/models/PureScale_2x.pth` |
| Source | Official Epoch99 (Li-Chongyi/Zero-DCE), exported with torch 2.13.0 | Saafke/EDSR_Tensorflow | megvii NAFNet (GoPro w64), official Drive link | xinntao v0.1.0 | limitlesslab `PureVision` | limitlesslab `PureScale2` (SAFMN) |
| SHA-256 | `e5cb6f92…dae14` | `58562322…d9b4` | `329d3ab4…196f` | `4fa0d389…82f1` | `3c3bec21…06a3` | `c8fb6987…a5cb` |
| Runtime checksum | **Yes**, pinned, cached by file identity | **No** (existence only) | Yes, re-hashed **every call** | Yes, every call | Yes, every call | Yes, every call |
| Runtime | OpenCV DNN | OpenCV dnn_superres | PyTorch | PyTorch | PyTorch | PyTorch |
| Strict weights | n/a (ONNX) | n/a | `strict=True`, `weights_only=True` | same | same | same |
| Colour / range | BGR→RGB by `swapRB`, /255, curves applied to full-res | BGR in/out uint8 | BGR→RGB, [0,1] float, clamp | same | same | same |
| Padding | none (proxy resize ≤1024) | none | reflect-free zero pad to ×16, cropped | none | none | none |
| Finite-value check | curve tensor shape only | non-empty | tile raw range ∈ [-0.5, 1.5] else input tile (new) | same | same | same |
| Native / output scale | 1x | 2x | 1x | 4x → 2x (reduced per tile) | 1x | 2x |
| Limits | inference edge 1024 | input short edge ≤ 480; output ≤ 20 MP | – | output ≤ 20 MP (native limit only at 4x) | – | output ≤ 20 MP |
| Device | CPU | CPU | auto: CUDA→MPS→CPU (ran MPS; CPU identical output) | MPS | MPS | MPS |
| Tile / overlap | curves applied in 1024 tiles | none | 256 / 24, uint16 average | 256 / 24 | 256 / 24 | 256 / 24 |
| Memory retry | – | – | halve tile ×2, min 64, then fallback | same | same | same |
| Cache key | resolved path only (not checksum) | (path, scale) | (kind, path, device) | same | (path, device) | (path, device) |
| Strength / blend | adaptive strength by scene luminance, ≤ 0.85 (extreme) | full | full, then guards | 0.35 blend with bicubic | full | full |
| Eligibility | `low_light`/`severe_low_light` defect | `low_resolution` + edge ≤ 480 | HD on **and** measured blur (input defects — see P1-4) | HD on | PUREVISION_ENABLED, not extreme dark | PURESCALE_ENABLED and no earlier upscaler |
| Fidelity / rejection | guard A (luminance, contrast, clipping, chroma) | guard B | chroma ≤ 8, luminance ≤ 30 mean shift; guard B; face check | guard B; face check | chroma ≤ 8, lum ≤ 30; guard B | guard B; face check |
| Last safe image | yes (stage runner) | yes | yes; deblur survives failed SR | yes | yes | yes |
| Measured | 0.6 s @1400 px [CPU] | 13 s @360→720 px [CPU] | 7 s @900 px [MPS]; 1.34 GB GPU | 11 s @640 px; 1.1 GB GPU | 3.5 s @900 px; 1.1 GB | 8 s @640 px; 1.1 GB |
| Quality evidence | Recovers synthetic darkening; brightens intentional low-key | Runs; no visual study | 3-7 dB PSNR gain on synthetic motion blur; diverges on near-black tiles | No measured gain on real photos | Mixed; raised clipping on a clean photo | Subtle; no visible defect |
| State | **On** | **On** | Off | Off | Off (licence) | Off (licence) |

| Attribute | GFPGAN v1.4 (+ RetinaFace, ParseNet) | Colorization SIGGRAPH17 | YuNet | SFace |
|---|---|---|---|---|
| Purpose | Restore degraded faces (generative) | Colour for grayscale | Face detection | Face embedding |
| Source | TencentARC v1.3.0; facexlib v0.1.0 / v0.2.2 | richzhang S3 `siggraph17-df00044c.pth` | libfacedetection.train (BSD-3) | opencv_zoo (Apache-2.0) |
| SHA-256 | `e2cd4703…e5ad` (+ `6d1de9c2…`, `3d558d8d…`) | `df00044c…6dc66` | `daede240…10a2d3` | `0ba9fbfa…7e79` |
| Runtime checksum | Yes, all three, **every call** (471 ms) | Yes, every call (87 ms) | Yes, cached | Yes, cached |
| Runtime | gfpgan 1.3.8 / basicsr 1.4.2 / facexlib 0.3.0 in the worker env; torchvision shim | PyTorch | OpenCV FaceDetectorYN | OpenCV FaceRecognizerSF |
| Contract | BGR uint8, 512 crops, paste back, weight 0.30 | Lab L in, ab out at ≤ 768 edge, pad ×8; luminance restored exactly | BGR, any size, 5 landmarks | aligned 112×112 crop → 128-d |
| Device | MPS | MPS | CPU | CPU |
| Blend / eligibility | output blend 0.25 (min 0.08); blur/low-res defects; identity gate (bypassed only under AI Max, reported) | only near-grayscale sources | – | similarity ≥ 0.80 for "verified" |
| Fidelity | SSIM ≥ 0.90, mean change ≤ 12, p95 ≤ 42, chroma ≤ 3, clipping ≤ 1 pp; face check; identity guard | luminance shift ≤ 6 only | – | – |
| Consent | Requires a verifiable identity unless AI Max; refused without consent | – | detection only | needs consent |
| Measured | 6.6-16 s per image incl. load [MPS], 2.5 GB GPU; real-photo median 2.5 s | 2.4-3.7 s; 2.3 GB GPU | 0.05 s | 0.2 s; rebuilt per guard call (44 ms) |
| Quality evidence | Visibly restores a degraded face; subtle at 0.25 blend; identity 0.97-0.99 | **Blotches** on 2 of 2 portraits | 1 face found on 3 portraits; Haar alternative false-positives | 0.98 brightened, 0.93 mirrored/blurred self-similarity |
| State | Off, optional | Off | **On** | **On** |

**Private curve model** (`facecloud_curve_best_opencv.onnx`, manifest
`f4805a62…f220`): missing. Not required: `ZERO_DCE_MODEL_PATH` points at the
public export. It would need its own `ZERO_DCE_MODEL_SHA256` if used.

**EDSR integrity, specifically:** `_load_edsr_model` (`services.py:359`) checks
only `is_file()`. No other runtime control verifies it; `scripts/model_manifest.py
verify` and `gpu_preflight.py` check the hash offline, when someone runs them.
The actual guarantee is therefore "whatever file is at the configured path".

## 2. Thresholds: units, rationale, calibration, held-out evidence

| Threshold | Value | Units | Rationale | Calibration data | Held-out? | Sensitivity / known issue |
|---|---|---|---|---|---|---|
| Blur `severe` / `mild` | Laplacian variance < 35 / < 90 | grey-level² | Pre-existing | None recorded | No | **Falls 5-14x with resolution and 16x with 4x darkening** (P1-4) |
| Noise visible | median abs residual > 5 | grey levels | Pre-existing | None | No | Integer-quantised; texture reads as noise |
| Low light | mean < 65 / < 115 | grey levels | Pre-existing | None | No | Cannot tell intent |
| Overexposed | clipping > 8 % (> 20 % → HIGH) | % pixels | ADR-020 era | 4 + 18 photos | partially | – |
| JPEG blockiness | strong ≥ 1.35, weak ≥ 1.15 | ratio | 8-px grid steps | 4 photos | **Yes, 18 photos** | Resized JPEGs read clean (safe direction) |
| Denoise ladder | 0,3,4,7,10,13,16 at 1,2,3.5,5,7,9 | NLM h | ADR-019 | Synthetic Gaussian, 4 photos | No | Real sensor noise differs |
| Denoise acceptance: removed ≥ 1.0, share ≥ 0.30 | 1.0 / 0.30 | grey levels / fraction | ADR-021 | 4 photos, synthetic | **Yes, 18 photos** | Resolution-dependent per-pixel noise |
| Removed-residual whiteness | ≥ 3.0 | ratio | ADR-021 | all 22 in view | No | Correlated real noise reads lower |
| Detail / edge retention | ≥ 0.85 | amplitude ratio | guard vs destructive candidate | blur candidate ≤ 0.82 | n/a (guard) | Never fired on the real denoiser |
| Face fidelity SSIM / texture / chroma / clipping | 0.85 / 0.60-1.80 / 4.0 / 1.0 | various | ADR-017 | synthetic + 1 portrait | No | Haar false positives; uncalibrated on real restorations |
| GFPGAN guard | see table above | various | pre-existing | None recorded | No | – |
| Identity minimum | 0.80 cosine | SFace similarity | pre-existing | real photos (doc) | unknown | Similarity is evidence, not proof; dark faces < 45 grey not judged |
| NAFNet / PureVision fidelity | chroma ≤ 8, lum ≤ 30 | mean abs Lab | pre-existing | None | No | Missed a single bad tile (now caught by the tile range check) |
| Tile validity | raw ∈ [-0.5, 1.5] | model units | 2026-10-14 | 1 failing tile | No | Detects divergence, not subtle tile errors |
| Pixel budget | 24 MP | pixels | time/memory | measured CPU | n/a | – |
| Claim lease / visibility | 1800 s / 4500 s | seconds | ADR-018 | queue test (short values) | n/a | See P2-7 |

## 3. Calibration gaps

- No threshold was calibrated on real degraded photos with ground truth. Synthetic degradations, unpaired real-photo review and SSIM to an original are three different kinds of evidence; only the first gives paired measurement, and it is synthetic.
- No group photographs; three skin tones at most; no real night, motion-blur or compression samples.
- No CUDA timing, memory or numerical-parity data.
- Haar remains the fallback detector and produced false faces on synthetic images.
