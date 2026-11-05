# A dim face has an identity check, and AI Enhance accepts a smaller deblur gain (2026-11-05)

**The report.** Testing the `pro` preset (high / ai_max) on a photograph, the result showed "Face: Identity could not be verified ... The source face is too dark for a reliable identity comparison" and
"Blur: the result did not pass the safety checks", and the face restoration and the deblur were not applied.

## What the engine actually decided (result 126, `12.jpg`, 320 x 400; the same photograph traced with the real models)

| step | what happened |
|---|---|
| `super_resolution` (Real-ESRGAN) | **accepted**, 640 x 800 |
| `detail_restoration` (the compact model) | **accepted** |
| `motion_deblurring` (NAFNet-GoPro) | refused `face_not_clearer_or_over_sharpened`: the face's detail rose from 8.2 to 9.9, **x1.207 against the minimum 1.25**; edge gain 1.19 (limit 2.5) |
| `face_restoration` | skipped twice: "too small to restore without inventing it" (the face is 47 px) before the enlargement; after it, "Identity could not be verified": the face box's mean grey level is **41.4**, under the floor of **45** |

* **It is not a classical fallback.** `processing_summary` of the stored result: `ai_applied: true`, `classical_fallback: false`; Real-ESRGAN and the detail model ran. What did not run is the face restoration and the deblur.
* Two things in the brief did not match the code: the identity model is **SFace**, not ArcFace (ArcFace is an optional, off, non-commercial provider), and the live face model is **CodeFormer** (GFPGAN is off and not installed; measured not better, 2026-11-04). The deblur gate has **no SSIM threshold** (the only SSIM limits are the low-light lift's 0.70 and polish's 0.985), and its **maximum edge gain is already 2.5**. A brightening threshold of 40 would not even have applied to this face (41.4); the gate that refused it was the existing floor of 45.

## Fix 1: a dim face is embedded brightened, not refused at 45

`enhancement/identity_guard.py`: `_brightened_for_embedding`, called from `_embed_faces`, the one place faces are embedded, so the reference and every candidate are treated by the same rule. A face crop darker than `IDENTITY_GUARD_DARK_FACE_BRIGHTNESS_THRESHOLD` (40) is multiplied by `IDENTITY_GUARD_DARK_FACE_GAIN` (2.5) **for the embedding only**: no pixel of the photograph, of a candidate or of the crop a restoration model receives changes. The floor `IDENTITY_GUARD_MIN_FACE_LUMINANCE` goes from 45 to **15**.

**Measured** (the real SFace through the project's provider; 160 different public LFW faces darkened to a known face-box mean with sensor-like noise; "genuine" = the dark face against the bright photograph of the same face; "impostor" = the closest look-alike among the other 159):

| face mean | as is: median / 10th percentile / above 0.65 | brightened x2.5: median / 10th percentile / above 0.65 | look-alike impostor, highest | impostors above 0.65 |
|---|---|---|---|---|
| 50 | 0.970 / 0.938 / 100 % | 0.973 / 0.939 / 100 % | 0.446 | 0 |
| 41 | 0.958 / 0.916 / 100 % | 0.976 / 0.941 / 100 % | 0.443 | 0 |
| 30 | 0.936 / 0.885 / 100 % | 0.964 / 0.925 / 100 % | 0.404 | 0 |
| 20 | 0.889 / 0.818 / 100 % | 0.930 / 0.875 / 100 % | 0.417 | 0 |
| 16 | 0.853 / 0.762 / 100 % | 0.908 / 0.845 / 100 % | 0.404 | 0 |
| 12 | 0.783 / 0.657 / 91 % | 0.859 / 0.768 / 99 % | 0.392 | 0 |
| 10 | 0.719 / 0.596 / 77 % | 0.821 / 0.709 / 96 % | 0.417 | 0 |
| 8 | 0.641 / 0.480 / 48 % | 0.760 / 0.631 / 88 % | 0.387 | 0 |
| 6 | 0.520 / 0.318 / 18 % | 0.666 / 0.484 / 54 % | 0.407 | 0 |
| 4 | 0.358 / 0.169 / 2 % | 0.467 / 0.272 / 13 % | 0.360 | 0 |

(0.65 is the restoration limit, 0.80 the limit of every other step.) SFace copes with a dim face far better than the floor of 45 assumed; brightening the crop helps, by about 0.05 to 0.08 at a mean of 12 to 16, and **does not move a look-alike toward the person** (never above 0.45). Below about 8 the embedding is not reliable, so the floor is 15. A gain to a target mean (capped at x4) was within about 0.01 of the fixed x2.5 (0.867 against 0.859 at a mean of 12; 0.834 against 0.821 at 10); x8 was worse (noise); gamma helped less.

**On the photograph that started this** (real models, `pro`): the identity reference is built; the enlargement now verifies at similarity 0.978; the face restoration runs for the first time. **CodeFormer's candidate invents sunglasses over the shadowed eyes and moves the mouth and chin**; SFace gives it 0.779, above the 0.65 limit, but the geometry gate refuses it (landmarks moved 0.0755 beyond a plain enlargement of the original, a real move, not detector noise). So the face is correctly left as photographed. **The identity score alone would have accepted that candidate: the geometry gate is what protects here, and it is not relaxed.**

## Fix 2: AI Enhance accepts a face deblur with a detail gain of 1.10

`enhancement/adaptive/profile.py` + `service.py`: the `ai_max` profile carries `face_deblur_min_detail_gain` (`ADAPTIVE_AI_MAX_FACE_DEBLUR_MIN_DETAIL_GAIN`, 1.10); the engine lowers the validator's `face_deblur_min_detail_gain` to it for that style only and never raises a limit the operator set. Natural, dynamic, and the `standard` preset (high + natural) keep 1.25. The face's detail ceiling (260), the edge-gain ceiling (2.5), data consistency, halo, identity and geometry all still apply.

**Measured** (real NAFNet-GoPro; 70 LFW faces of 70 px or more, each degraded three ways; PSNR against the clean photograph, output minus input; "closer" = share of deblurs that moved toward the truth):

| blur | detail gain 1.10 to 1.25 (what the lower limit adds) | detail gain 1.25 and more (accepted today) |
|---|---|---|
| defocus (sigma 3) | 22 deblurs: +1.06 dB, **100 %** closer | 42: +1.30 dB, 100 % |
| low resolution (x0.4) | 10: +0.91 dB, **90 %** closer | 58: +0.67 dB, 79 % |
| motion (21 px) | 6: -0.26 dB, **17 %** closer | 59: +2.71 dB, 69 % |

So 1.25 turned away real, small improvements for defocused and low-resolution faces; for motion blur the added band is not better (6 cases, -0.26 dB: not harmful in size, not helpful). On the photograph above the gain is 1.207: NAFNet is now applied. The difference from the result without it is crisper edges (cap, hair, shoulder), mean absolute difference 1.89 of 255, no new artifact, the face interior unchanged.

## The library, before and after (real models; 8 photographs whose face is darker than the old floor, two requests each: natural / auto and the `pro` preset)

The whole engine with `IDENTITY_GUARD_MIN_FACE_LUMINANCE=45`, brightening off and AI Enhance at 1.25 (the earlier behaviour) against the new defaults. **All 16 runs now have a verified identity (it was unavailable in every one). 9 outputs are identical, 7 differ, and in 6 of the 7 more operations apply and none is lost:**

| photograph (face mean) | request | earlier | now |
|---|---|---|---|
| 3c8fcf43 (43.2) | natural | enlarge, detail | **face restoration**, enlarge, detail |
| 3c8fcf43 | pro | enlarge, detail | **face restoration**, enlarge, detail |
| aa35bb77 (44.9) | natural | detail | **face restoration**, detail |
| aa35bb77 | pro | enlarge, detail | **deblur, face restoration**, enlarge, detail |
| 67c29a14 (39.1) | pro | denoise, enlarge, detail | denoise, **deblur, face restoration**, enlarge, detail |
| 311f34cb (41.4, the photograph above) | pro | enlarge, detail | enlarge, **deblur**, detail (face restoration judged, refused) |
| 67c29a14 | natural | denoise, enlarge, detail | denoise, **face restoration** (enlarge and detail now refused, below) |

I looked at four of the newly accepted face restorations next to the earlier result (`67c29a14` natural, `3c8fcf43` natural, `aa35bb77` natural and pro): the same people, with clearer eyes and smiles and no invented feature, where the earlier result was smoother. The other two (`67c29a14` and `3c8fcf43` as `pro`) were not looked at. **One trade-off:** on `67c29a14` / natural the face is restored but the enlargement and the detail layer are then refused for `identity_changed`. After a face restoration is accepted at 0.65, the later steps are held to 0.80 against the original, and the restored face does not reach it. That is how the engine already treats every face it restores; it is not changed here (see below).

## Not verified, and what is left

* The darkening is synthetic (a scaled copy with noise); real dark photographs have heavier noise and compression. The real checks are the 8 library photographs and the one traced photograph, not a large set.
* Look-alike impostors come from 160 people; a larger gallery has closer look-alikes. The identity score is evidence, not proof, as the sunglasses candidate (0.779) shows.
* The deblur relaxation is measured on 70 faces of 70 px and more; the motion-blur band is 6 cases.
* **Left open:** after an accepted face restoration the later steps are held to 0.80 against the original (the `67c29a14` / natural trade-off); holding them to the restoration limit would keep the enlargement. The detector-noise effect on a face restoration's geometry gate (0.04) on soft faces is also untouched, on purpose: that gate is what refused the sunglasses.
* GFPGAN is not enabled or installed; the live face model is CodeFormer. Nothing here changes which model runs.
* **Off switches:** `IDENTITY_GUARD_MIN_FACE_LUMINANCE=45` and `IDENTITY_GUARD_DARK_FACE_BRIGHTNESS_THRESHOLD=0` restore the earlier identity behaviour; `ADAPTIVE_AI_MAX_FACE_DEBLUR_MIN_DETAIL_GAIN=1.25` restores the earlier deblur limit.
