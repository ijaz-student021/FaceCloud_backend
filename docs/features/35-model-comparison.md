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
| `natural` | The default pipeline as users get it: classical exposure, measured denoise and conditional sharpening |
| `dynamic` | The default pipeline plus the bounded, guarded vivid contrast/colour boost |
| `ai_max` | Highest strength with adaptive denoise: the strongest local correction. No learned restoration model exists. |

**Retired profile:** `opencv_baseline` (the pipeline with the Zero-DCE model switched
off) was retired on 2026-10-15 together with Zero-DCE: it is now identical to
`natural`. Asking for it is a `400` that says so; it is not run as another
profile. A comparison made before the retirement still shows its output, labelled
"OpenCV adaptive baseline (retired)". See `docs/enhancement/retired-capabilities.md`.

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
   dynamic, ai_max). A riskier profile must be meaningfully
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
- Profiles never change the pixel grid. (A comparison made before the upscaling
  stages were retired may hold a larger preview; its metrics were normalised to the
  source grid.)

## Example (historical)

On 2026-10 a dark night street photograph (mean luminance 11.6 of 255) was run
through the then-current profiles: the classical baseline lifted it to 18.8
(quality score 59.9) while the Zero-DCE profiles reached about 100 (score 83-84).
That gap is what the lightweight policy gave up: automatic mode now withholds the
lift from dark photos (their intent cannot be read from the pixels), and a chosen
level lifts them with the classical correction only, which is weaker than Zero-DCE
was on very dark photos (docs/enhancement/lightweight-pipeline.md, section 3).

## Verification

`enhancement/test_comparison.py`: every branch of the recommendation
rule (gap, near-tie, keep-original, guard, faces, clipping, empty); profile
validation; real-pipeline runs producing metrics and previews for every profile;
the retired profile being refused with its reason (and still readable in an earlier comparison); failure isolation per profile and overall;
unreadable sources; idempotence; downscaling; the CSV report; and the API
(queueing, defaults, validation, preprocessing and ownership rules, the
concurrency limit, an unavailable queue, detail, list, previews, report, deletion
removing files, login required); plus retention cleanup and stuck-job recovery.
