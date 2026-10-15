# Baseline of the lightweight pipeline, 2026-10-15

Taken after the learned models were retired (`docs/enhancement/retired-capabilities.md`),
with `scripts/evaluate_pipeline_cases.py`: six real photographs (held out earlier, none
of them a portrait; they are not committed), each degraded in eight known ways, run
through the real `enhance_bgr` (level `auto`, style `natural`, longest edge reduced to
1024 px), CPU only, Apple M1. SSIM is to the clean photograph, so a rise means the
output moved toward it. Raw rows: `fix-validation/2026-10-15/cleanup/lightweight_baseline/`.
Images are not kept.

| Case | SSIM before | SSIM after | Mean time | Photos left untouched | Reading |
|---|---|---|---|---|---|
| good | 1.000 | 1.000 | 0.21 s | 5 of 6 | One photo got a light highlight recovery |
| dark | 0.510 | 0.508 | 0.56 s | 4 of 6 | **By design**: automatic mode withholds the lift (`lift_withheld`); choose a level to lift |
| noisy | 0.883 | 0.947 | 0.56 s | 0 of 6 | Measured denoising works, with the denoise guard accepting it |
| blurred | 0.884 | 0.888 | 0.18 s | 0 of 6 | Light sharpening only; blur is reported as beyond the pipeline |
| compressed | 0.944 | 0.943 | 0.18 s | 5 of 6 | No sharpening is applied to a photo with block artefacts |
| overexposed | 0.750 | 0.753 | 0.18 s | 0 of 6 | Clipped highlights are not recoverable |
| grayscale | 1.000 | 1.000 | 0.19 s | 5 of 6 | No colourisation any more |
| group | 1.000 | 1.000 | 0.38 s | 5 of 6 | |
| transparent | 0.657 | 0.657 | 0.19 s | 6 of 6 | Alpha preserved exactly in all six |

Peak process memory 528 MB. Identity was `inconclusive` for every photo (no usable face
in the set; one blurred photo had one face detected). This run does not show that the
output is *better* than the learned pipeline for dark, blurred or low-resolution
photos: it is not, and the retirement accepted that (ADR-024).

## Regression fixtures that remain (what verifies each concern)

| Concern | Where |
|---|---|
| Decoding, ICC to sRGB, orientation, 16-bit, alpha | `uploads/test_decoding.py`, `uploads/test_decoding_paths.py`, `uploads/test_decoding_refs.py`, `enhancement/test_edit_lifecycle.py` |
| Colour (chroma kept, LAB lightness only), warmth and saturation | `enhancement/test_controls.py`, `enhancement/tests.py` |
| Transparency | `enhancement/test_staged_pipeline.py` (`TransparencyTests`), `enhancement/test_previews.py`, `enhancement/test_edit_lifecycle.py` |
| Detail retention and artefacts | `enhancement/test_denoise.py`, `test_denoise_guard.py`, `test_blur_measure.py` (dead-leaves chart), `test_conservative_routing.py`, `test_quality_analysis.py` |
| Output quality guards and neutral identity | `enhancement/tests.py` (`_quality_guard`), `test_controls.py`, `test_render_policy.py` |
| Faces | `enhancement/test_face_guard_outcomes.py`, `test_identity_guard.py`, `test_identity_bypass.py`, `test_comparison_identity.py` |
| Queue, claims, cancellation | `enhancement/test_claim_retry.py`, `test_execution_ownership.py`, `scripts/check_queue_integration.py`, `scripts/check_advisor_queue.py` |
