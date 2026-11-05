# A face of 70-130 px no longer vetoes an enlargement because of where the detector puts its landmarks (2026-11-05)

**The report.** Real-size faces (114 and 127 px wide) still made the engine refuse the enlargement of their photograph for `face_geometry_changed` after the tiny-face fix (2026-11-04, 21 and 43 px faces). The stored refusals: two photographs, six results (`1.jpg`, 448 x 296, face 114 px, quality `poor`, results 100, 102, 124; `facecloud-images_1.jpg`, 472 x 708, face 127 px, quality `acceptable`, results 117, 118, 119).

## What was measured (real models, the whole engine, the live `.env`)

The gate measures the mean displacement of the five landmarks, as a share of the face width, between the ORIGINAL photograph's landmarks (brought to the candidate's grid) and the landmarks the detector finds on the candidate. Limits: 0.06 for an enlargement, 0.04 for everything else.

| | 114 px face (448 x 296) | 127 px face (472 x 708) |
|---|---|---|
| Lanczos x2, **no model** | **0.0760** | 0.0651 |
| bicubic x2, **no model** | **0.0779** | 0.0636 |
| Real-ESRGAN x2 (the pipeline's candidate) | 0.0801 | 0.0553 |
| engine, auto / natural | enlargement refused (0.0801) | enlargement **accepted** (0.0553); then the final layer refused at 0.050 and 0.052 against 0.04 |
| engine, high / ai_max (after a low-light lift) | not run | enlargement refused at **0.0606** |

* **The model is not what moved the face.** On the 114 px face a model-free enlargement already measures 0.078 (per-landmark displacements 3 to 12 px of the original, almost identical for Lanczos, bicubic and Real-ESRGAN, with the whole constellation offset by about 8 px). The detector places the landmarks of any enlargement of a small, soft face somewhere else; that is what the 0.06 limit was refusing.
* **It is not only these two photographs.** On 93 public LFW faces (68 to 134 px wide, median 92) a plain bicubic x2 moves the landmarks by **0.040** (median), **0.066** (90th percentile) and 0.093 (99th). **14 of 93 Real-ESRGAN candidates (15 %) are refused today and for all 14 the plain enlargement alone is refused too**; by face width: under 90 px 28 %, 90 to 140 px 8 %. On the library's own photographs with one face: 9 of 19 refused today, all 9 also refused with no model.
* **A step after an accepted enlargement inherits that placement.** Every later step is judged on the enlarged grid against the original's landmarks, so on the 127 px photograph the final layer was refused at 0.050 and 0.052 (limit 0.04) although it moved nothing (the enlargement itself measured 0.0553).

## Why not simply a tolerance that grows as the face gets smaller

A pixel floor scaled by the face width (the earlier `ADAPTIVE_LANDMARK_PIXEL_FLOOR` is 2 px) would have to be about 10 px to accept the 114 px photograph (0.080 = 9.1 px). Measured on the same 112 enlargements and 896 controls (the real candidate with a known distortion put into it: the face translated, or the mouth pushed down; "true move" is the mean landmark displacement as a share of the face width):

| rule | good enlargements refused | distortions caught, true move 0.015-0.04 | 0.035-0.06 | 0.055-0.08 | 0.08 and more |
|---|---|---|---|---|---|
| today (0.06 against the original) | 23 of 112 (20.5 %) | 31 % | 47 % | 78 % | 100 % |
| size-scaled floor, 8 px (cap 0.09) | 8 of 112 (7.1 %) | 6 % | 14 % | 39 % | 86 % |
| size-scaled floor, 10 px (cap 0.09) | 4 of 112 (3.6 %) | 4 % | 10 % | 30 % | 85 % |
| **relax-only, 0.035 (implemented)** | **0 of 112** | 11 % | 34 % | 76 % | **100 %** |
| excess-only, 0.035 (not implemented) | 1 of 112 (0.9 %) | 21 % | 69 % | 97 % | 100 % |

A tolerance proportional to the face size still refuses good enlargements and, because it widens the limit for everything that small, misses far more real distortions. Today's rule is also weak the other way: it catches only about half of true 3.5 to 6 % distortions, because the noise it measures is as large as the signal.

## The change

`FinalImageQualityValidator._shift_beyond_plain_enlargement` (`enhancement/adaptive/validator.py`). When the share limit refuses a candidate of a different size from the original, the validator compares the candidate's landmarks with the landmarks the detector finds on **a plain bicubic enlargement of the original to the same size** (what the model moved, with the detector's own placement taken out). The candidate is accepted when that is at most `ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX` (0.035 of the face width) and the verdict records it (`landmark_shift_beyond_plain_enlargement`).

* **On the same faces the model moves the landmarks by 0.005 (median), 0.014 (90th percentile), 0.034 (99th percentile), 0.043 at most.** The largest value among the candidates refused today is 0.0296 (the 114 px photograph); 0.035 leaves it a margin of 0.005 and sits at the 99th percentile of the good candidates.
* **Never stricter than before.** It is tried only for a candidate the share limit has already refused, so a candidate that passed still passes; `0` switches it off and gives the earlier behaviour exactly (the library comparison below uses that).
* **Not for a generative face restoration** (always strict, as before) **and not for polish** (its own 0.02 gate and structure check). Applied to the other non-generative operations when the candidate is not the original's size, which includes every step after an accepted enlargement.
* **A candidate of the original's size is not compared**: the plain enlargement would be the original itself and say nothing new. Refusals of a step on a photograph that was never enlarged are unchanged.
* **A face that is no longer found is still a loss**, and **the identity check is independent**: a candidate that passes the geometry gate this way is still refused when the identity model says it is another person (tested).
* **Cost.** One detector pass on a bicubic enlargement, only after a refusal, once per size per photograph (cached).

## Measured after the change (real models, the whole engine, default setting)

| | before | after |
|---|---|---|
| 114 px photograph, auto / natural | enlargement refused; result 448 x 296, `detail_restoration` only | **896 x 592**, `super_resolution` + `detail_restoration` (enlargement beyond the plain enlargement: 0.0033; final layer 0.0105 and 0.008) |
| 127 px photograph, auto / natural | enlargement accepted, final layer refused | `super_resolution` + `detail_restoration` (final layer beyond the plain enlargement: 0.022 and 0.018) |
| 127 px photograph, high / ai_max | enlargement refused at 0.0606; result 472 x 708 | **944 x 1416**, `low_light_enhancement` + `super_resolution` + `detail_restoration` (0.0154, then 0.014 and 0.013); the two low-light candidates that changed identity (similarity 0.71 and 0.78) are still refused |

Looked at side by side with a plain bicubic enlargement at the same size, both faces keep their geometry (hairline, ears, eyes, mouth, shirt pattern on the first; profile, lashes, lips, ornaments on the second) and are only sharper.

## The whole library, before and after (real models, natural / auto)

The library's 46 distinct originals, those with a face and at most 2 megapixels (23 photographs), through the whole engine twice with the new code: `ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX` = 0 (exactly the earlier behaviour) and the default. **20 outputs are identical to the last pixel, 3 differ, and no photograph lost an operation.** The three:

| photograph | earlier behaviour | now |
|---|---|---|
| 448 x 296, face 114 px | enlargement refused; `detail_restoration` only, 448 x 296 | `super_resolution` + `detail_restoration`, **896 x 592** |
| 472 x 708, face 127 px | `super_resolution`; final layer refused (inherited placement) | `super_resolution` + `detail_restoration` |
| 375 x 500, face 89 px | `super_resolution` (750 x 1000); final layer refused (inherited placement) | `super_resolution` + `detail_restoration` |

## Not verified

* The controls are synthetic (a translated face, a mouth pushed down). They are a way to compare rules, not a model of what Real-ESRGAN does; it is not a face generator and the enlargement is blended with a bicubic one (0.35).
* **Relax-only catches fewer small distortions than today's rule** (true move 1.5 to 4 %: 11 % against 31 %; 3.5 to 6 %: 34 % against 47 %), and the same as today from 5.5 % up (76 % against 78 %, and 100 % from 8 %). Today's catch rate there is itself noise-driven. The "excess-only" row (the candidate refused whenever the model moved more than 0.035 beyond the plain enlargement) is the sharper gate (97 % of 5.5 to 8 %) at the price of new refusals (1 of 112 good enlargements); it is not implemented, because a new refusal is a behaviour change.
* The 114 px photograph's face is too soft for the identity model (`embedding: unavailable`), so for it the geometry check is the only face check on the enlargement; that is the policy for every non-generative operation.
* Calibrated on YuNet. Another detector needs the same measurement (`ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX`).
* Only enlargements were calibrated; the other non-generative operations after an enlargement use the same rule (the same detector effect: measured on the 127 px photograph's final layer).
* Refusals of a step on a photograph that was never enlarged are not touched: the final layer on a 234 px face (results 81 and 82, which also fail `no_added_detail_or_invented_detail`).
* The library comparison covers 23 photographs with a face (the others have no face or are over 2 megapixels, which the enlargement does not reach); it shows no regression, not a proof for every photograph.
* The frontend still replaces the engine's reason with a fixed sentence for a photograph that was not enlarged (see 2026-11-04-tiny-face-geometry.md).
