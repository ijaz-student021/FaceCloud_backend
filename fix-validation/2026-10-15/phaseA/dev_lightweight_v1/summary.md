# Pipeline evaluation `dev_lightweight_v1`

- Evidence: **`cpu_classical_only`** (no learned model ran; OpenCV processing only)
- PyTorch: not loaded
- Settings switched on for this run: none
- Model stages that ran: none

Level `auto`, style `natural`. Degradations are synthetic; PSNR and SSIM are measured against the clean photo, so a rise means the output moved toward it.

| Photo | Case | MP | Time (s) | PSNR before | PSNR after | SSIM before | SSIM after | Noise | Clipped % | Faces checked/adjusted |
|---|---|---|---|---|---|---|---|---|---|---|
| a_portrait_lowkey | original | 17.92 | 9.73 | 99.0 | 41.4 | 1.0 | 0.9953 | 1.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| b_portrait_harsh_sun | original | 24.0 | 3.59 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| c_portrait_studio | original | 23.45 | 3.25 | 99.0 | 42.93 | 1.0 | 0.9978 | 0.0 -> 0.0 | 26.926 -> 26.932 | 1/0 |
| d_hands_dark | original | 17.85 | 1.88 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| e_highkey_white | original | 13.8 | 1.64 | 99.0 | 51.37 | 1.0 | 0.9996 | 0.0 -> 0.0 | 77.602 -> 77.602 | 0/0 |
| f_still_life | original | 24.0 | 2.93 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.071 -> 0.071 | 0/0 |
| g_closeup | original | 16.0 | 2.15 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |

## Stages that fell back

- none

## Identity, devices and memory

| Photo | Case | Identity | Similarity | Model stages (device) | Time (s) | GPU peak MB |
|---|---|---|---|---|---|---|
| a_portrait_lowkey | original | verified | 0.9954 | none | 9.73 | None |
| b_portrait_harsh_sun | original | verified | 1.0 | none | 3.59 | None |
| c_portrait_studio | original | verified | 0.9883 | none | 3.25 | None |
| d_hands_dark | original | inconclusive | None | none | 1.88 | None |
| e_highkey_white | original | inconclusive | None | none | 1.64 | None |
| f_still_life | original | inconclusive | None | none | 2.93 | None |
| g_closeup | original | verified | 1.0 | none | 2.15 | None |

Peak process memory: 1796 MB.
