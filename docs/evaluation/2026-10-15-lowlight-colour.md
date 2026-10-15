# Low-light colour: why the lift looked grey-brown, and what was measured

Reproduce: `RETINEXFORMER_ENABLED=True python scripts/evaluate_lowlight_colour.py --images <photos> --out <dir>`
(16 phone photographs at 640 px, 4 darkening levels, the real Retinexformer; calibration photographs are the even-indexed ones,
held-out the odd-indexed; nothing was chosen on the held-out half). Date: 2026-10-15, Apple M1 (MPS).

## The problem

The defence demonstration showed the brown shirt turning grey-brown and the skin paler after a low-light lift, which PSNR rewards.
Two causes were found by reading the code and measuring, not one:

1. **The network's own colours are pale.** Raw Retinexformer output keeps only 0.56-0.81 of the clean photograph's chroma on held-out
   photographs and rotates hue by 6-10 degrees. Its output also carries visible colour speckle on a noisy dark input.
2. **The earlier fix under-restored colour.** `services._amplify_source_chroma` scaled the *source's* CIELAB chroma by the square
   root of the lightness gain. CIELAB chroma grows roughly in proportion to lightness under an exposure change, so a square root leaves
   the result up to about 40 % paler on the darkest inputs (retention 0.61-0.92 held-out, 0.57-0.84 on the calibration half). A larger exponent
   over-saturates (exponent 1.0: retention 1.37-1.53; 0.75: 0.92-1.16, unstable), so tuning the exponent was rejected.

## The method kept

`enhancement/adaptive/colour.py::chromaticity_linear`: in linear light, brightening multiplies all three channels by the same factor, so
colour *ratios* (chromaticity) are unchanged. The ratios come from the smoothed source (which also removes the sensor's colour
noise), the luminance comes from the network, and a pixel that would leave the display range is scaled down uniformly rather than
clipped channel by channel (highlight protection). No colour is invented, nothing is tuned: there is no exponent or blend weight.
It is selected by `ADAPTIVE_LOWLIGHT_COLOUR=chromaticity` (default); `legacy` keeps the earlier method for comparison.

## Held-out photographs (8 of 16), against the clean photograph

| Darkening | Method | Chroma error (lower better) | Chroma retention (1.0 = clean) | Hue shift (deg) | LPIPS (lower better) |
|---|---|---|---|---|---|
| gamma x0.25 (earlier benchmark) | raw network | 8.76 | 0.56 | 6.5 | 0.267 |
| | square-root chroma (before) | 7.73 | 0.61 | 4.7 | 0.277 |
| | **chromaticity (now)** | **4.62** | **0.83** | **3.1** | **0.252** |
| linear 1/4 | raw network | 5.04 | 0.81 | 6.0 | 0.258 |
| | square-root chroma (before) | 3.81 | 0.92 | 3.2 | 0.271 |
| | **chromaticity (now)** | 4.16 | 1.10 | 2.5 | 0.259 |
| linear 1/8 | raw network | 5.97 | 0.73 | 7.6 | 0.338 |
| | square-root chroma (before) | 4.55 | 0.81 | 3.5 | 0.341 |
| | **chromaticity (now)** | **3.38** | **1.03** | **2.8** | **0.328** |
| linear 1/16 | raw network | 7.51 | 0.66 | 10.4 | 0.419 |
| | square-root chroma (before) | 6.37 | 0.68 | 4.4 | 0.412 |
| | **chromaticity (now)** | **3.34** | **0.93** | **3.7** | **0.398** |

Chromaticity has the lowest hue shift at every level (also against the exponent 0.75 and 1.0 variants in the full tables) and the lowest chroma error of the three methods shown at three of four (at 1/4 darkening the earlier method
is slightly closer, 3.81 against 4.16, while over-shooting retention 1.10); PSNR and SSIM are equal or better, LPIPS equal or better.
The full tables, including skin-pixel results and the calibration half, are produced by the script
(`lowlight_colour.md`, `lowlight_colour.json`).

## What it changed in the real engine

Full adaptive engine, real Retinexformer, the same dark inputs, colour method the only difference (3 photographs x 2 darkenings):

* photograph 0, both darkenings: **before, the lift was rejected and the original kept (`original_preserved`); now `enhanced`**,
  chroma retention 0.26 -> 0.95 and 0.54 -> 1.09, PSNR 12.7 -> 16.1 and 13.7 -> 24.4;
* photograph 1: enhanced both ways, retention 0.58 -> 0.75 and 0.70 -> 0.82;
* photograph 13: preserved both ways at the heavy darkening because the lift did not make the face clearer (face tonal range
  35 -> 45 where 110 is the target), which is the gate working, not a colour effect.

## A gate that can see it

The old colour gate for lifts compared absolute CIELAB a/b with the parent's, which cannot see a pale result (the source's chroma is kept
by construction) and penalises legitimate chroma growth. A lift is now judged on the exposure-invariant quantities in linear light
(`colour.lift_colour_metrics`): hue shift and *saturation ratio* against the parent. Limits (`validator.Limits`): ratio >= 0.85
(between the raw network's maximum, 0.80, and the used method's minimum, 0.92, on the calibration half; the held-out half agreed: used
method 0.94-0.99, raw 0.63-0.80), <= 1.18 (its reciprocal), hue <= 15 degrees (a catastrophic-rotation guard; hue does not
discriminate the methods at the levels measured). Reasons reported: `colour_washed_out`, `colour_oversaturated`, `colour_drift`.
Consequence: with `legacy` colour most lifts now fail the gate (ratios 0.73-0.83) and return the original; that is intended, the setting
exists for comparison.

## Limits (read before quoting)

* The darkening is synthetic. The linear-light variants preserve chromaticity by construction, which favours a chromaticity method;
  the `gamma x0.25` case does not, and there the method still wins (retention 0.83 against 0.61) but does not reach 1.0.
* No real night photographs were available. A real night photograph has its own white balance and tone curve; this method keeps the
  camera's colour cast rather than correcting it, deliberately.
* 16 photographs, one phone, mostly one person; skin tone diversity is untested. The "skin" figures use a simple YCrCb mask.
* The contact sheet was looked at only by the author. Numbers say which method keeps colour closer to the clean photograph, not
  that a viewer will like the result; a structured human evaluation is still to do.
* Luminance grain of the lifted result is not changed by this. A post-lift denoise rule was measured and rejected
  (`2026-10-15-denoising.md`).
