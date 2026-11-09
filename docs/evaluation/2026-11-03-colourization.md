# Colourizing black-and-white photographs with DDColor (2026-11-03)

Status: measured on one Apple M1 (8 GB, PyTorch on MPS) with the real DDColor weights; the owner's black-and-white portrait (4608 x 3072) and six real-colour photographs from the owner's
library (grey-converted, lightness kept exactly) were used. Colourization runs only for the `dynamic` and `ai_max` styles (`enhancement/adaptive/profile.py`), only for a photograph whose
colour channels are empty, and is **off unless `DDCOLOR_ENABLED=True`**.

## The problem the owner found

The first colourization (DDColor "tiny", raw prediction) turned the teeth of the owner's portrait orange-brown and gave the whole photograph a brown cast. "Teeth are white; colours must be natural,
the colour scheme must not change by itself."

## What was measured

**1. Which model.** Brightest 10 percent of the mouth crop of the portrait (teeth), CIELAB chroma (0 = white): small model 39.5 (a +23.5, b +30.6), large model 15.0. The large model
(ConvNeXt-L, 912 MB) is the default; the face and mouth were looked at side by side (not only measured): tiny gave peach teeth and a red glow on the lip, large near-white teeth and natural pink lips.

**2. Does it work on other photographs? Ground truth.** Six real-colour photographs (seven rows: ids 91 and 92 are the same photograph) were made grey (CIELAB lightness kept, chroma removed),
colourized, and the chroma compared with the true colours (mean |da| and |db| at 1024 px; lower is closer). "No colour" is the grey input itself.

| photograph | no colour | raw prediction | damped (the default) |
|---|---|---|---|
| 100 | 5.55 | 8.84 | 6.33 |
| 95 | 9.41 | 9.71 | 7.86 |
| 91 (dark portrait; the raw prediction painted the wall **blue**, the truth is brown) | 3.30 | 7.90 | 4.32 |
| 119 | 16.38 | 17.52 | 15.67 |
| 108 | 11.90 | 17.82 | 10.12 |
| 99 | 5.21 | 7.92 | 6.37 |
| 92 (same as 91) | 3.30 | 7.90 | 4.32 |
| **mean** | **7.86** | **11.09** | **7.86** |

Read it honestly: a colourizer *guesses*; this error measure rewards a guess that stays near grey. The raw prediction is **worse than not colouring at all** (11.09 against 7.86) because it is too
saturated; damping brings it to the level of "no colour" while still adding plausible colour. It does not recover the true colours (it cannot: the information is not in a black-and-white photograph).
Other damping variants tried (mean error): x0.8 9.46, x0.7 8.73, x0.8 + dark 8.46, x0.8 + dark + highlights 8.37, **x0.7 + dark + highlights 7.86**, x0.9 + dark + highlights 8.95. The defaults
(`DDCOLOR_CHROMA_SCALE=0.7`, `DDCOLOR_DARK_DAMPING=0.3`, `DDCOLOR_HIGHLIGHT_DAMPING=0.5`) are the best of those seven on these photographs: **chosen on the same photographs they were measured on,
so the improvement is optimistic**. Teeth of the portrait with them: 10.6 in the sweep; 8.3 through the whole engine (polish included).

Tiny against large on ten photographs (four of them were already black and white, which only measures how much colour was invented): large was closer to the truth on seven, tiny on one
(a photograph that was already black and white), one tie. Mean error: no colour 4.76, tiny 11.45, large 9.22 (both raw).

**3. Through the real engine** (`AdaptiveImageEnhancementService`, real DDColor, polish on): the portrait with style `natural` is left black and white (`original_preserved`: nothing to
improve); with `ai_max` the polish and the colourization were both accepted, output 4608 x 3072, mean chroma 4.4 (the engine took 28.5 s on the loaded machine).
The production adapter alone, on the portrait and seven grey-converted photographs: lightness PSNR against its input 42.7 to 55.5 dB (colourization may not touch lightness), mean chroma
2.3 to 20.8, 99th percentile 15 to 47, cast 0.8 to 19.0 (the validator's backstop limits are 38 dB, 28, 58 and 26).

## What else this change fixed (same session, real models)

* A 480 x 270 rooftop photograph with a 21 px silhouette was refused for `face_geometry_changed` at 0.078 (1.6 px) for a Real-ESRGAN candidate that passed every other gate; with
  `ADAPTIVE_LANDMARK_PIXEL_FLOOR` (2 px) the engine enlarges it to 960 x 540.
* Polish was refused on the owner's soft 944 x 1416 portrait at a landmark shift of 0.053 although it moved no pixel; the face's own structure (SSIM of the face crop, 0.996 against 0.965 for a
  2 px shift) now decides for polish. With Dynamic / AI Enhance the polish is accepted: structure energy +16 percent, contrast (standard deviation) +7 percent on that photograph.
* AI Enhance used to do less than Dynamic when its dark-scene lift was refused (the attempt blocked the polish); a refused attempt that the style or strength asked for no longer blocks it.

## Limits and what was not verified

* Six distinct photographs; a larger set would move the numbers. No human judged the colours (the face and mouth crops were looked at by the author of this note, once).
* Grey-converted colour photographs are not scanned film: grain, tone curves and toning differ. Only one genuinely black-and-white photograph (the portrait) was colourized end to end.
* Non-photographic pictures (line art, screenshots, text) were not tried; a colourization there is meaningless.
* The unit tests use stand-in networks (CI has no weights); the real network was run only on this one laptop. Timings were taken while another process on the machine kept the load average
  at 13 to 17 and the system was swapping: 16 to 69 s per photograph in the adapter test, against about 12 s for the model on the portrait on a quiet machine. Treat them as upper bounds.
* Faces: identity is checked as for any operation when the account allows biometric processing; without consent the colourization still runs (it does not change geometry) and the result says
  identity was not verified.
