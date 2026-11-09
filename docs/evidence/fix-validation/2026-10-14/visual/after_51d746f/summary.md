# Pipeline evaluation `after_51d746f`

- Evidence: **`real_cpu_models`** (a learned model ran, on the CPU)
- PyTorch: not loaded
- Settings switched on for this run: none
- Model stages that ran: none

Level `auto`, style `natural`. Degradations are synthetic; PSNR and SSIM are measured against the clean photo, so a rise means the output moved toward it.

| Photo | Case | MP | Time (s) | PSNR before | PSNR after | SSIM before | SSIM after | Noise | Clipped % | Faces checked/adjusted |
|---|---|---|---|---|---|---|---|---|---|---|
| a_portrait_lowkey | original | 17.92 | 16.0 | 99.0 | 24.16 | 1.0 | 0.9464 | 1.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| a_portrait_lowkey | dark | 17.92 | 17.82 | 13.7 | 19.83 | 0.5859 | 0.9242 | 1.0 -> 1.0 | 0.0 -> 0.0 | 1/0 |
| a_portrait_lowkey | blurred | 17.92 | 6.11 | 37.53 | 23.68 | 0.9973 | 0.9445 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| b_portrait_harsh_sun | original | 24.0 | 7.62 | 99.0 | 27.58 | 1.0 | 0.975 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| b_portrait_harsh_sun | dark | 24.0 | 22.34 | 10.1 | 13.14 | 0.5507 | 0.8553 | 1.0 -> 1.0 | 0.0 -> 0.0 | 1/0 |
| b_portrait_harsh_sun | blurred | 24.0 | 8.76 | 31.73 | 26.12 | 0.9971 | 0.9725 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| c_portrait_studio | original | 23.45 | 3.97 | 99.0 | 42.93 | 1.0 | 0.9978 | 0.0 -> 0.0 | 26.926 -> 26.932 | 1/0 |
| c_portrait_studio | dark | 23.45 | 28.11 | 6.52 | 8.3 | 0.5522 | 0.7454 | 1.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| c_portrait_studio | blurred | 23.45 | 3.95 | 32.67 | 32.29 | 0.9965 | 0.9949 | 0.0 -> 0.0 | 26.873 -> 26.874 | 1/0 |
| d_hands_dark | original | 17.85 | 18.61 | 99.0 | 19.39 | 1.0 | 0.6155 | 0.0 -> 1.0 | 0.0 -> 0.0 | 0/0 |
| d_hands_dark | dark | 17.85 | 17.46 | 15.21 | 20.03 | 0.6126 | 0.7077 | 1.0 -> 3.0 | 0.0 -> 0.0 | 0/0 |
| d_hands_dark | blurred | 17.85 | 7.44 | 38.07 | 19.27 | 0.9986 | 0.6136 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |

## Stages that fell back

- adaptive_denoise: Denoising removed too little noise to justify changing the image.

## Identity, devices and memory

| Photo | Case | Identity | Similarity | Model stages (device) | Time (s) | GPU peak MB |
|---|---|---|---|---|---|---|
| a_portrait_lowkey | original | verified | 0.9891 | none | 16.0 | None |
| a_portrait_lowkey | dark | inconclusive | None | none | 17.82 | None |
| a_portrait_lowkey | blurred | verified | 0.9868 | none | 6.11 | None |
| b_portrait_harsh_sun | original | verified | 0.9848 | none | 7.62 | None |
| b_portrait_harsh_sun | dark | inconclusive | None | none | 22.34 | None |
| b_portrait_harsh_sun | blurred | verified | 0.9954 | none | 8.76 | None |
| c_portrait_studio | original | verified | 0.9883 | none | 3.97 | None |
| c_portrait_studio | dark | inconclusive | None | none | 28.11 | None |
| c_portrait_studio | blurred | verified | 0.9945 | none | 3.95 | None |
| d_hands_dark | original | inconclusive | None | none | 18.61 | None |
| d_hands_dark | dark | inconclusive | None | none | 17.46 | None |
| d_hands_dark | blurred | inconclusive | None | none | 7.44 | None |

Peak process memory: 2100 MB.
