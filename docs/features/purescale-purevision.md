# PureScale and PureVision restoration models

## Status

Two optional, checksum-pinned adapters are implemented behind independent
rollout flags, both defaulting to disabled: `PURESCALE_ENABLED` and
`PUREVISION_ENABLED`. Both checkpoints are third-party and licensed
**CC BY-NC-SA 4.0 (non-commercial)** by
[limitlesslab/AI-upscaling-models](https://github.com/limitlesslab/AI-upscaling-models).
They must not be enabled in any commercial deployment without a separate
license from the author. For this FYP they are suitable for evaluation and
demonstration.

## Models

- `PureScale_2x.pth` (PureScale 2.0, SAFMN architecture)
  - SHA-256: `c8fb698766f502be6409cf3cee1a69d63381c6eebf3ff5de7df6ed87f6c9a5cb`
  - Role: alternative 2x super-resolution candidate to Real-ESRGAN.
  - Architecture reimplemented locally in `enhancement/purescale.py`
    (`dim=128`, `n_blocks=16`, `ffn_scale=2.0`, `upscaling_factor=2`),
    verified against the checkpoint's `state_dict` with `strict=True`.
- `PureVision_1x.pth` (PureVision 1.0, old-arch RRDBNet / ESRGAN family)
  - SHA-256: `3c3bec2111d0f7c3926f3171f37ce28e72502b744e084c566d8960c06a6a06a3`
  - Role: same-resolution compression-artifact removal and mild-blur
    cleanup (JPEG/WEBP/AVC/HEVC/MPEG-2). Fills the same pipeline role
    NAFNet was intended for (see
    [hd-deblur-super-resolution-pipeline.md](hd-deblur-super-resolution-pipeline.md)),
    but NAFNet remains disabled due to unresolved checkpoint corruption
    (T-003), while PureVision passes the same fidelity guard on real
    content.
  - The checkpoint pixel-unshuffles the input by 4 before the RRDB trunk;
    the network's internal 4x upsampling exactly cancels that unshuffle,
    so output dimensions always equal input dimensions.

## Processing order

Both stages are additive and independently gated in `enhancement/services.py`:

1. Exposure correction.
2. Adaptive denoising.
3. **`purevision_restoration_stage`** — optional, same-resolution artifact
   removal. Runs ahead of any resolution-changing stage since it never
   changes the pixel grid.
4. Low-resolution EDSR restoration (legacy fallback).
5. `hd_restoration_stage` — optional NAFNet deblur + Real-ESRGAN 2x/4x.
6. **`purescale_super_resolution_stage`** — optional 2x upscaling. Only
   runs when `hd_restoration_stage` did **not** already apply a
   super-resolution candidate, since stacking two independent upscalers
   would compound scale factors and could exceed
   `PURESCALE_MAX_OUTPUT_PIXELS`.
7. Optional guarded face restoration (GFPGAN).
8. Sharpening and final quality guard.

Recognition continues to use the immutable original or conservative
standardized image; these stages only affect display/download derivatives.

## Safety controls

- SHA-256 verification occurs before checkpoint loading, before any
  PyTorch runtime import.
- PyTorch loads checkpoints with `weights_only=True`.
- `load_state_dict(..., strict=True)` — any architecture/checkpoint
  mismatch is rejected rather than silently loading partial weights.
- Tiled inference bounds working memory (`PURESCALE_TILE_SIZE`,
  `PUREVISION_TILE_SIZE`, with overlap blending).
- PureScale rejects requests whose native output would exceed
  `PURESCALE_MAX_OUTPUT_PIXELS`.
- PureVision candidates have luminance and chroma fidelity limits
  (`PUREVISION_MAX_MEAN_CHROMA_SHIFT`, `PUREVISION_MAX_MEAN_LUMINANCE_SHIFT`),
  mirroring the existing NAFNet guard.
- Any model failure falls back without losing the source image.

## Recommended rollout

Both flags stay disabled pending a real-photo visual QA pass, following the
same conservative rollout precedent as NAFNet/GFPGAN/HD restoration. If
PureVision continues to pass fidelity checks on real content where NAFNet
does not, it is the stronger candidate to promote for T-003's deblur slot.
Confirm the non-commercial license is acceptable for the deployment target
before enabling either flag outside local evaluation.
