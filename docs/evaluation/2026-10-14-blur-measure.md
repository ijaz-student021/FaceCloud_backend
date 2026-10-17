# Blur measure: study and thresholds, 2026-10-14

Evaluation record for `enhancement.services.measure_blur` (audit finding P1-4).
Classical processing only: no model is involved in measuring blur.

## Problem

Blur used to be decided by the Laplacian variance of the full-size image
(severe below 35, mild below 90 grey-level²). That number depends on things that
are not blur:

| Same picture | Laplacian variance | Old verdict |
|---|---|---|
| Sharp dead-leaves chart, darkened ×0.25 | 83.5 (1229 undarkened) | mild blur |
| Flat grey frame | 0 | severe blur |
| Sharp 3000×5333 close-up portrait | 9.4 (33.5 at 1600 px wide) | severe blur |
| Real photos, median over 498 (study below) | 62 native → 363 at 800 px; noise ×10 | — |

The verdict decided sharpening, NAFNet deblurring, GFPGAN eligibility, the
recorded plan and denoise protection, so sharp night photos and sharp
high-resolution photos were routed to blur corrections.

## Measure

On a copy reduced to a long edge of 1024 px (never enlarged): the median, over
the strongest 2 % of gradients, of the local maximum of |Laplacian| divided by
the gradient magnitude. This is roughly the inverse of the edge width in pixels.

* A gain cancels in the ratio, so exposure does not change it.
* A fixed measuring scale makes it a property of the picture, not of its pixel
  count.
* Only the strongest edges count, so soft backgrounds and noise in flat areas do
  not decide it.

`uncertain` (no blur defect, no blur correction) when the 5–95 % tonal range is
under 8 grey levels (a flat or near-black frame), or the strongest gradients are
under 1 grey level per pixel (quantisation steps only; a smooth gradient carries
no evidence of focus).

## Study

* **Photos:** 498 JPEG/PNG files from the owner's Downloads folder. These are phone photos,
  stock photos and some graphics; they include no group photos, and
  the set is not representative of any user population. Split into
  **calibration (270)** and **evaluation (228)** by a hash of the file name,
  before any threshold was chosen.
* **Variants per photo:** native size (when larger than 2400 px), 1600 and
  800 px, darkened ×0.5 and ×0.25, Gaussian noise σ 8, JPEG quality 30;
  Gaussian blur σ 0.7 / 1.2 / 2.0 / 3.5 and linear motion blur 5 / 11 px,
  sizes given at the 1024-px measuring scale; severe blur combined with
  darkening and with noise; mild blur with JPEG 30.
* **Labels:** an unaltered photo is *sharp*, σ 1.2 and 5-px motion *mild*,
  σ ≥ 2 and 11-px motion *severe*, σ 0.7 *borderline*.

### Thresholds (chosen on the calibration half only)

| Threshold | Value | Units | Rule |
|---|---|---|---|
| mild below | 0.422 | edge ratio | 1st percentile of sharp variants: ~1 % of sharp photos flagged |
| severe below | 0.232 | edge ratio | best separation of σ 1.2 from σ 2.0 |
| uncertain: tonal range under | 8 | grey levels | 0.6 % of severe variants, 2.7 % of sharp (graphics, near-uniform frames) |
| uncertain: strongest gradients under | 1 | grey level / px | 0.4 % of severe variants |

### Evaluation half (thresholds not tuned on it)

| Variant | Sharp | Mild | Severe | Uncertain |
|---|---|---|---|---|
| Sharp photos: native / original / 1600 / 800 px | 94–97 % | 3–5 % | 0–1.6 % | 0–0.5 % |
| Sharp, darkened ×0.25 | 92.5 % | 3.1 % | 0 % | 4.4 % |
| Sharp + noise σ 8 / JPEG 30 | 97–98 % | 2–3 % | 0 % | 0 % |
| Gaussian σ 1.2 (mild) | 14 % | 82 % | 4 % | 0.4 % |
| Gaussian σ 2.0 (severe) | 0 % | 12 % | 88 % | 0.4 % |
| Gaussian σ 3.5 | 0 % | 0 % | 99 % | 1.3 % |
| Motion 11 px (severe) | 29 % | 55 % | 15 % | 0.4 % |
| σ 2.0 + darkened ×0.25 | 0 % | 29 % | 65 % | 5.7 % |
| σ 2.0 + noise σ 8 | 7 % | 72 % | 21 % | 0 % |

**The 3000×5333 close-up portrait**, as uploaded, darkened ×0.25 and at 1600 px:
**sharp** each time (old verdict: severe blur).

**Unaltered photos read as blurred (evaluation half): 8 of 228.** These were
reviewed by eye at the measuring scale by the engineer, not by an independent
annotator. 5 look genuinely soft or out of focus, 1 is a false positive (a
graphic of smooth shapes), and 2 are borderline (shallow depth of field). The
"sharp" label of the original photos is therefore an assumption that is
slightly wrong in a few cases.

## Where the verdict remains uncertain or wrong

* **Directional motion blur is under-detected.** Edges parallel to the motion
  stay sharp, so 29 % of 11-px motion blurs read sharp and most of the rest read
  mild. The error is in the safe direction: no correction is applied.
* **Severity is understated under noise and darkness.** A σ 2 blur with noise
  or ×0.25 darkening mostly reads mild. It is still detected as blur (93 % or
  more), but GFPGAN, which by default needs *severe* blur, is less often
  eligible.
* **Mild and borderline overlap:** a third of σ 0.7 blurs read mild.
* The measure is global. A blurred face in a sharp scene reads sharp, so faces
  are not judged on their own.
* No real (camera-made) blurred photos with ground truth were available; blur
  was synthetic. This is the main calibration gap.

## Consumers

One input verdict now drives sharpening, conservative routing, the plan
(including `deblur`, which used to promise deblurring whenever NAFNet was on),
the NAFNet gate, GFPGAN eligibility and denoise protection. The Laplacian
variance is still reported as `laplacian_sharpness` for comparison. The new
fields are in `quality_before.blur` (additive).

Study scripts were kept outside the repository; their raw rows contain only
file names and numbers. The commands and outputs are in
`fix-validation/2026-10-14/`.
