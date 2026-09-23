# Automatic black-and-white colorization

## Status

Implemented behind `COLORIZATION_ENABLED`, disabled by default pending
rollout QA. Uses SIGGRAPH17 from
[richzhang/colorization](https://github.com/richzhang/colorization)
(**BSD-2-Clause**, permissive — unlike the PureScale/PureVision checkpoints,
no non-commercial restriction applies here).

## Model

- `colorization_siggraph17.pth`
  - SHA-256: `df00044c0a4d7c3edcecf6f75437ce346a66e7a42612d9b968e1a7e17dbc6f66`
  - Role: predicts the ab (colour) channels only, in Lab space, from the L
    (luminance) channel. The source luminance is never modified by the
    network — it is only ever read as input.
  - Architecture reimplemented locally in `enhancement/colorization.py`
    (SIGGRAPHGenerator, no user hints — fully automatic mode), verified
    against the checkpoint's `state_dict` with `strict=True`.

## Automatic gating

Colorization is not a general "make it more colorful" filter — it only ever
runs on images automatically detected as near-grayscale (mean LAB chroma at
or below `COLORIZATION_GRAYSCALE_CHROMA_THRESHOLD`, default `3.0`), set once
in `analyze_original_stage` alongside the existing defect detection. An
already-coloured photo is left untouched even if the flag is enabled.

## Processing order

Colorization runs as the **final** pipeline stage, after `output_guard_b`.
This is a deliberate placement: every earlier stage in this pipeline
(`_preserve_source_chroma`, `_quality_guard`'s `max_chroma_shift`) exists to
keep hue close to the *original* image, because until now nothing in this
pipeline was supposed to change colour deliberately. Colorization is the one
stage designed to introduce a large, intentional chroma change, so it must
run after every chroma-limiting guard, never before — running it earlier
would have those guards detect the new colour as drift and pull it back
toward the (near-zero-chroma) source, silently undoing the colorization.

## Safety controls

- SHA-256 verification occurs before checkpoint loading, before any
  PyTorch runtime import.
- PyTorch loads checkpoints with `weights_only=True`.
- `load_state_dict(..., strict=True)`.
- Inference resizes down to `COLORIZATION_MAX_INFERENCE_EDGE` (default 768px
  longest edge) to bound memory, then the predicted ab is bilinearly
  upsampled back to the source resolution — the L channel used for the final
  image is always the untouched, full-resolution original.
- **Luminance-fidelity guard**: Lab→RGB gamut clipping can distort
  perceived luminance even when the Lab L channel fed to the network is
  untouched, and this codebase measures "luminance" as OpenCV's
  `COLOR_BGR2GRAY` (ITU-R BT.601 luma) everywhere else, which is a
  *different* quantity from Lab's L* and can diverge for saturated colours.
  The adapter re-imposes the exact original BT.601 luma via a YCrCb
  channel-swap (mirroring `_preserve_source_chroma`'s technique), then
  rejects the candidate if residual drift still exceeds
  `COLORIZATION_MAX_MEAN_LUMINANCE_SHIFT` (default `6.0`).
- Any model failure falls back to leaving the source uncoloured.

## Recommended rollout

Stays disabled pending a real-photo visual QA pass on genuine black-and-white
scans (this model is well-established research code, but its colour
plausibility on this project's specific photo distribution hasn't been
evaluated yet). Unlike PureScale/PureVision, the license places no
commercial restriction on enabling it once QA passes.
