# Pipeline evaluation `before_e9c1099`

- Evidence: **`real_cpu_models`** (a learned model ran, on the CPU)
- PyTorch: not loaded
- Settings switched on for this run: none
- Model stages that ran: none

Level `auto`, style `natural`. Degradations are synthetic; PSNR and SSIM are measured against the clean photo, so a rise means the output moved toward it.

| Photo | Case | MP | Time (s) | PSNR before | PSNR after | SSIM before | SSIM after | Noise | Clipped % | Faces checked/adjusted |
|---|---|---|---|---|---|---|---|---|---|---|
| a_portrait_lowkey | original | 17.92 | 15.71 | 99.0 | 24.16 | 1.0 | 0.9464 | 1.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| a_portrait_lowkey | dark | 17.92 | 19.18 | 13.7 | 19.75 | 0.5859 | 0.9236 | 1.0 -> 1.0 | 0.0 -> 0.0 | 1/0 |
| a_portrait_lowkey | blurred | 17.92 | 7.93 | 37.53 | 23.68 | 0.9973 | 0.9445 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| b_portrait_harsh_sun | original | 24.0 | 7.7 | 99.0 | 27.58 | 1.0 | 0.975 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| b_portrait_harsh_sun | dark | 24.0 | 29.61 | 10.1 | 13.14 | 0.5507 | 0.8553 | 1.0 -> 1.0 | 0.0 -> 0.0 | 1/0 |
| b_portrait_harsh_sun | blurred | 24.0 | 17.06 | 31.73 | 26.18 | 0.9971 | 0.973 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| c_portrait_studio | original | 23.45 | 8.14 | 99.0 | 42.93 | 1.0 | 0.9978 | 0.0 -> 0.0 | 26.926 -> 26.932 | 1/0 |
| c_portrait_studio | dark | 23.45 | 28.71 | 6.52 | 8.3 | 0.5522 | 0.7454 | 1.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| c_portrait_studio | blurred | 23.45 | 8.57 | 32.67 | 32.28 | 0.9965 | 0.995 | 0.0 -> 0.0 | 26.873 -> 26.873 | 1/0 |
| d_hands_dark | original | 17.85 | 20.09 | 99.0 | 19.39 | 1.0 | 0.6155 | 0.0 -> 1.0 | 0.0 -> 0.0 | 0/0 |
| d_hands_dark | dark | 17.85 | 21.76 | 15.21 | 19.9 | 0.6126 | 0.7095 | 1.0 -> 4.0 | 0.0 -> 0.0 | 0/0 |
| d_hands_dark | blurred | 17.85 | 11.09 | 38.07 | 19.27 | 0.9986 | 0.6136 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |

## Stages that fell back

- adaptive_denoise: Denoising removed too little noise to justify changing the image.

## Identity, devices and memory

| Photo | Case | Identity | Similarity | Model stages (device) | Time (s) | GPU peak MB |
|---|---|---|---|---|---|---|
| a_portrait_lowkey | original | verified | 0.9891 | none | 15.71 | None |
| a_portrait_lowkey | dark | inconclusive | None | none | 19.18 | None |
| a_portrait_lowkey | blurred | verified | 0.9868 | none | 7.93 | None |
| b_portrait_harsh_sun | original | verified | 0.9848 | none | 7.7 | None |
| b_portrait_harsh_sun | dark | inconclusive | None | none | 29.61 | None |
| b_portrait_harsh_sun | blurred | verified | 0.9953 | none | 17.06 | None |
| c_portrait_studio | original | verified | 0.9883 | none | 8.14 | None |
| c_portrait_studio | dark | inconclusive | None | none | 28.71 | None |
| c_portrait_studio | blurred | verified | 0.9904 | none | 8.57 | None |
| d_hands_dark | original | inconclusive | None | none | 20.09 | None |
| d_hands_dark | dark | inconclusive | None | none | 21.76 | None |
| d_hands_dark | blurred | inconclusive | None | none | 11.09 | None |

Peak process memory: 1909 MB.
