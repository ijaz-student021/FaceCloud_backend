# Feature 35: Model comparison

## Status

Complete for the backend. The frontend needs a comparison page (source and
profile previews side by side, the metrics table, the recommendation, and a
report download).

## Purpose

Evidence for the project and for a reviewer: what does each enhancement approach
actually do to a real photograph? One image is run through several profiles and
the results are measured and shown next to the untouched source, with timing and
an explainable recommendation. Nothing here changes what users get by default.

## Profiles

| Profile | What it is |
|---|---|
| `original` | The untouched source at the comparison size (always included, for reference) |
| `opencv_baseline` | Classical exposure, denoise and sharpen only. The learned Zero-DCE model is switched off for this run. |
| `natural` | The default pipeline as users get it; uses Zero-DCE when the photo is low-light |
| `dynamic` | The default pipeline plus the bounded, guarded vivid contrast/colour boost |
| `ai_max` | Highest strength with adaptive denoise. Licence-gated restoration models still follow the platform flags. |

`opencv_baseline` versus `natural` is the "OpenCV against Zero-DCE" comparison the
project asked for. It is done by a new `use_zero_dce` switch on `enhance_bgr`
(default on, so nothing else changes) rather than by touching global settings, so
it is safe under concurrent workers. On a well-lit photo the two are identical,
because Zero-DCE is only used for low light; the algorithm name in each output
says what actually ran.

## What is measured

Every profile runs on the same copy of the image, shrunk to at most
`COMPARISON_MAX_EDGE` (default 1280 px) so the numbers are comparable and the job
stays bounded on CPU. For the source and each profile:

- The quality metrics the platform already uses: brightness, contrast, sharpness,
  noise, shadow and highlight clipping. Outputs are measured on the source pixel
  grid, so an upscaling profile is not rewarded for having more pixels.
- The quality scores from Feature 14 (brightness, sharpness, contrast, noise,
  exposure, resolution, and an overall score out of 100).
- Faces found by the configured detector, and the identity-guard outcome.
- Processing time and the pipeline's own warnings.
- A downscaled JPEG preview.

## The recommendation

An explicit rule, not a model's opinion (`recommend` in
`enhancement/comparison.py`):

1. Each profile's **adjusted score** is its overall quality score minus penalties:
   identity guard unresolved (-20) or forced to reduce strength (-2); faces that
   were detectable in the source are no longer detected (-15); clipped shadows and
   highlights up by more than 5 points (up to -15).
2. The best profile wins, but any profile within 1 point of the top counts as
   tied and the **more conservative** one is chosen (natural, then
   opencv_baseline, dynamic, ai_max). A riskier profile must be meaningfully
   better.
3. If no profile beats the original by at least 1 point, the recommendation is
   `original` with the reason "keep the original". A comparison that says nothing
   helped is a valid result.

The reason string spells out the scores, and each output stores its adjusted score
and penalty notes.

## Endpoints

All owner-only.

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/enhancements/images/{id}/compare/` | Start a comparison. Body: `level` (default `auto`), optional `profiles`. Returns 202. |
| GET | `/api/enhancements/comparisons/` | Your comparisons (summaries) |
| GET | `/api/enhancements/comparisons/{id}/` | Full result: outputs, metrics, scores, previews, recommendation |
| DELETE | `/api/enhancements/comparisons/{id}/` | Delete it and its preview files |
| GET | `/api/enhancements/comparisons/{id}/outputs/{profile}/preview/` | Inline JPEG preview (`original` included) |
| GET | `/api/enhancements/comparisons/{id}/report/` | CSV evaluation report, one row per profile |

The image must be preprocessed. The job runs in Celery (`enhancement.run_model_comparison`)
and is covered by the stuck-job reaper. A profile that fails is recorded as failed
with a generic reason and the others still complete; the comparison fails only if
every profile does.

## Limits and housekeeping

- At most `MAX_ACTIVE_COMPARISONS_PER_USER` (default 2) run at once; more get 429.
  Requests are also throttled (`model_comparison`, 30/hour).
- Previews are copies of the user's photo, so finished comparisons are deleted
  after `COMPARISON_RETENTION_DAYS` (default 7) by a daily task
  (`enhancement.cleanup_comparisons`, 04:30). Users can delete one sooner.
- Previews live under `users/<id>/comparisons/`, which the orphaned-media scanner
  knows about.
- A profile that upscales a small image (the low-resolution restorer) produces a
  larger preview than the source preview; the metrics are still normalised to the
  source grid.

## Example: a real dark photograph

A dark night street photograph (mean luminance 11.6 of 255), CPU only, ~16 s for
the whole comparison:

| Profile | Mean luminance | Quality score | Time | Ran |
|---|---|---|---|---|
| original | 11.6 | 51.5 | | |
| opencv_baseline | 18.8 | 59.9 | 1.6 s | classical only |
| natural | 101.1 | 83.4 | 3.9 s | Zero-DCE hybrid |
| dynamic | 100.4 | 83.7 | 3.8 s | Zero-DCE + vivid boost |
| ai_max | 100.7 | 84.1 | 6.4 s | Zero-DCE |

The classical baseline barely lifts a photo this dark; the learned model is what
rescues it. The three learned-model profiles are within 1 point of each other, so
the rule recommends the conservative `natural`. This run is asserted by
`enhancement/test_comparison_real_image.py` (skipped when the sample photo or the
Zero-DCE model is not present).

## Verification

`enhancement/test_comparison.py` (32 tests): every branch of the recommendation
rule (gap, near-tie, keep-original, guard, faces, clipping, empty); profile
validation; real-pipeline runs producing metrics and previews for every profile;
the baseline never calling Zero-DCE; failure isolation per profile and overall;
unreadable sources; idempotence; downscaling; the CSV report; and the API
(queueing, defaults, validation, preprocessing and ownership rules, the
concurrency limit, an unavailable queue, detail, list, previews, report, deletion
removing files, login required); plus retention cleanup and stuck-job recovery.
