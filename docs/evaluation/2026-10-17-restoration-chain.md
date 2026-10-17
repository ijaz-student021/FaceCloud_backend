# Restoration chain on real photographs (2026-10-17)

Reproduce: `FACE_DETECTOR_PROVIDER=yunet_v1 ... python scripts/evaluate_restoration.py --images <folder> --count 3` (see the script's docstring for
the environment). Three photographs from one phone, mostly one person, resized to 1280 px, three degraded copies each (Gaussian blur sigma 2.5
= "mild", sigma 7 = "strong", and the photograph reduced to a 240 px shorter edge), run through `enhance_bgr` as an upload would be (default
policy, restoration escalation on, biometric processing off, so no face restorer could run anyway). Apple M1, MPS. NAFNet-GoPro and
Real-ESRGAN x4plus installed; CodeFormer and GFPGAN **not** installed (and off by default).

"Face detail" is the Laplacian variance of the main face resized to 128 px (a sharp face about 100 to 170 here, strongly blurred below 25).

## What the numbers say

* **Blurred faces are now found and deblurred.** Before this change every one of the six blurred copies went through the lightweight
  pipeline and came out essentially unchanged (face detail x1.00 to x1.06, no stage, no explanation). Now all six are escalated to the chain,
  the deblur stage is accepted, and the result lists it.
* **The gain is real but modest.** Median face detail x1.32 (mild) and x1.44 (strong). A mildly blurred face goes from about 13 to about 17
  against a clean 100 to 166; a strongly blurred one from about 2 to about 2.3. The faces are **not restored to sharp**. NAFNet-GoPro is a
  motion-deblurring model; Gaussian defocus is outside what it was trained for, which agrees with the earlier measurement
  (`2026-10-15-blur-routing.md`: +0.9 to +1.5 dB on large defocus). The engine says so: outcome `partially_enhanced`, with `face_blur` and
  `face_poor` listed as unresolved. Clearly recovering a defocused face needs a generative face restorer (CodeFormer or GFPGAN), which is
  non-commercial, not installed here, and off by default; with one installed and acknowledged, the same chain adds the `face_restore` stage.
* **Low-resolution photographs are enlarged.** All three 240 px copies went to 480 px (x2) with the upscale stage accepted. The face-detail
  figure *falls* (a median x0.71) because that measure resizes every face to 128 px, so an enlarged soft face reads smoother; it is not a
  measure of upscaling quality and no claim of recovered detail is made. Real-ESRGAN's output is blended 35 percent with a bicubic
  enlargement (`REALESRGAN_OUTPUT_BLEND`).
* **Time:** deblur 24 to 27 s for a 1.2 MP photograph, upscale 4 to 5 s for 0.08 MP, on the M1's GPU (MPS). Not representative of a server.

## Two defects in the existing engine found by this evaluation (fixed in this change)

1. An upscaled candidate was **never accepted for a photograph with a face**: the identity gate compared the original's face coordinates with
   the enlarged candidate's, so every enlarged face looked "lost" (`face_lost`) unless it sat near the top-left corner. The original's
   faces are now brought onto the candidate's pixel grid first.
2. The deblur gate judged only whole-frame edge energy, which cannot see a face that is the only blurred part. A deblur chosen for a blurred face
   is now judged on the face's own detail (it must rise at least x1.25 and not exceed what a real face holds).

A third adjustment, evidence-based: plain bicubic enlargement of 240 px portraits already moves the five landmarks by 0.019 to 0.037 of the
face width, so the 0.04 limit left no room for an upscale; upscaled candidates may move them up to 0.06 (`ADAPTIVE_UPSCALE_LANDMARK_SHIFT_MAX`).

## Raw output


Stages available on the machine that ran this: deblur=yes, face_restore=NO, upscale=yes

| photo | case | face detail clean | before | after | face blur seen | size in -> out | stages run | seconds | outcome |
|---|---|---|---|---|---|---|---|---|---|
| p1 | mild_blur | 101.1 | 14.4 | 19.7 | high | 960x1280 -> 960x1280 | deblur | 26.7 | partially_enhanced |
| p1 | strong_blur | 101.1 | 2.0 | 3.7 | high | 960x1280 -> 960x1280 | deblur | 23.9 | partially_enhanced |
| p1 | low_res_240 | 101.1 | 67.9 | 54.4 | none | 240x320 -> 480x640 | upscale x2 | 4.3 | enhanced |
| p2 | mild_blur | 165.7 | 12.9 | 16.6 | high | 963x1280 -> 963x1280 | deblur | 25.9 | partially_enhanced |
| p2 | strong_blur | 165.7 | 1.7 | 2.3 | high | 963x1280 -> 963x1280 | deblur | 24.4 | partially_enhanced |
| p2 | low_res_240 | 165.7 | 203.3 | 62.5 | none | 240x319 -> 480x638 | upscale x2 | 4.2 | enhanced |
| p3 | mild_blur | 22.4 | 2.8 | 3.7 | high | 963x1280 -> 963x1280 | deblur | 25.3 | partially_enhanced |
| p3 | strong_blur | 22.4 | 0.9 | 1.3 | high | 963x1280 -> 963x1280 | deblur | 24.3 | partially_enhanced |
| p3 | low_res_240 | 22.4 | 71.1 | 50.7 | unknown | 240x319 -> 480x638 | upscale x2 | 4.1 | enhanced |

## By case (medians)

| case | n | face detail before | after | gain (after/before) | escalated | output larger |
|---|---|---|---|---|---|---|
| mild_blur | 3 | 12.9 | 16.6 | x1.32 | 3/3 | 0/3 |
| strong_blur | 3 | 1.7 | 2.3 | x1.44 | 3/3 | 0/3 |
| low_res_240 | 3 | 71.1 | 54.4 | x0.71 | 3/3 | 3/3 |

## Restoration needed but not done

* Restoration was needed but not completed: face_restore (Biometric processing is not permitted, so faces are not restored.)
