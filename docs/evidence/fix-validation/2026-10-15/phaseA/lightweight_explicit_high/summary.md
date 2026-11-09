# Pipeline evaluation `lightweight_explicit_high`

- Evidence: **`cpu_classical_only`** (no learned model ran; OpenCV processing only)
- PyTorch: not loaded
- Settings switched on for this run: none
- Model stages that ran: none

Level `high`, style `natural`. Degradations are synthetic; PSNR and SSIM are measured against the clean photo, so a rise means the output moved toward it.

| Photo | Case | MP | Time (s) | PSNR before | PSNR after | SSIM before | SSIM after | Noise | Clipped % | Faces checked/adjusted |
|---|---|---|---|---|---|---|---|---|---|---|
| low_key__05 | original | 1.57 | 0.45 | 99.0 | 25.25 | 1.0 | 0.9396 | 0.0 -> 1.0 | 0.522 -> 0.786 | 0/0 |
| low_key__06 | original | 2.56 | 0.5 | 99.0 | 23.7 | 1.0 | 0.9595 | 0.0 -> 1.0 | 0.0 -> 0.0 | 0/0 |
| low_key__07 | original | 1.15 | 0.25 | 99.0 | 29.4 | 1.0 | 0.9589 | 0.0 -> 0.0 | 2.68 -> 2.748 | 0/0 |
| low_key__08 | original | 1.44 | 0.34 | 99.0 | 32.08 | 1.0 | 0.9717 | 1.0 -> 1.0 | 0.0 -> 0.0 | 1/0 |
| textured__12 | original | 0.87 | 0.4 | 99.0 | 24.23 | 1.0 | 0.9435 | 1.0 -> 1.0 | 0.056 -> 0.226 | 2/0 |

## Stages that fell back

- none

## Identity, devices and memory

| Photo | Case | Identity | Similarity | Model stages (device) | Time (s) | GPU peak MB |
|---|---|---|---|---|---|---|
| low_key__05 | original | inconclusive | None | none | 0.45 | None |
| low_key__06 | original | inconclusive | None | none | 0.5 | None |
| low_key__07 | original | inconclusive | None | none | 0.25 | None |
| low_key__08 | original | verified | 0.9836 | none | 0.34 | None |
| textured__12 | original | verified | 0.9826 | none | 0.4 | None |

Peak process memory: 655 MB.
