# Automatic enhancement after upload: one batch of 20 photographs (2026-10-22)

What this measures: a batch of 20 photographs uploaded through the real API (`POST /api/images/upload/multiple/`), with the real models, in a throwaway database.
The upload request itself returns when everything is done, because background jobs run inside it (`CELERY_TASK_ALWAYS_EAGER`, `processing_mode: "inline"`), so
**the duration of the request is the time to finish the whole batch**, one photograph after another, with nothing running in parallel.

## Method and limits (read before quoting a number)

* **Machine:** an Apple M1 laptop, nothing else running. The PyTorch models used the device their own setting chose (`auto`: MPS for Retinexformer, which is the only one whose device was recorded here); OpenCV work ran on the CPU. One run of each configuration; no repetition, so the
  seconds are one measurement, not an average.
* **The photographs are synthetic variants of one public-domain portrait** (the NASA astronaut picture bundled with scikit-image), cropped to head and shoulders and
  scaled to 480 x 600, plus one picture without a face. They are: clean, resized (300, 640, 768 px wide), flipped, JPEG at quality 40, noisy (sigma 10, 18, 25), noisy and dim,
  motion-blurred (15, 25 px), defocused (radius 5, 8), dimmed (x0.55, x0.40, x0.30), very dark (x0.15, x0.10) and the face-less scene. **They are not a sample of real uploads**:
  one face, one source, degradations I applied. They show what the pipeline does with each kind of defect, not how often.
* A first attempt used the whole 512 x 512 portrait; the face check refused every copy (`face_too_small`: the face is about 3% of that frame), so it measured nothing and was thrown away.
* Quality verdicts are the real ones (not forced). Identity checking: **verified set** with consent granted and YuNet + SFace; **defaults** have no identity model configured (the
  repository's default), so no identity comparison ran there.
* `verified set` = Retinexformer, NAFNet denoise and deblur, Real-ESRGAN on; Zero-DCE, CodeFormer, GFPGAN off. `defaults` = every setting as shipped (Zero-DCE on,
  Retinexformer off, Real-ESRGAN and NAFNet-GoPro on). Only the checkpoint folder was pointed at the main checkout's `.runtime`.
* Not measured: a real Celery worker (concurrency, queueing), a batch over 20, real photographs of people, the S3 backend.

## Result

| | Verified model set | Repository defaults | Automatic enhancement off (control) |
|---|---|---|---|
| Time to finish the batch | **84.8 s** | **85.3 s** | 7.2 s (the checks alone) |
| Outcomes | {'original_preserved': 8, 'enhanced': 5, 'partially_enhanced': 5, 'not enhanced (rejected)': 2} | {'original_preserved': 8, 'enhanced': 4, 'partially_enhanced': 6, 'not enhanced (rejected)': 2} | no enhancement made |
| Models that changed pixels | nafnet_gopro, nafnet_sidd, opencv_nlm, realesrgan, retinexformer | nafnet_gopro, opencv_nlm, realesrgan, zero_dce | none |
| Notification | ['10 photos enhanced (8 kept as uploaded because enhancing them would not have helped).'] | ['10 photos enhanced (8 kept as uploaded because enhancing them would not have helped).'] | none |
| Notifications per photograph | 0 | 0 | 0 |
| Batch status | partial (the 2 rejected photographs make it partial, as before) | partial | partial |
| Rendering policy of the results | adaptive_v1 | adaptive_v1 | |

Of the 84.8 s, about 77.6 s is enhancement (the control spent 7.2 s on the checks alone); seven of the eight kept-as-uploaded photographs take under half a second (the engine sees that nothing would help and keeps the original, `original_preserved`, a successful outcome); the eighth, the one dimmed to x0.40, took 1.0 s (defaults) and 3.5 s (verified set).

## Per photograph (outcome · models that changed pixels · seconds the enhancement took)

| # | Photograph | Quality verdict | Verified model set | Repository defaults |
|---|---|---|---|---|
| 01 | clean_480 | accepted | original_preserved · none · 0.41 s | original_preserved · none · 0.3 s |
| 02 | clean_640 | accepted | original_preserved · none · 0.37 s | original_preserved · none · 0.35 s |
| 03 | clean_768 | accepted | original_preserved · none · 0.21 s | original_preserved · none · 0.21 s |
| 04 | clean_flipped | accepted | original_preserved · none · 0.24 s | original_preserved · none · 0.25 s |
| 05 | clean_small_300 | accepted | enhanced · realesrgan · 9.08 s | enhanced · realesrgan · 10.11 s |
| 06 | clean_jpeg_q40 | accepted | original_preserved · none · 0.15 s | original_preserved · none · 0.1 s |
| 07 | noisy_sigma10 | accepted | enhanced · nafnet_sidd · 8.0 s | enhanced · opencv_nlm · 1.3 s |
| 08 | noisy_sigma18 | accepted | enhanced · nafnet_sidd · 6.63 s | enhanced · opencv_nlm · 0.92 s |
| 09 | noisy_sigma25 | accepted | partially_enhanced · nafnet_sidd, opencv_nlm · 11.14 s | partially_enhanced · opencv_nlm · 0.9 s |
| 10 | noisy_dim | accepted | original_preserved · none · 0.26 s | original_preserved · none · 0.14 s |
| 11 | motion_blur_15 | accepted | enhanced · nafnet_gopro · 6.51 s | partially_enhanced · nafnet_gopro · 8.34 s |
| 12 | motion_blur_25 | accepted | partially_enhanced · nafnet_gopro · 5.04 s | partially_enhanced · nafnet_gopro · 7.83 s |
| 13 | defocus_5 | accepted | partially_enhanced · nafnet_gopro · 5.06 s | partially_enhanced · nafnet_gopro · 5.29 s |
| 14 | defocus_8 | accepted | partially_enhanced · nafnet_gopro · 5.17 s | partially_enhanced · nafnet_gopro · 5.13 s |
| 15 | dim_x0.55 | accepted | original_preserved · none · 0.29 s | original_preserved · none · 0.14 s |
| 16 | dim_x0.40 | needs_review | original_preserved · none · 3.47 s | original_preserved · none · 0.99 s |
| 17 | dim_x0.30 | needs_review | partially_enhanced · retinexformer · 2.42 s | partially_enhanced · zero_dce · 0.72 s |
| 18 | very_dark_x0.15 | rejected | not enhanced (rejected) | not enhanced (rejected) |
| 19 | very_dark_x0.10 | rejected | not enhanced (rejected) | not enhanced (rejected) |
| 20 | no_face_scene | needs_review | enhanced · realesrgan · 12.11 s | enhanced · realesrgan · 33.96 s |

The two very dark photographs are refused by the quality check (`severe_underexposure`), so no enhancement is made for them: that is the existing rule, unchanged.
The face-less picture is `needs_review`; in a personal workspace no moderator can decide, so it is enhanced at once (upscaled), as the person folders are.

Raw results: `evidence/2026-10-22/auto-enhancement-20-photos-{verified,defaults,off}.json`.
