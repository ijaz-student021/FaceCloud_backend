# Adaptive engine: the "polish" operation (2026-10-24)

Status: measured on the owner's four stored photographs and a fixed golden set, on one Apple M1 laptop. **Not merged, not enabled anywhere**:
`ADAPTIVE_POLISH_ENABLED` defaults to `False`.

## Why

In the adaptive engine every operation runs only when the analyzer reports its defect, so a well-lit, sharp photograph ends as
`original_preserved` at every level. The product owner asked that a dark photograph keep the dark logic and a normal one get a simple, small
improvement, while a photograph that is genuinely balanced and sharp must still be reported honestly as unchanged.

## Diagnosis first (before the code went into the repository)

A prototype outside the repository was run on the four stored photographs, with the real analyzer, face detector, identity model and
validator limits, and found three things that changed the design:

1. **The textbook version of step (a) is not gentle.** CLAHE at the style's share (25 %) raised the noise estimate by x1.17 on the two
   heavily compressed portraits (photos 54 and 56) and moved the detected face landmarks by 1.9 % of the face width on photo 56. An unsharp
   mask alone did neither (noise x1.00 to x1.01, drift 0.14 to 0.16 %). The cause is that CLAHE's change map also lifts grain and JPEG
   blocks and compresses the global tone range (frame contrast fell by 2 % on a contrasty photograph). Keeping only the band of the change
   map between sigma 4 and 32 (at the 1024 px working scale) removed all three effects (noise x1.00 to x1.03, drift under 0.4 %, frame
   contrast up, not down).
2. **A "frame contrast" measured as local contrast is circular.** CLAHE and an unsharp mask always raise local contrast by 8 % or more, so
   a gate on it passes everything and nothing is ever reported as already balanced. The gate therefore uses the main face's detail and the
   frame's contrast (standard deviation of luminance), as the task states; local contrast is only checked for not getting worse.
3. **An already sharp face must not be sharpened.** Photo 54's face already measures 219 (a sharp face's 90th percentile is 175, its median
   119). The unsharp amount inside a face falls from 0.57 of the frame's amount at detail 119 to zero at 175, and the gate refuses a face
   pushed past that range.

## What it does, and when

See `docs/enhancement/adaptive-engine.md` section 5a. In one line: OpenCV and NumPy only, offered by the router only to a photograph with no
recommended operation, none tried or applied and no dark scene, kept only if a measurable gain passes every gate, otherwise the original
pixels come back with `skipped_stages: [{"stage": "polish", "reason": "Already balanced and sharp: nothing to improve."}]`.

## The four stored photographs (real router, operation, analyzer, validator and identity model; settings: adaptive_v1, Zero-DCE on, the other models off)

| photo | style | variant | mean brightness | contrast (std) | face detail | frame sharpness | mean dE | face drift | new clipped highlights (points) | noise level | identity similarity | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 44 | natural | blend 1.0 | 72.7 -> 72.5 | 34.7 -> 35.4 | - | 0.52 -> 0.56 | 0.69 | 0.00 % | +0.00 | 0.25 -> 0.25 | - | REFUSED: no_measurable_improvement |
| 44 | natural | blend 0.65 | 72.7 -> 72.5 | 34.7 -> 35.2 | - | 0.52 -> 0.55 | 0.52 | 0.00 % | +0.00 | 0.25 -> 0.25 | - | REFUSED: no_measurable_improvement |
| 44 | dynamic | blend 1.0 | 72.7 -> 72.5 | 34.7 -> 36.4 | - | 0.52 -> 0.59 | 1.19 | 0.00 % | +0.00 | 0.25 -> 0.25 | - | REFUSED: no_measurable_improvement |
| 44 | dynamic | blend 0.65 | 72.7 -> 72.6 | 34.7 -> 35.8 | - | 0.52 -> 0.57 | 0.83 | 0.00 % | +0.00 | 0.25 -> 0.25 | - | REFUSED: no_measurable_improvement |
| 53 | natural | not offered (dark scene left as photographed) | | | | | | | | | | | |
| 53 | dynamic | not offered (dark scene left as photographed) | | | | | | | | | | | |
| 54 | natural | blend 1.0 | 101.3 -> 101.1 | 72.5 -> 73.0 | 219 -> 220 | 1.45 -> 1.53 | 0.85 | 0.07 % | +0.06 | 0.16 -> 0.16 | 0.9972 | REFUSED: no_measurable_improvement |
| 54 | natural | blend 0.65 | 101.3 -> 101.1 | 72.5 -> 72.8 | 219 -> 219 | 1.45 -> 1.51 | 0.62 | 0.05 % | +0.03 | 0.16 -> 0.16 | 0.9984 | REFUSED: no_measurable_improvement |
| 54 | dynamic | blend 1.0 | 101.3 -> 101.0 | 72.5 -> 73.7 | 219 -> 228 | 1.45 -> 1.61 | 1.56 | 0.23 % | +0.09 | 0.16 -> 0.16 | 0.9925 | REFUSED: no_measurable_improvement |
| 54 | dynamic | blend 0.65 | 101.3 -> 101.1 | 72.5 -> 73.3 | 219 -> 228 | 1.45 -> 1.56 | 1.07 | 0.12 % | +0.04 | 0.16 -> 0.16 | 0.9965 | REFUSED: no_measurable_improvement |
| 56 | natural | blend 1.0 | 96.4 -> 96.2 | 56.8 -> 58.0 | 128 -> 144 | 1.85 -> 1.97 | 1.10 | 1.69 % | +0.28 | 0.24 -> 0.24 | 0.9948 | CHOSEN |
| 56 | natural | blend 0.65 | 96.4 -> 96.3 | 56.8 -> 57.6 | 128 -> 140 | 1.85 -> 1.93 | 0.78 | 1.72 % | +0.19 | 0.24 -> 0.24 | 0.9979 | accepted, not preferred |
| 56 | dynamic | blend 1.0 | 96.4 -> 96.4 | 56.8 -> 59.3 | 128 -> 152 | 1.85 -> 2.09 | 2.00 | 0.76 % | +0.64 | 0.24 -> 0.25 | 0.9832 | REFUSED: highlight_clipping_increased |
| 56 | dynamic | blend 0.65 | 96.4 -> 96.4 | 56.8 -> 58.4 | 128 -> 143 | 1.85 -> 2.00 | 1.36 | 0.60 % | +0.43 | 0.24 -> 0.24 | 0.9901 | CHOSEN |

Reading it. Photo 53 is a dark scene with a visible face and is left as photographed, so polish is not offered. Photos 44 and 54 are
refused: 44 is soft (defocus) but the polish gains under 5 % contrast and cannot recover defocus; 54's face is already sharp (219) and its
contrast moves under 2 %. Photo 56 is polished (face detail 128 to 144, +12.5 %; contrast +2.1 %; mean CIELAB distance 1.1; identity
similarity 0.995). Style dynamic at full blend is refused for photo 56 because it clips 0.64 points of new highlights (limit 0.5), and the
0.65 blend is chosen instead.

**One number above the task's test target.** The detected face landmarks on photo 56 moved by 1.69 % (natural, blend 1.0) of the face
width; the task's unit test asks for under 1.5 % on a clean portrait. The synthetic test portrait passes it (the test reads the pixels
through a detector that finds the skin-toned patch), the real photograph does not, and the production gate allows 2 % for polish (the other
operations allow 4 %). The other photographs and variants moved by 0.00 to 0.23 %; photo 56's value is YuNet's own jitter on a compressed
face (the unsharp-only run moved it by 0.16 %), not a changed face (identity similarity 0.995).

## Golden before/after (nothing else changed)

Fixed inputs: the owner's stored photographs 44, 53, 54 and 56, a dark one (photo 54 x0.40), a noisy one (photo 54 plus Gaussian noise
sigma 18), a blurred one (photo 54, Gaussian blur sigma 5), a small one (photo 54 at 300 px wide) and a clean synthetic scene; each at the
levels auto, medium and high and the styles natural and dynamic: 54 runs per configuration. Configuration **A** is the server's settings
(adaptive_v1, Zero-DCE on, GFPGAN, CodeFormer, Real-ESRGAN and NAFNet-deblur off). Configuration **B** switches those four on, so deblur,
upscale and face restoration run too. Photo 57 (a degraded face) was added in B to exercise face restoration with the real CodeFormer.
Every output was hashed (SHA-256 of the pixels) and every run's outcome, summary, algorithm, applied and skipped stages were stored,
outside the repository, before the change and after it. **The unmodified code is deterministic** (two runs of each configuration gave
identical hashes and records in 54 of 54 runs, and 6 of 6 for photo 57), so a hash comparison means something.

| Configuration | Runs | Flag absent vs before | Flag `False` vs before | Flag `True`: runs that already apply an operation | Flag `True`: runs that changed |
|---|---|---|---|---|---|
| A (server settings) | 54 | 0 hash and 0 record differences | 0 and 0 | 12 of 12 identical (low light x6, denoise x6) | 24, all `original_preserved` before: 12 polished (photo 56 x6, synthetic scene x6), 12 refused, record only (photo 44 x6, photo 54 x6: the extra `skipped_stages` entry) |
| B (four more models on) | 54 | 0 and 0 | 0 and 0 | 24 of 24 identical (low light, denoise, deblur and upscale, x6 each) | the same 24 |
| B, photo 57 (degraded face) | 6 | 0 and 0 | 0 and 0 | 6 of 6 identical (face restoration x6) | 0 |

So with the flag off or absent nothing differs, and with it on a photograph that already gets low light, denoise, deblur, face
restoration or upscale gets byte-identical pixels and the identical record; only photographs that were `original_preserved` can change,
and only to a polished result that passed the gate (hash changes) or to the same pixels plus the refusal entry (record only).

Existing data: a manifest of every existing result, edit version, share link and share access (32 rows) and of every output file (57
files) was taken from the owner's database read-only before the change and again after: **identical** (no row and no file changed). The
`GET /api/enhancements/results/<id>/` JSON of all 26 stored results, serialised on a copy of the database by the old and the new code,
is byte-identical. Five result rows appeared in between (31 to 35): they come from the owner's own use of the server; the work in this
branch never opens that database.

## Limits and what was not verified

* Four photographs of one account, two configurations, one machine. Not a sample of real uploads; the face detector's jitter and the
  5 % threshold are measured on these and nowhere else.
* "Better" here is a set of measurements (face detail, contrast, noise, colour distance, identity similarity), not a judgement of how a
  person sees it. Photo 56's polish is small by design (mean CIELAB distance 1.1, below the 2.3 that is just noticeable).
* The honest result for a photograph that is already balanced and sharp is no change: two of the three photographs offered polish got none.
  A product that wants every photograph visibly different cannot be built on this gate without dropping the 5 % rule.
* Face restoration was exercised on one photograph (57) with CodeFormer, deblur and upscale on the blurred and the small variant of photo 54.
* No GPU, no real Celery worker, no PostgreSQL ranking of anything: this is a pure image operation. Large photographs (18 megapixels) were
  run; the strips keep its memory use bounded, the peak was not profiled.
