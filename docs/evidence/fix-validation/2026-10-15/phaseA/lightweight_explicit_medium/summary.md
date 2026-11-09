# Pipeline evaluation `lightweight_explicit_medium`

- Evidence: **`cpu_classical_only`** (no learned model ran; OpenCV processing only)
- PyTorch: not loaded
- Settings switched on for this run: none
- Model stages that ran: none

Level `medium`, style `natural`. Degradations are synthetic; PSNR and SSIM are measured against the clean photo, so a rise means the output moved toward it.

| Photo | Case | MP | Time (s) | PSNR before | PSNR after | SSIM before | SSIM after | Noise | Clipped % | Faces checked/adjusted |
|---|---|---|---|---|---|---|---|---|---|---|
| low_key__05 | original | 1.57 | 0.47 | 99.0 | 29.05 | 1.0 | 0.9713 | 0.0 -> 1.0 | 0.522 -> 0.71 | 0/0 |
| low_key__06 | original | 2.56 | 0.52 | 99.0 | 27.31 | 1.0 | 0.979 | 0.0 -> 1.0 | 0.0 -> 0.0 | 0/0 |
| low_key__07 | original | 1.15 | 0.24 | 99.0 | 34.71 | 1.0 | 0.9864 | 0.0 -> 0.0 | 2.68 -> 2.814 | 0/0 |
| low_key__08 | original | 1.44 | 0.36 | 99.0 | 39.43 | 1.0 | 0.9919 | 1.0 -> 1.0 | 0.0 -> 0.0 | 1/0 |
| textured__12 | original | 0.87 | 0.33 | 99.0 | 28.07 | 1.0 | 0.9736 | 1.0 -> 1.0 | 0.056 -> 0.165 | 2/0 |

## Stages that fell back

- none

## Identity, devices and memory

| Photo | Case | Identity | Similarity | Model stages (device) | Time (s) | GPU peak MB |
|---|---|---|---|---|---|---|
| low_key__05 | original | inconclusive | None | none | 0.47 | None |
| low_key__06 | original | inconclusive | None | none | 0.52 | None |
| low_key__07 | original | inconclusive | None | none | 0.24 | None |
| low_key__08 | original | verified | 0.9951 | none | 0.36 | None |
| textured__12 | original | verified | 0.9868 | none | 0.33 | None |

Peak process memory: 621 MB.
