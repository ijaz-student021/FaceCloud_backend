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

## Update (Phase 1B): face restoration switched on, and Strength "Automatic" measured

Models ran locally on an Apple M1 (MPS): NAFNet-GoPro, GFPGAN v1.4 and CodeFormer (non-commercial licences, demo only), Real-ESRGAN x4plus,
checkpoints fetched with `scripts/fetch_models.py` (checksums verified). One photograph (p1, 960x1280), degraded synthetically.

### Chains: NAFNet only / + GFPGAN / full chain (face detail = Laplacian variance of the face at 128 px; clean photograph = 101.1)

| case | chain | face detail before -> after | PSNR vs clean before -> after | seconds |
|---|---|---|---|---|
| mild blur | NAFNet | 14.4 -> 19.7 | 38.27 -> 37.69 | 30 |
| mild blur | + GFPGAN | 14.4 -> 99.1 | 38.27 -> 36.71 | 56 |
| mild blur | full (CodeFormer) | 14.4 -> 99.4 | 38.27 -> 36.71 | 52 |
| strong blur (σ7) | NAFNet | 2.0 -> 3.7 | 32.63 -> 34.04 | 27 |
| strong blur | + GFPGAN | 2.0 -> 87.3 | 32.63 -> 34.32 | 40 |
| strong blur | full | 2.0 -> 85.2 | 32.63 -> 34.15 | 31 |
| defocus | NAFNet | 8.9 -> 21.0 | 36.31 -> 38.53 | 26 |
| defocus | + GFPGAN | 8.9 -> 92.0 | 36.31 -> 37.05 | 45 |
| defocus | full | 8.9 -> 88.7 | 36.31 -> 37.13 | 33 |
| motion | NAFNet (the others add nothing) | 15.0 -> 57.9 | 34.29 -> 41.89 | 25 |
| 240 px low-res | full (Real-ESRGAN x2) | 67.9 -> 54.4 | n/a (size changes) | 5 |

Honest reading: NAFNet is the right tool for motion blur (the PSNR gain is large) and helps Gaussian/defocus blur only a little (x1.3-2.4). For
defocus and strong blur the face restorers produce most of the visible gain (face detail 85-99 against 101 for the clean photograph) while
PSNR falls a little for mild blur (they invent plausible detail). On the 240 px copy the face-detail metric falls after enlarging; that metric
is not valid across a change of scale (the same face has more pixels), so the upscale is judged by the validator's detail-versus-bicubic gate
instead. Full raw output: the tables above. Timings include model loading; GFPGAN adds about 15-25 s uncontended on this machine.

### A defocus-capable deblur model (Restormer, single-image defocus)

Rejected: about 10 minutes for one 640 px crop on MPS, PSNR 34.14 -> 33.86 (worse), and the licence of its DPDD training data is not confirmed.
The router therefore says why: defocus relies on face restoration (the router skip reason names it).

### Strength Automatic on six fixtures (`scripts/evaluate_auto.py`, style natural, long edge 1280)

| fixture | face detail before -> after | face luminance | noise level | chroma drift | size | seconds | plan |
|---|---|---|---|---|---|---|---|
| dim | 5.0 -> 46.2 | 17 -> 61 | 0.055 -> 0.146 | 2.25 | 960x1280 -> 1920x2560 | 61 | exposure 0.64, CodeFormer 0.82, tone 0.62, x2 Lanczos |
| noisy | 628.9 -> 516.1 | 76 -> 80 | 2.25 -> 1.5 | 3.06 | unchanged | 55 | denoise 0.91; HD refused (face_lost, no added detail) |
| motion blur | 15.0 -> 81.4 | 81 -> 82 | 0.068 -> 0.097 | 0.61 | 1920x2560 | 81 | NAFNet 0.85, CodeFormer 0.88, tone 0.41, Real-ESRGAN x2 |
| defocus blur | 8.9 -> 71.1 | 80 -> 83 | 0.051 -> 0.066 | 0.57 | 1920x2560 | 82 | NAFNet 0.91, CodeFormer 0.75, tone 0.41, Real-ESRGAN x2 |
| 240 px low-res | 67.9 -> 60.1 | 80 -> 80 | 0.25 -> 0.095 | 0.37 | 240x320 -> 960x1280 | 4 | tone 0.41, Real-ESRGAN x4 (HD 1920 not reached with one x4) |
| clean | 101.1 -> 50.0 | 81 -> 89 | 0.166 -> 0.17 | 0.35 | 1920x2560 | 53 | tone 0.40, Real-ESRGAN x2 |

Chroma drift is the mean CIELAB a/b difference against the input at the input's size (below 3 is not visible). Each plan differs because each
measurement differs; every fixture ends with an honest, non-empty summary. Limits: the "dim" fixture uses the classical exposure engine
(Zero-DCE fails its integrity check in this environment, Retinexformer is not enabled), so the face is lifted from luminance 17 to 61 and
not further; the "noisy" fixture (σ26 Gaussian) is only partly denoised (level 2.25 -> 1.5) and HD is refused because enlarging would amplify the
residual noise; face detail 'after' on enlarged faces is not comparable with 'before' (see above). Synthetic degradations of one photograph, one
device, no human judgement.
