# A back-lit subject: detected always, lifted by AI Enhance only when switched on (2026-11-05 / 06)

**The report.** A person against a bright sky or a sunset stays a dark silhouette after enhancement: the engine measures the frame, sees a bright one and leaves the lighting alone. The brief asked for subject-aware detection (the face, or a centre region; subject 40 percent darker than the frame; a histogram with a dark and a bright peak), a forced Zero-DCE lift when the photograph is back-lit or the subject is under 50, a setting for the 50, a local lift that does not overexpose the background, tests with back-lit images, and nothing CUDA-only.

## Why nothing happened (on purpose)

`analyzer.py` already named the case: status `backlit_subject`, "a dark face in a bright frame: not a global-lift problem, left alone". A lift of the whole frame is wrong for it (it changes the sky; `docs/evaluation/2026-11-04-lowkey-floor.md` records a back-lit portrait that was once taken for a low-key one). The engine also did not *know* four of the 11 back-lit photographs of the library were back-lit: their frame status is `normal` (ids 130, 171, 200) or `overexposed` (id 173).

## What was built

1. **Detection, always on** (`ImageQualityAnalyzer._subject_exposure`, reported as `quality_report.subject_exposure`). The MAIN face is back-lit when its tonal range is crushed (`visibility` poor: the same test the analyzer uses everywhere, so a well-lit dark skin with a wide range is not caught), its median luminance is under `ADAPTIVE_BACKLIT_SUBJECT_BRIGHTNESS_MAX` (60) **and** under `ADAPTIVE_BACKLIT_SUBJECT_MAX_RATIO` (0.40) of the frame's mean, and the frame's median is at least `ADAPTIVE_BACKLIT_MIN_SCENE_MEDIAN` (70; a dark frame is the low-light rules' business). No face, no judgement.
2. **A shadow lift** (`operations.shadow_weight`, `run_low_light(..., local="shadows")`, one router branch): the network's output is blended in only where the original is clearly darker than the FRAME'S OWN median luminance (full below 0.5 of it, gone at 0.9, smoothstep, lightly blurred), so a sky of 0.4 and a window of 0.7 are both "background" in their own photograph. Proposed for **AI Enhance (`ai_max`) only**, at the usual two blends; every gate of the validator judges it as before.
3. **A gate for what the lift makes visible** (`validator`, `compression_blocks`, shadow lifts only): refused when the analyzer reads the LIFTED photograph as heavily JPEG-compressed (section "What goes wrong"). Reason: `lift_shows_compression_blocks`. **The refusal covers the whole step** (`service._refuse_weaker_shadow_lifts`): the weaker blends of the photograph are refused with it (a gentler lift only shows the same blocks fainter) and no fallback engine is tried (the classical lift that comes after Zero-DCE shows them too). In the first real runs a gentler blend passed the gate on id 200 and the classical engine on id 182, and both results were still damaged.
4. **`ADAPTIVE_BACKLIT_SUBJECT_LIFT`, OFF by default.** Detection (1) is always reported; (2) and (3) act only when the switch is on and the style is AI Enhance.

## Detection: the brief's rules against the 62 library photographs (45 with a face)

| rule | flagged | of the 11 back-lit | others flagged |
|---|---|---|---|
| A: subject mean under 0.6 of the frame mean | 19 | 11 | 57, 69, 93 (white studio background, frame mean 239), 96, 158, 164, 170, 186 |
| B: histogram, dark third and bright third | 8 | **3** | 20, 22, 41, 73 (well-exposed studio portraits on white), 164 |
| C: subject mean under 50 | 19 | 11 | 15, 58, 63, 69, 131, 158, 170, 186 |
| A or B or C (the brief's "force Zero-DCE") | **27 of 62** | 11 | **16 photographs that are not back-lit** |
| **shipped rule** (face range crushed, median < 60 and < 0.40 of the frame mean, frame median >= 70) | **11** | **11** | **none** |

The 11 (ids 130, 157, 160, 169, 171, 173, 176, 177, 182, 188, 200): face median 8 to 50, ratio 0.09 to 0.36. The histogram rule is not used: it misses 8 of the 11 and flags the studio portraits. The limits are calibrated on this library (a small sample, see "Not verified"); 60 and not 50 because id 177's face median is exactly 50.

## What a forced lift does to the frame (real Zero-DCE through the whole engine, `auto` / `natural` with the router forced; frame mean, before -> after)

| id | the brief's whole-frame lift | shadow lift, absolute thresholds | shadow lift relative to the frame's median (shipped) |
|---|---|---|---|
| 157 | 126 -> 162 | 126 -> 151 | 126 -> 149 |
| 160 | 122 -> 152 | 122 -> 152 | 122 -> 151 |
| 169 | 140 -> 167 | 140 -> 155 | 140 -> 151 |
| 171 | 92 -> 122 | 92 -> 111 | 92 -> 106 |
| 173 | 136 -> 172 | 136 -> 159 | 136 -> 155 |
| 176 | 137 -> 160 | 137 -> 152 | 137 -> 157 |
| 130 | 95 -> 150 | 95 -> 132 | 95 -> 118 |
| 200 | 111 -> 147 | 111 -> 130 | 111 -> 120 |

The whole-frame lift raises the frame mean the most on all eight (by 23 to 55 grey levels over all 11 photographs; the sky is lifted by construction; the table shows the eight that all three variants were run on), and the relative shadow lift the least on seven (id 176 is the exception). The three variants also differ in the later steps they allow, so the means are a comparison of size, not of quality.

## Whole engine, real models, AI Enhance (`high` / `ai_max`), the lift off and on in one process

| id | photograph | frame mean: before / AI Enhance, lift off / lift on | lift (real models) | result (my reading of the face crops) |
|---|---|---|---|---|
| 182 | 14.jpg | 117.4 / 117.5 / 117.5 | refused | **lift refused** (compression blocks): left as before |
| 200 | 33.jpg | 110.9 / 109.7 / 109.7 | refused | **lift refused** (compression blocks): left as before |
| 130 | jodhpur-india-june-28- | 95.0 / 95.8 / 95.8 | refused | **lift refused** (compression blocks): left as before |
| 171 | 4.jpg | 92.2 / 92.4 / 92.4 | refused | **lift refused** (compression blocks): left as before |
| 157 | 1.jpg | 125.7 / 125.9 / 148.7 | accepted | lit; window and trees untouched; no artifact |
| 160 | 2.jpg | 121.6 / 123.1 / 149.5 | accepted | lit, but a pink, smoothed face; the face restoration the earlier engine accepted is no longer accepted |
| 169 | 10.JPG | 140.1 / 140.2 / 151.1 | accepted | lit (modestly); a thin dark outline along the hair |
| 173 | 12.jpg | 136.2 / 136.7 / 154.7 | accepted | lit; a dark outline around the cap and the hair |
| 176 | facecloud-12.jpg | 136.6 / 137.6 / 156.6 | accepted | lit; a dark outline around the cap and the hair |
| 177 | 10_enhanced_low.jpg | 140.0 / 140.4 / 151.1 | accepted | lit (modestly); a thin dark outline along the hair |
| 188 | 14_enhanced_medium.jpg | 117.8 / 118.3 / 153.2 | accepted | lit, **blocks visible in the face** (a 4x enlargement of 182: not recognised as compressed) |
| 22 | 27.jpg | 136.3 / 136.0 / 136.0 | not proposed | control, no back-lit subject: pixel-identical |
| 93 | mojtaba-mosayebzadeh-W | 238.6 / 238.6 / 238.6 | not proposed | control, no back-lit subject: pixel-identical |

Pixels differ from the earlier engine for **7 photographs** (ids 157, 160, 169, 173, 176, 177, 188); the lift was refused by the gate for ids 182, 200, 130, 171 (their results are identical to the earlier engine's); both controls are identical. Auto / Natural on ids 157, 171: pixel-identical with the switch on. Judged by eye on face crops: **no artifact** on 1 (157), **a dark outline** along the subject on 4 (169, 173, 176, 177), **damaged** on 2 (160, 188); 4 refused.

## What goes wrong: a lift raises the JPEG blocks of a small, heavily compressed photograph

Without the gate, the lift left **6 of the 11 visibly damaged** (my reading of face crops, original | earlier engine | new engine; no metric):

* **Blocky faces** on ids 182, 188, 200 and 130 (a pixelated head and shirt, in 130 with a white outline): the shadows of a compressed JPEG hold few distinct levels, a lift raises them several times and the blocks of the compression show. The network's lift ALONE (blend 1.0, native size, no detail model, no enlargement) already does it on 182 and 188 (188, `14_enhanced_medium.jpg`, is the 4x enlargement of 182, `14.jpg`); gentler blends (0.65 / 0.4) and no detail layer do not remove them, and the steps that follow keep them.
* **Id 171**: lighting good, but a light outline along the hair and faint blocks on the cheek.
* **Id 160**: a pink, smoothed face, and the face restoration that the earlier engine accepted is not accepted after the lift; a colour problem, not blocks.
* Not damaged: id 157 (no artifact) and ids 169, 173, 176, 177, with a dark outline where the subject meets the sky (clearer on 173 and 176): the weight follows each pixel's own brightness, so the ramp between the dark subject and the bright sky stays unlifted.

No simple measure of the lifted face separates the two groups: the grid-aligned (8 px) step excess of the face after the lift is 5.1 grey levels for 182 and 4.2 for 171, against 3.7 for the clean 157 and 2.7 for 173; the colour of the lifted face (Lab chroma and hue) does not separate the pink id 160 from the others either. What does separate the blocky ones is **the analyzer's own compression reading of the WHOLE lifted photograph** (the signal the engine already uses to refuse sharpening on a compressed photograph): `heavy` for 182, 200, 130 and 171, `none` for 157, 169, 173, 176, 177, and also for 160 and 188 (a 4x enlargement: its blocks are 32 px wide, there is no 8 px grid for the measure to find).

**It took three steps to make the refusal hold on the real photographs** (the first two passed their tests and still left a damaged result):

1. The gate alone refused the strong Zero-DCE blend of 130, 171, 182 and 200. On 200 a gentler blend (0.65) passed it (frame mean 111 -> 117) and the result was still damaged (a distorted face): a fainter lift shows the same blocks fainter. The weaker blends are refused when a stronger one shows them.
2. With that, 200 was refused. On 182 Zero-DCE was refused and the engine fell back to the classical lift (`models_run` `opencv_exposure`, `zero_dce`), whose full-strength lift read as not compressed and was accepted (frame mean 117 -> 132), with blocks in the cheek and the dress.
3. So the refusal covers the whole step: once a shadow lift shows the blocks, no other engine is asked (`service._refuse_weaker_shadow_lifts`). Result above: 182, 200, 130 and 171 are pixel-identical to the earlier engine.

The gate and the rule therefore catch **4 of the 6 damaged photographs and none of the 5 that were not damaged**; ids 188 and 160 are not caught.

## Tests

`enhancement/test_adaptive_backlit_subject.py`, 39 tests on synthetic scenes (a bluish sky with a dark head and body; no real photograph is in the repository): the rule (crushed subject, a silhouetted head too small for a face, a well-exposed portrait, a studio portrait on white whose histogram has two peaks, a dark skin with a wide tonal range, a subject that is dark but not under the limit, a dark frame, no face, the limits as settings, the report), the weights (full on the subject, zero on the sky, relative to the frame, a smaller blend lifts less, the whole-frame lift unchanged), the router (AI Enhance proposes local variants, Natural and Dynamic do not, the switch, a dark scene keeps the whole-frame lift), the engine (the subject is lifted and the sky stays within 2 grey levels; Natural, the off switch and a well-exposed portrait are pixel-identical to the input), the gate (a JPEG of quality 40 is refused for that reason alone, one of quality 80 is lifted, the gate reads the LIFTED photograph and not the original, a whole-frame lift is not judged on it) and the rule for the whole step (the weaker blends are refused when the stronger one shows the blocks, no fallback engine is asked, a fallback engine is still asked when the first fails for another reason; on the engine and on the verdicts alone).

Mutation check: 34 mutations (17 of the detection, router, operations and profile; 8 of the gate and its wiring; 9 of the whole-step rule), all caught. Three survived the first version of their tests (the gate reading the original instead of the lifted photograph, the rule without its reason, and the rule refusing a lift that has no such gate) and have a test each now.

## Not verified

* **Eleven back-lit photographs of one library.** The limits (60, 0.40, 70) rest on them, and so does the "4 of 6" of the gate. The verdicts on the results are my reading of face crops, not a metric.
* **Fairness across skin tones is argued, not measured.** The rule requires a crushed tonal range, so a well-lit dark face is not back-lit (a synthetic test); no diverse set of real photographs was run.
* **Silhouettes without a real face** are judged on a guessed box (ids 130 and 200: faces of 21 x 28 and 31 x 37 px).
* **Two damaged results are not caught**: id 188 (a 4x enlargement of 182: its blocks have no 8 px grid, so the analyzer reads it as not compressed; photographs resized after compression are the common case in the wild) and id 160 (a pink, smoothed face: a colour problem, and the face colour after the lift, measured in Lab, does not separate it from the clean ones). The switch stays off until a better guard exists.
* **Later steps change after a lift.** They are judged against the lifted photograph: face restoration was lost on id 160, the enlargement on id 176.
* **The outline** along the subject's edge (ids 169, 173, 176, 177) comes from the weight following each pixel's own brightness: the ramp between a dark subject and a bright sky stays unlifted. Dilating the weight was tried: the dark outline became softer and a light halo appeared around the cap, so the weight was kept.
* **Only AI Enhance** is affected; the controls (ids 22 and 93, and Auto on ids 157 and 171) were pixel-identical with the switch on.
* The frame's median must stay under the validator's 175 ceiling after the lift, as for any lift.
* Real models ran on the M1 (MPS); nothing here is CUDA-specific.

## Using it

`ADAPTIVE_BACKLIT_SUBJECT_LIFT=True` in `.env`, restart the worker (Celery does not reload code), choose AI Enhance. The result's `lighting` block then reads `detected: light`, `brightened: true`. Id 157 is the clean example of the library; 169 and 177 are lifted modestly, with a thin outline.

## What differs from the brief

| brief | here | why |
|---|---|---|
| `detect_subject_underexposure()` in `service.py` | `ImageQualityAnalyzer._subject_exposure`, reported in `quality_report` | the face and its statistics are already analysed there; a second function would measure twice |
| centre region when there is no face | no judgement | without a face there is no subject to protect, and the centre of a photograph is not a person |
| `is_backlit_scene()`, histogram bimodality | not used | misses 8 of 11, flags the white-background studio portraits |
| force Zero-DCE when back-lit or subject < 50 | a shadow-only lift, AI Enhance only, switch off by default | a forced whole-frame lift changes the whole frame, sky included (+23 to +55 grey levels on the frame mean) and "subject < 50" alone flags 8 photographs that are not back-lit |
| `BACKLIT_SUBJECT_BRIGHTNESS_THRESHOLD=50` | `ADAPTIVE_BACKLIT_SUBJECT_BRIGHTNESS_MAX=60` and two more limits | the prefix of the other settings; id 177's face median is 50 |

## Addendum 2026-11-06: what the live pipeline showed, and what this evaluation missed

Every row above fed the engine the photograph's **original file**. The live pipeline does not always: when preprocessing enlarges a very small photograph (the small-image enlargement, on by default;
`enhancement/services.py` `_read_enhancement_source`, strategy `standardized_enlarged`), the engine sees the **enlarged copy**. On 2026-11-06 the owner enhanced `14.jpg` with Pro through the app
(results 162 and 164). It is library id 182 (260 x 194, 8 KB), the photograph this note calls "lift refused". The source the engine saw was **1040 x 776**, its compression reading was `none`
(blockiness 1.19 / 1.10, the same as the clean photographs 157, 169 and 177), the shadow lift was accepted, and the face came back with 8 x 8 blocks across the eyes and cheeks: a 4x enlargement of
a blocky JPEG has blocks 32 px wide, and the reading looks for 8 px.

A reading at 1/2 and 1/4 scale of the enlarged copy does not separate it either (1.16 / 1.12 and 1.22 / 1.16, against 1.14 / 1.09 and 1.20 / 1.13 for the clean photograph 157), so a multi-scale
reading was measured and **not** used. What decides instead is the one thing that is known: the pipeline enlarged the photograph. `process_enhancement_result` tells the engine (`source_enlarged`, only
when it is true) and the validator refuses a **shadow lift** of such a source (gate `compression_blocks`, reason `lift_on_enlarged_source`): a photograph small enough to be enlarged is small enough to be a
compressed one. Whole-frame lifts, every other operation and every photograph the pipeline did not enlarge are unchanged (`enhancement/test_adaptive_backlit_subject.py`).

Still not covered: a photograph the person enlarged themselves before uploading (id 188, 1040 x 776 with 32 px blocks, which the pipeline does not enlarge again), and the pink face of id 160.
Of the eleven library photographs only id 182 is enlarged by the pipeline.
