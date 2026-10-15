# HD deblur and super-resolution pipeline

## Status

Real-ESRGAN 2x display upscaling is implemented behind `HD_RESTORATION_ENABLED`.
NAFNet is integrated behind the separate `NAFNET_DEBLUR_ENABLED` rollout flag.
Both flags default to disabled.

## Models

- `NAFNet-GoPro-width64.pth`
  - SHA-256: `329d3ab4077b8d6b7ff61de376e483714667960bf85be027bf4335cda701196f`
  - Intended role: motion deblurring.
  - Current status: disabled because visual QA produced checkerboard and chroma corruption. The runtime fidelity guard rejects such candidates.
- `RealESRGAN_x4plus.pth`
  - SHA-256: `4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1`
  - Role: native 4x inference followed by a configurable 2x/4x output.
  - Current status: checkpoint structure and isolated inference verified.

## Processing order

1. Exposure correction.
2. Adaptive denoising.
3. Optional NAFNet deblurring.
4. Real-ESRGAN super-resolution.
5. Optional guarded face restoration.
6. Sharpening and final quality guard.

Recognition continues to use the immutable original or conservative standardized
image. HD restoration is a display/download derivative and must not replace the
recognition source.

## Safety controls

- SHA-256 verification occurs before checkpoint loading.
- PyTorch loads checkpoints with `weights_only=True`.
- Tiled inference bounds working memory.
- Native 4x output has a configurable pixel ceiling.
- Real-ESRGAN is conservatively blended with a bicubic source-grid reference.
- NAFNet candidates have luminance and chroma fidelity limits.
- Any model failure falls back without losing the source image.

## Recommended rollout

Enable Real-ESRGAN on a dedicated GPU Celery queue first. Keep NAFNet disabled
until a checkpoint passes the sample dataset and identity-preservation tests.
