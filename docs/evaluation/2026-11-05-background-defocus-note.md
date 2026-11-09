# A portrait with a soft background is no longer reported as "Defocus blur cannot be recovered reliably" (2026-11-05)

**The report.** The result card of two studio portraits (library photographs 20 and 22, `polish_only.jpg` and `polish_two.jpg` in the browser checks) read "Polished: balanced the contrast and sharpened lightly. **Not fully corrected: Defocus blur cannot be recovered reliably.**" although the person in them is sharp. The engine's analysis reads the frame as defocused (a plain, soft background), the deblur step is skipped for defocus (`router.py`: "the motion-deblurring model is not applied to it and cannot recover it"), and the summary then listed the defocus as a defect left behind and called the result `partially_enhanced`.

## What was measured (live configuration: real face detector, real models)

The library's 61 distinct photographs, analysed (nothing enhanced):

* **The engine calls 19 of them defocused at the frame level** (9 strongly). **In every one the main face is found; in 15 it is sharp** (`face_blur` = `none`, face detail 61 to 1092 against the 50 limit; sharp faces are about 119) **and covers 0.045 to 0.162 of the frame**. The four where it is not: a face covering 0.004 of the frame, one covering 0.020, a soft face (`low`) and a blurred face (`high`).
* **A face's size matters.** A far-away face says nothing about the frame: the rule therefore needs the main face to cover at least `ADAPTIVE_BACKGROUND_DEFOCUS_MIN_FACE_AREA` (0.03) of it. The observed faces leave a gap between 0.020 and 0.045; 0.03 sits in it.
* A plain Laplacian variance of the frame (what a pasted brief proposed to use as the "severe defocus" test, see `docs/evaluation/2026-11-05-dark-face-identity.md` for the earlier brief) is not an alternative: it flags 12 of the 61 photographs, 3 of which the engine finds not blurred at all and 4 of which are simply dark, and misses 10 of the 19 the engine calls defocused (variance 55 to 234). The portrait that the brief used as its example (448 x 296, variance 7453, frame sharp, a blurred face) is not a defocus case; it was fixed by the baseline change (`2026-11-05-baseline-after-face-restoration.md`).

## The change

`background_defocus_only(report)` in `enhancement/adaptive/service.py`: true when the ORIGINAL photograph's analysis has `defocus_blur`, its main face reads `none` for blur and covers at least the share above. Then, **in the verdict only**:

* `unresolved_defects` and the "Not fully corrected" clause leave `defocus_blur` out, so a result that is otherwise complete is `enhanced`, not `partially_enhanced`;
* for a photograph that was left unchanged, the sentence does not say "it has defocus blur, which cannot be recovered reliably" and falls back to the engine's usual "did not need enhancement";
* `adaptive.conditions_not_counted_as_defects` records what was left out and why, and the analysis (`quality_report.conditions`) and the deblur step's own skip record are unchanged, so nothing is hidden.

**No pixel changes, no gate changes.** `ADAPTIVE_BACKGROUND_DEFOCUS_NOT_A_DEFECT=False` gives the earlier wording and outcome exactly.

## Before and after, whole engine, real models

The 13 defocused photographs of at most 2.6 megapixels (the others are 7 to 12 megapixels), `auto` / `natural`, and `high` / `ai_max` for three of them: 16 runs, the rule off and on, same process.

| | runs |
|---|---|
| output identical to the last pixel | **16 of 16** |
| verdict changed: `partially_enhanced` -> `enhanced`, the "Not fully corrected: Defocus blur ..." clause gone | **13** (photographs 20, 22 (both requests), 23 (both), 41, 67, 73, 77, 80, 141, 167, 192) |
| verdict unchanged | 3: photograph 145 (its result no longer read as defocused, so it never carried the note) and photograph 188 twice (a blurred face: the rule does not apply and the note stays) |

Example (photograph 22, `auto` and `ai_max` alike): "Polished: balanced the contrast and sharpened lightly. Not fully corrected: Defocus blur cannot be recovered reliably." becomes "Polished: balanced the contrast and sharpened lightly."

## Tests

`enhancement/test_adaptive_background_defocus.py`, 16 tests: the rule on its own (sharp prominent face, no face, a soft or unmeasured face, a far-away face, the inclusive limit and its setting, a frame without defocus, the off switch) and the engine end to end with stand-in models (a portrait is not reported as partly corrected, a soft face and a photograph without a face and a tiny face still are, another unresolved defect is still reported next to the soft background, the ORIGINAL's face decides and not the result's, the off switch, an unchanged photograph with a soft background and with a soft face). Ten mutations of the new code were tried; all are caught (two survived the first draft of the tests: a non-defocus condition dropped as well, and the rule reading the result's analysis instead of the original's; a test for each was added).

## Not verified

* **It makes no photograph look better.** The two portraits are still only polished; this change stops a misleading sentence. Recovering real defocus blur needs a model trained for it, which is not installed (a defocus-trained model has not been evaluated; licence and weights are the owner's decision).
* **A small, uneven sample.** 15 photographs, four of them from the same studio session (1251 x 1349); the face-share limit of 0.03 rests on a gap in 19 photographs.
* **The deblur step's skip record still reads "the blur is defocus ... cannot recover it"** in the details of a result (it is a statement of what the engine did, not a verdict); only the sentence and the outcome changed.
* Photographs over 2.6 megapixels (24, 76, 93, 96, 100, 197) were analysed but not run through the whole engine. The rule fires for 24, 76 and 197 and not for 93, 96 and 100.
* `enhancement_outcome` becomes `enhanced` for these photographs; the frontend only treats `original_preserved` specially (`changedNothing`), so no screen changes except the sentence.
