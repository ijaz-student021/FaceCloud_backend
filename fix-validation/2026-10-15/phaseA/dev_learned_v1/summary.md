# Pipeline evaluation `dev_learned_v1`

- Evidence: **`real_cpu_models`** (a learned model ran, on the CPU)
- PyTorch: not loaded
- Settings switched on for this run: none
- Model stages that ran: none

Level `auto`, style `natural`. Degradations are synthetic; PSNR and SSIM are measured against the clean photo, so a rise means the output moved toward it.

| Photo | Case | MP | Time (s) | PSNR before | PSNR after | SSIM before | SSIM after | Noise | Clipped % | Faces checked/adjusted |
|---|---|---|---|---|---|---|---|---|---|---|
| a_portrait_lowkey | original | 17.92 | 12.77 | 99.0 | 24.16 | 1.0 | 0.9464 | 1.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| b_portrait_harsh_sun | original | 24.0 | 6.75 | 99.0 | 27.58 | 1.0 | 0.975 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| c_portrait_studio | original | 23.45 | 2.96 | 99.0 | 42.93 | 1.0 | 0.9978 | 0.0 -> 0.0 | 26.926 -> 26.932 | 1/0 |
| d_hands_dark | original | 17.85 | 12.62 | 99.0 | 19.39 | 1.0 | 0.6155 | 0.0 -> 1.0 | 0.0 -> 0.0 | 0/0 |
| e_highkey_white | original | 13.8 | 1.67 | 99.0 | 51.37 | 1.0 | 0.9996 | 0.0 -> 0.0 | 77.602 -> 77.602 | 0/0 |
| f_still_life | original | 24.0 | 2.97 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.071 -> 0.071 | 0/0 |
| g_closeup | original | 16.0 | 4.09 | 99.0 | 30.43 | 1.0 | 0.9911 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |

## Stages that fell back

- adaptive_denoise: Denoising removed too little noise to justify changing the image.

## Identity, devices and memory

| Photo | Case | Identity | Similarity | Model stages (device) | Time (s) | GPU peak MB |
|---|---|---|---|---|---|---|
| a_portrait_lowkey | original | verified | 0.9891 | none | 12.77 | None |
| b_portrait_harsh_sun | original | verified | 0.9848 | none | 6.75 | None |
| c_portrait_studio | original | verified | 0.9883 | none | 2.96 | None |
| d_hands_dark | original | inconclusive | None | none | 12.62 | None |
| e_highkey_white | original | inconclusive | None | none | 1.67 | None |
| f_still_life | original | inconclusive | None | none | 2.97 | None |
| g_closeup | original | verified | 0.9705 | none | 4.09 | None |

Peak process memory: 1816 MB.
