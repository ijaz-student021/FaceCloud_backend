# The enlargement of a lifted photograph (2026-11-06)

A brief for the owner's back-lit demo said: after Zero-DCE brightens a dark face, "the safety gate compares the brightened result to the original DARK image and blocks the upscaler with
`face_geometry_changed`", so the 2x Real-ESRGAN enlargement is not applied (the output keeps the original's size); the proposed fix was a setting `UPDATE_BASELINE_AFTER_LIGHTING` that makes every later
check compare against the brightened image. This note is what was measured, what the premise got right and wrong, and what was changed. Real models (Zero-DCE, Real-ESRGAN, CodeFormer, SFace) on an Apple M1
(MPS); the engine called directly (`AdaptiveImageEnhancementService.enhance`), **not** through the API or the UI.

## 1. What the premise got wrong

Every per-step check of the validator already judges a step against the image it was given, which after a lift is the lifted photograph: `service._propose` passes `current.image` and the analysis of it
(`report`) to `validator.evaluate`, so data consistency, colour, halo, noise and **highlight clipping** all compare with the lifted image. Only two checks read the upload: the identity embedding and the
face-landmark share limit (with its plain-enlargement rule).

The owner's database (live checkout, 50 results with an enlargement step, 15 of them refused):

| refusal | count | what they were |
|---|---|---|
| `face_geometry_changed` | 14 | 10 with no lift at all (2-5 November, before the tiny/small-face geometry fixes #135 and #139); 3 after a lift (results 117-119, a 472x708 photograph, 4 November: **accepted today**, 944x1416); 1 after a lift and a face restoration (the 293 px 29.jpg, 5 November) |
| `highlight_clipping_increased` | 1 | result 174, 25.jpg (a man in shadow in front of a window; the lift was accepted) |

So the brief's symptom is real, but it is two different checks, and for the photograph the owner was testing when it was written (25.jpg) the geometry was **not** the blocker.

## 2. Case 1: 25.jpg, refused for its highlights (not the geometry)

Standard (`high/natural`), the library's 414x277 photograph, 12 KB, face median 17 of 255, exposure `low_light_subject` (so the router gives the ordinary whole-frame lift; the shadow lift is only for a back-lit
subject the frame does not already call dark). Lift accepted (Zero-DCE, blend 1.0; face 18 to 90, frame mean 116 to 152). The enlargement candidate (Real-ESRGAN at `REALESRGAN_OUTPUT_BLEND` 0.35):

* geometry and identity **passed**: landmark shift 0.0172 (limit 0.06), similarity 0.8095 (limit 0.80);
* highlights **failed**: clipped pixels (luminance 250 and above) 1.27 percent in the lifted photograph, 9.30 percent after the enlargement, +8.03 points against a limit of 3.0.

The model adds only +0.9 levels on average to the bright part, but the lift had moved the window to the edge of the range (sky 238 to 242 on average, 0.4 to 3.0 percent of it at 250 or more), so a +1 level
crosses the line for hundreds of pixels. The same enlargement on the original, with no lift, is accepted (+0.44 points). The detail layer is refused for the same reason (+14.4 points) and, in addition, for
identity (0.77 and 0.79): that one this change does not address.

**Change (`ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD`, default on).** An enlargement refused for `highlight_clipping_increased` **alone** is judged once more with the model's brightening of already bright pixels taken back
(`operations.keep_highlights`), as a second candidate that must pass every gate. Result: clipping 1.27 to 1.40 percent (+0.13), identity 0.8102, landmark shift 0.0174, **accepted, 828x554**; the face restoration is
then proposed too and accepted (similarity 0.7506 against the restoration limit 0.65); final operations: lift, enlargement, face restoration (Pro: plus the deblur). The sky of the result is the lift's own (242.1 mean,
3.0 percent at 250 or more, the same as the lifted photograph enlarged plainly).

## 3. Case 2: 29.jpg, refused for the face geometry after a lift

The owner's upload of 14:45 UTC (result 179) and the library's id 186, a 293x293 photograph of a woman against a bright window: the lift and the face restoration were accepted, then the enlargement was refused
for `face_geometry_changed`: landmark shift 0.0728 against the **dark original's** landmarks (limit 0.06), and 0.0455 beyond the landmarks the detector finds on a plain enlargement of the **dark original**
(limit 0.035). The original face is almost black, so both references are guesses. Judged against the lit image the step was given: 0.042.

An experiment (the geometry reference of the enlargement only, in a scratch copy) showed that moving the baseline for one step moves the refusal to the next: the enlargement was accepted and the detail layer,
now on the enlarged grid, read 0.086 and 0.056 against the dark original and was refused. So the second opinion covers every later step.

**Change (`ADAPTIVE_BASELINE_AFTER_LIGHTING`, default on).** Once a lift is accepted, a step the share limit and the plain-enlargement rule refused gets a second opinion against the lit image it was given:
its landmarks within its own limit of that image's (0.06 for an enlargement, 0.04 otherwise), or, for an enlargement, within 0.035 of a plain enlargement of that image. Never for a face restoration or polish; a face no
longer found is still a loss; the identity embedding is judged as before. It is only consulted after a check against the original has refused the step, so it can add acceptances, never remove one.

## 4. What was not changed

* **The identity baseline.** The brief asks for it too. The lifted face of 25.jpg is at 0.8035 against 0.80, a thin margin, but no measured enlargement was lost to it (0.8095, 0.9883) and moving it would loosen the
  one guarantee the engine makes about who is in the photograph. Not applied.
* **SSIM and the other data checks.** Already against the step's input (section 1).
* **The highlights limit (3.0 points).** Raising it would accept real blown skies; the retry keeps it.

## 5. Regression

Photographs whose enlargement is accepted today must come out identical. Each was run twice with real models, the new setting on and off (the engine's 180 s time budget lifted, because two runs that differ in
speed otherwise differ in the steps they reach):

| photograph | request | highlight guard on / off | baseline after lighting on / off |
|---|---|---|---|
| 167 (472x708) | auto/natural; Pro | identical | Pro: identical (lift, enlargement, detail) |
| 205 (448x296) | auto/natural | identical | |
| 204 (480x320) | Pro | identical | |
| 208 (260x194, enlarged by the upload) | Pro | identical (9 candidates both) | |
| 157 (448x296) | Pro | identical | **448x296 to 896x592** (lift, denoise, enlargement, detail) |
| 186 (293x293) | auto/natural | identical | **293x293 to 586x586** (lift, face restoration, enlargement, detail) |

Where nothing was refused, nothing changed: the retry and the second opinion are not even evaluated. The unit tests (`enhancement/test_adaptive_upscale_highlight_guard.py`, `enhancement/test_adaptive_lifted_baseline.py`)
fix the rules; every new rule was broken once in a copy of the tree and a test failed (mutation run, below).

## 6. A second brief, the same day: force the lift, relax the gates, make it 4x

A second brief asked for three things; each was checked against the owner's whole database (148 results) and the real models before anything was changed.

**Force the lift for a dark face (face luminance under 40) and the NAFNet deblur for a blurred one; tell the validator to expect a large brightness change.** The router already lifts a face the analyzer calls
dark (`face_median_dark` 75, wider than 40, when the frame is dark too; a back-lit subject is the shadow lift, for the `ai_max` style or the person's own "Dark photo"), and the validator has a lift-specific branch
for exactly that (brightness-normalised SSIM, chromaticity in linear light, no overshoot gate, a noise allowance). The lift stage was refused 5 times in 39 attempts, never for its SSIM. The deblur is already proposed
for a blurred face or motion blur; of 36 attempts 28 were refused, 26 of them for `face_not_clearer_or_over_sharpened` (the model ran and the face was not clearer): forcing it would ship what the engine's own
measure calls no better. A defocus is left alone on purpose (the motion model cannot recover it, `router.py`). A back-lit face with a bright frame is lifted for Auto/Standard only when the person says "Dark photo"
(`lighting`): lifting it unasked damaged 6 of 11 library photographs before the compression gate existed.

**Relax the geometry limit to 0.12 and the SSIM limit to 0.50 for a poor input.** Why each stage was refused (all 148 results): enlargement 15 geometry and 1 highlights; detail layer 14 `no_added_detail_or_invented_detail`,
7 highlights, 6 halo, 5 geometry, 1 identity; lift 3 `subject_not_clearer_or_too_bright`, 2 geometry, 1 identity, 1 compression blocks, 1 enlarged source; face restoration 1 geometry, 1 identity, 1 detail invented.
The lift's SSIM limit (0.70) has never refused a lift, and the real lifts sit far above it (structure SSIM 0.90-0.97 on 25.jpg, 29.jpg, 1.jpg, 9.jpg and the Jodhpur photograph, measured with the engine's own `structure_ssim`), so lowering it changes nothing and
can only let a distorted lift through. A geometry limit of 0.12 for every step would also open the generative face restoration, whose geometry gate stays strict (CodeFormer invented sunglasses on library 173); the second
opinion of section 3 does the same job for the steps that need it, against the right reference instead of a looser limit. Re-run with today's code (real models), the photographs once refused for geometry,
identity or highlights (one batch of 11 photograph/request pairs: Jodhpur x3, dark-054-040, 1.jpg x2, a 1251x1349 photograph x2, 10.JPG, 4.jpg, 9.jpg Pro; and 472x708, 29.jpg and 25.jpg before it): **every enlargement that
was refused is now accepted**. What is still refused is legitimate: the face restoration of 4.jpg (geometry, strict), the lift of 9.jpg Pro and of Jodhpur Pro (their shadows show the compression blocks; 9.jpg is a 13 KB
JPEG and with every gate off its lift reads face median 3 to 23 with heavy blocks), and the detail layer of the 1251x1349 photograph (halo, highlights).

**Make it 4x HD / "HD restoration is not available".** The banner is the frontend's text for any result whose `hd_unavailable_reason` is set; the backend sets it whenever a restoration the photograph needed (deblur, face
restoration, enlargement) was not completed ("Restoration was needed but not completed: upscale (the result did not pass the safety checks: highlight_clipping_increased)", `restoration.py`). The models are available (the live
`.env` has `HD_RESTORATION_ENABLED=True`; 37 of 53 enlargements were accepted): the banner's wording is wrong, not the server. The engine enlarges 2x by design (Real-ESRGAN is a x4 network, its output is resized to 2x and
blended with the plain enlargement at `REALESRGAN_OUTPUT_BLEND` 0.35). **`ADAPTIVE_UPSCALE_SCALE` (default 2) now lets the engine ask for 4x** (4 falls back to 2 under `ADAPTIVE_MAX_OUTPUT_EDGE`). Measured with real
models, all with every gate passing: 25.jpg 414x277 to 1656x1108 (35 s), 29.jpg 293 to 1172, 1.jpg Pro 448x296 to 1792x1184, 2.jpg 448x296 to 1792x1184, Jodhpur 480x270 to 1920x1080 (32 s), 10.JPG 375x500 to 1500x2000.
**4x is not better on every photograph:** on 25.jpg the face restoration that the 2x run accepted is refused at 4x (`face_geometry_changed`, 0.042 against the strict 0.04: the detector places the landmarks of a bigger
enlargement further away), so the 4x face is the lifted face, blocky, where the 2x face is restored; the identity margin of the dark photographs is thin (0.8085 against 0.80), and the pixels are the plain enlargement's plus
35 percent of the model's, so 4x is more pixels, not more detail per pixel. The default stays 2.

## 7. A third brief: update the baseline per stage, force the deblur for Pro with a second opinion, log every skip

* **The baseline per stage.** Already how the engine works, apart from the two checks that read the upload: `service._propose` hands every step `current.image` and its analysis (the last ACCEPTED candidate), so
  data consistency, SSIM, colour, halo, noise and highlights compare a step with its own input; `validator.lifted_input` (section 3) gives the geometry a second opinion against that input after a lift;
  `RestoredBaseline` does the same for the identity after a face restoration. The identity embedding stays against the upload (the identity guard is unchanged).
* **Forcing NAFNet (Pro) and a second opinion against the upscaled original.** The analyzer's blur reading is not a Laplacian variance (gradient energy that survives an extra blur, a direction ratio for the type,
  and the main face's own detail), and the router already sends NAFNet for a blurred face and for motion blur. Measured with the real model on 59 distinct library photographs (reduced to 1000 px, the validator's
  real gates, no identity model): the 11 the router sends, **1 accepted, 10 refused**; forced on the 48 it skips, **5 accepted, 43 refused**, and the 5 are edge gains of 1.10-1.21 on photographs read "no blur" or a
  mild defocus (a light sharpening of photographs that were not blurred). What refuses it is the face (`face_detail_gain` 1.0 against 1.25: the model did not clear the face); the geometry, where "compare with the
  upscaled original" already exists (`_shift_beyond_plain_enlargement`, section 3), never refused a deblur (0 of 28 refusals). A second opinion could only accept deblurs that did not help, so it was not built.
* **Logging.** Every gate of the validator keeps the reading it compared and the limit it was held to (`metric` / `limit`; `reason` for a failed one). `validator.refusal_lines(verdict)` words them and the engine logs
  one WARNING line per failed gate of a refused candidate, per model that could not run and per operation left out, with the photograph's label (the task sets it: `adaptive/trace.py`):
  `SKIPPED realesrgan for Photo ID 225 (result 183). Reason: face_geometry failed (face_geometry_changed). Metric: landmark_shift=0.0734, Threshold: 0.06; beyond a plain enlargement of the original 0.0356 against 0.035`.
  A refusal that took a hair's breadth (0.0356 against 0.035) is now readable from the log instead of from a re-run.

## 8. Not verified

* Through the API and the UI on the live server (the engine was called directly; the live server runs `main` until this is merged).
* PostgreSQL (CI runs it; these tests are database-free).
* The identity of a face restored on a near-black original is "similar to the upload" (an embedding is evidence, not proof); the restored face of 25.jpg is a model's, not the photograph's.
* The blocks of a 12 KB JPEG remain visible in dark areas after any lift (the plain gamma curve shows the same, `docs/evaluation/2026-11-05-backlit-subject-lift.md`).
* A second photograph with the highlights case: only 25.jpg is in the database.
