# Detail restoration: why a WhatsApp portrait stayed blurry, and what makes it clearer (2026-11-03)

Status: measured with the real models on one Apple M1 (8 GB, MPS) on the owner's 1600 x 1382 WhatsApp portrait (id 136, upload "WhatsApp Image 2026-11-02 at 5.26.33 PM.jpeg"), a
professional 1600 x 955 portrait, and the same portrait darkened. Detail restoration is **on by default** (`ADAPTIVE_DETAIL_ENABLED`); 4x-UltraSharp is **off** unless `ULTRASHARP_ENABLED=True`.

## What the owner saw
The photograph came out "a little blurry" after Enhance. The engine had measured: frame blur "low" of type defocus, heavy JPEG compression, a sharp face (detail 126.5; a sharp face's median is
119). It had no operation for that: deblurring is only for motion blur, "defocus cannot be recovered reliably", so it applied a gentle polish and said "Not fully corrected: defocus blur".

## What was measured (original size kept: the 4x model is run at 2x and shrunk back; blur and compression as the engine's own analyzer reads them)

| variant | structure energy | face detail | halo (overshoot) | PSNR vs original | blur / compression after | time |
|---|---|---|---|---|---|---|
| original | 1.00 | 126.5 | 0 | | low / heavy | |
| engine, Natural (polish only; before this change) | 1.06 | 143.6 | 0.0002 | 38.2 | low / heavy | 1.7 s |
| engine, AI Enhance + Strong (polish only) | 1.10 | 160.3 | 0.023 | 32.4 | low / heavy | 1.8 s |
| Real-ESRGAN x4plus, blend 0.35 | 1.05 | 135.5 | 0.0035 | 39.2 | none / none | 69 s |
| Real-ESRGAN x4plus, blend 1.0 | 1.17 | 184.8 | 0.027 | 30.1 | none / none | |
| 4x-UltraSharp, blend 1.0 | 1.06 | 155.6 | 0.0003 | 37.5 | none / none | 70 s |
| NAFNet GoPro deblur, 1.0 | 1.15 | 201.2 | 0.059 | 29.9 | none / heavy | 17 s |
| **compact realesr-general-x4v3, blend 0.6** | 1.04 | 192.9 | 0.0015 | 39.1 | none / none | **6 s** |
| compact, blend 1.0 | 1.07 | 273.2 | 0.0059 | 34.7 | none / none | 6 s |

Read it honestly: polish (the only thing that ran) leaves blur and compression in place. The three model variants remove both at the same size and stay the same photograph (PSNR 37-39 dB).
On the pixels (face, eyes, beard, printed fabric) UltraSharp was the cleanest and sharpest, x4plus at 0.35 smoother, the compact model at 0.6 the crispest and a little over-sharp around the eyes
(at 1.0 its face detail left the validator's limit of 260). Hence the strengths per engine in `operations.run_detail`: UltraSharp 1.0, x4plus 0.35, compact 0.45. NAFNet GoPro added halos and face detail
beyond what a real face holds, as expected for a model trained on motion blur. The tile size of x4plus must stay 256 (512 took 387 s).

**Speed decides the default.** The compact model takes 6 s for what x4plus does in 69 s and UltraSharp in 70 s (one engine run through the whole engine: 7.7 s, accepted, structure x1.03,
compression and blur no longer found, face detail 169.9). It is the first choice; AI Enhance and the Strong level put 4x-UltraSharp first when it is switched on; x4plus is the fallback.
The model's output is computed once per step and shared by both blends (running it twice doubled the time: 145-148 s for the whole engine on the loaded machine, 72 s once).

## A trigger that was too loose
The first version offered detail restoration to any photograph whose frame read as blurred. A professional portrait with a sharp face and a defocused background (bokeh) qualified, ran the model for
118 s and was refused by the validator. "Soft" now means the frame reads blurred and the main face is not sharp; heavy compression is a reason on its own. The same photograph now ends in 0.9 s with
"did not need enhancement".

## What happens to a very dark photograph (the owner asked)
The product's own quality verdict, then the real engine, on the portrait multiplied by a darkness factor (1200 px). **Measured with the engine as it was before detail restoration was added** (the
quality verdict and the brightening do not depend on it; the dark versions were not run again with it):

| factor | mean brightness | quality verdict | Natural / auto | AI Enhance / Strong |
|---|---|---|---|---|
| 1.0 | 97 | accepted | polish only (detail restoration did not exist yet) | enlarged (a 1200 px photograph is below AI Enhance's 1280 px line) |
| 0.5 | 48 | needs review ("underexposed") | original kept (a dark scene is left as photographed) | brightened to 92 |
| 0.25 | 24 | needs review | brightened to 96 | brightened to 96 |
| 0.12 | 11 | needs review | brightened to 68 | brightened to 68 |
| 0.06 and darker | 5 and less | **rejected** ("severe_underexposure") | not enhanced | not enhanced |

Below a mean brightness of 6 (`QUALITY_REJECT_MIN_LUMINANCE`) a photograph has no usable signal and is refused with "Too dark: use more light, or enhance it from a brighter original"; a very dark but
visible photograph (mean 11) is brightened to a natural-looking picture. Looked at once (mean 11): faces and colours came back with no obvious artefacts; no metric was taken.

## Limits and what was not verified
* One soft photograph and one sharp one; the structure and halo numbers are from this photograph. A larger set would move the thresholds (`detail_min_gain` 1.02, `detail_max_gain` 1.6).
* Timings: 4x-UltraSharp and x4plus about 70 s were taken at load averages of 3 to 18 (another session was using the machine); the compact model's 6 s at a load of about 4. On a quiet machine all would be faster.
* Detail restoration runs inside the upload request when Celery runs eagerly (development default); the compact model keeps that to seconds, x4plus and UltraSharp do not. Without HD restoration it never runs.
* Nobody but the author of this note looked at the pixels, and only on this photograph.
