# Pipeline evaluation `learned_v1`

- Evidence: **`real_cpu_models`** (a learned model ran, on the CPU)
- PyTorch: not loaded
- Settings switched on for this run: none
- Model stages that ran: none

Level `auto`, style `natural`. Degradations are synthetic; PSNR and SSIM are measured against the clean photo, so a rise means the output moved toward it.

| Photo | Case | MP | Time (s) | PSNR before | PSNR after | SSIM before | SSIM after | Noise | Clipped % | Faces checked/adjusted |
|---|---|---|---|---|---|---|---|---|---|---|
| bright__13 | original | 1.18 | 0.37 | 99.0 | 49.42 | 1.0 | 0.9994 | 0.0 -> 0.0 | 60.434 -> 60.527 | 0/0 |
| bright__14 | original | 1.92 | 0.35 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.076 -> 0.076 | 0/0 |
| clean_mid__01 | original | 1.92 | 0.31 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 1.607 -> 1.607 | 0/0 |
| clean_mid__02 | original | 2.07 | 0.37 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.747 -> 0.747 | 0/0 |
| clean_mid__03 | original | 1.92 | 0.39 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| clean_mid__04 | original | 1.92 | 0.33 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| jpeg_artifacts__01 | original | 1.92 | 0.32 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 1.584 -> 1.584 | 0/0 |
| jpeg_artifacts__02 | original | 2.07 | 0.35 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.694 -> 0.694 | 0/0 |
| low_key__05 | original | 1.57 | 0.92 | 99.0 | 23.04 | 1.0 | 0.8699 | 0.0 -> 1.0 | 0.522 -> 0.626 | 0/0 |
| low_key__06 | original | 2.56 | 1.81 | 99.0 | 28.06 | 1.0 | 0.9754 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| low_key__07 | original | 1.15 | 0.68 | 99.0 | 18.07 | 1.0 | 0.7375 | 0.0 -> 0.0 | 2.68 -> 2.903 | 0/0 |
| low_key__08 | original | 1.44 | 0.84 | 99.0 | 22.58 | 1.0 | 0.8357 | 1.0 -> 1.0 | 0.0 -> 0.0 | 1/0 |
| textured__09 | original | 1.57 | 0.32 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 1.072 -> 1.072 | 0/0 |
| textured__10 | original | 1.39 | 0.38 | 99.0 | 58.45 | 1.0 | 1.0 | 0.0 -> 0.0 | 72.848 -> 72.858 | 0/0 |
| textured__11 | original | 1.57 | 0.3 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.872 -> 0.872 | 0/0 |
| textured__12 | original | 0.87 | 0.92 | 99.0 | 25.53 | 1.0 | 0.9371 | 1.0 -> 1.0 | 0.056 -> 0.064 | 2/0 |

## Stages that fell back

- none

## Identity, devices and memory

| Photo | Case | Identity | Similarity | Model stages (device) | Time (s) | GPU peak MB |
|---|---|---|---|---|---|---|
| bright__13 | original | inconclusive | None | none | 0.37 | None |
| bright__14 | original | inconclusive | None | none | 0.35 | None |
| clean_mid__01 | original | inconclusive | None | none | 0.31 | None |
| clean_mid__02 | original | inconclusive | None | none | 0.37 | None |
| clean_mid__03 | original | inconclusive | None | none | 0.39 | None |
| clean_mid__04 | original | inconclusive | None | none | 0.33 | None |
| jpeg_artifacts__01 | original | inconclusive | None | none | 0.32 | None |
| jpeg_artifacts__02 | original | inconclusive | None | none | 0.35 | None |
| low_key__05 | original | inconclusive | None | none | 0.92 | None |
| low_key__06 | original | inconclusive | None | none | 1.81 | None |
| low_key__07 | original | inconclusive | None | none | 0.68 | None |
| low_key__08 | original | verified | 0.9876 | none | 0.84 | None |
| textured__09 | original | inconclusive | None | none | 0.32 | None |
| textured__10 | original | inconclusive | None | none | 0.38 | None |
| textured__11 | original | inconclusive | None | none | 0.3 | None |
| textured__12 | original | verified | 0.9672 | none | 0.92 | None |

Peak process memory: 1252 MB.
