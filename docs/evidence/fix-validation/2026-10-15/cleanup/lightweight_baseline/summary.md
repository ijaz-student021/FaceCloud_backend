# Pipeline evaluation `2026-10-15-lightweight`

- Evidence: **`cpu_classical_only`** (OpenCV and Pillow only; no learned enhancement model exists)
- Pipeline version: staged_candidate_pipeline_v5

Level `auto`, style `natural`. Degradations are synthetic; PSNR and SSIM are measured against the clean photo, so a rise means the output moved toward it.

| Photo | Case | MP | Time (s) | PSNR before | PSNR after | SSIM before | SSIM after | Noise | Clipped % | Faces checked/adjusted |
|---|---|---|---|---|---|---|---|---|---|---|
| bright__13 | good | 0.48 | 0.25 | 99.0 | 49.34 | 1.0 | 0.9994 | 0.0 -> 0.0 | 58.994 -> 59.022 | 0/0 |
| bright__13 | dark | 0.48 | 0.38 | 4.1 | 4.08 | 0.5485 | 0.5412 | 1.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| bright__13 | noisy | 0.48 | 0.43 | 27.34 | 30.15 | 0.8988 | 0.9553 | 4.0 -> 1.0 | 41.342 -> 9.825 | 0/0 |
| bright__13 | blurred | 0.48 | 0.12 | 22.95 | 23.03 | 0.8792 | 0.885 | 0.0 -> 0.0 | 54.002 -> 53.196 | 0/0 |
| bright__13 | compressed | 0.48 | 0.12 | 29.96 | 29.85 | 0.9667 | 0.9661 | 0.0 -> 0.0 | 59.26 -> 59.273 | 0/0 |
| bright__13 | overexposed | 0.48 | 0.12 | 19.95 | 20.01 | 0.8395 | 0.8408 | 0.0 -> 0.0 | 93.358 -> 93.382 | 0/0 |
| bright__13 | grayscale | 0.48 | 0.12 | 99.0 | 50.85 | 1.0 | 0.9997 | 0.0 -> 0.0 | 58.994 -> 58.994 | 0/0 |
| bright__13 | group | 1.93 | 0.27 | 99.0 | 49.34 | 1.0 | 0.9996 | 0.0 -> 0.0 | 58.994 -> 59.022 | 0/0 |
| bright__13 | transparent | 0.48 | 0.13 | 5.53 | 5.53 | 0.6907 | 0.6907 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| clean_mid__01 | good | 0.79 | 0.18 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 1.603 -> 1.603 | 0/0 |
| clean_mid__01 | dark | 0.79 | 0.62 | 8.7 | 8.7 | 0.5226 | 0.5226 | 1.0 -> 1.0 | 0.0 -> 0.0 | 0/0 |
| clean_mid__01 | noisy | 0.79 | 0.59 | 25.3 | 28.17 | 0.85 | 0.9381 | 5.0 -> 3.0 | 0.901 -> 0.143 | 0/0 |
| clean_mid__01 | blurred | 0.79 | 0.2 | 31.98 | 32.13 | 0.963 | 0.9645 | 0.0 -> 0.0 | 1.465 -> 1.465 | 0/0 |
| clean_mid__01 | compressed | 0.79 | 0.17 | 31.94 | 31.94 | 0.9378 | 0.9378 | 0.0 -> 0.0 | 1.571 -> 1.571 | 0/0 |
| clean_mid__01 | overexposed | 0.79 | 0.19 | 10.01 | 10.1 | 0.7418 | 0.7436 | 0.0 -> 0.0 | 47.572 -> 47.65 | 0/0 |
| clean_mid__01 | grayscale | 0.79 | 0.19 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 1.603 -> 1.603 | 0/0 |
| clean_mid__01 | group | 3.15 | 0.44 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 1.603 -> 1.603 | 0/0 |
| clean_mid__01 | transparent | 0.79 | 0.18 | 10.14 | 10.14 | 0.6686 | 0.6686 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| clean_mid__02 | good | 1.05 | 0.27 | 99.0 | 99.0 | 1.0 | 1.0 | 1.0 -> 1.0 | 0.706 -> 0.706 | 0/0 |
| clean_mid__02 | dark | 1.05 | 0.74 | 7.47 | 7.47 | 0.4986 | 0.4986 | 1.0 -> 1.0 | 0.0 -> 0.0 | 0/0 |
| clean_mid__02 | noisy | 1.05 | 0.68 | 25.37 | 28.51 | 0.8814 | 0.9415 | 5.0 -> 2.0 | 0.656 -> 0.064 | 0/0 |
| clean_mid__02 | blurred | 1.05 | 0.26 | 28.47 | 28.63 | 0.9034 | 0.9081 | 0.0 -> 0.0 | 0.546 -> 0.546 | 0/0 |
| clean_mid__02 | compressed | 1.05 | 0.25 | 30.61 | 30.61 | 0.9209 | 0.9209 | 0.0 -> 0.0 | 0.706 -> 0.706 | 0/0 |
| clean_mid__02 | overexposed | 1.05 | 0.25 | 11.04 | 11.11 | 0.6936 | 0.6956 | 0.0 -> 0.0 | 54.741 -> 54.776 | 0/0 |
| clean_mid__02 | grayscale | 1.05 | 0.26 | 99.0 | 99.0 | 1.0 | 1.0 | 1.0 -> 1.0 | 0.706 -> 0.706 | 0/0 |
| clean_mid__02 | group | 4.19 | 0.51 | 99.0 | 99.0 | 1.0 | 1.0 | 1.0 -> 1.0 | 0.706 -> 0.706 | 0/0 |
| clean_mid__02 | transparent | 1.05 | 0.27 | 8.9 | 8.9 | 0.6476 | 0.6476 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| jpeg_artifacts__01 | good | 0.79 | 0.18 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 1.578 -> 1.578 | 0/0 |
| jpeg_artifacts__01 | dark | 0.79 | 0.58 | 8.7 | 8.7 | 0.5212 | 0.5212 | 1.0 -> 1.0 | 0.0 -> 0.0 | 0/0 |
| jpeg_artifacts__01 | noisy | 0.79 | 0.56 | 25.3 | 28.15 | 0.8521 | 0.9376 | 5.0 -> 3.0 | 0.895 -> 0.131 | 0/0 |
| jpeg_artifacts__01 | blurred | 0.79 | 0.19 | 31.98 | 32.12 | 0.959 | 0.9606 | 0.0 -> 0.0 | 1.442 -> 1.442 | 0/0 |
| jpeg_artifacts__01 | compressed | 0.79 | 0.18 | 31.72 | 31.72 | 0.935 | 0.935 | 0.0 -> 0.0 | 1.563 -> 1.563 | 0/0 |
| jpeg_artifacts__01 | overexposed | 0.79 | 0.17 | 10.02 | 10.1 | 0.7396 | 0.7413 | 0.0 -> 0.0 | 47.602 -> 47.684 | 0/0 |
| jpeg_artifacts__01 | grayscale | 0.79 | 0.17 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 1.578 -> 1.578 | 0/0 |
| jpeg_artifacts__01 | group | 3.15 | 0.4 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 1.578 -> 1.578 | 0/0 |
| jpeg_artifacts__01 | transparent | 0.79 | 0.18 | 10.13 | 10.13 | 0.668 | 0.668 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| low_key__05 | good | 0.7 | 0.19 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.075 -> 0.075 | 0/0 |
| low_key__05 | dark | 0.7 | 0.55 | 12.46 | 12.46 | 0.4862 | 0.4862 | 1.0 -> 1.0 | 0.0 -> 0.0 | 0/0 |
| low_key__05 | noisy | 0.7 | 0.59 | 25.82 | 27.17 | 0.9068 | 0.9329 | 6.0 -> 4.0 | 0.413 -> 0.386 | 0/0 |
| low_key__05 | blurred | 0.7 | 0.17 | 20.84 | 20.88 | 0.8173 | 0.8217 | 0.0 -> 0.0 | 0.0 -> 0.0 | 1/0 |
| low_key__05 | compressed | 0.7 | 0.18 | 26.67 | 26.67 | 0.9412 | 0.9412 | 0.0 -> 0.0 | 0.68 -> 0.68 | 0/0 |
| low_key__05 | overexposed | 0.7 | 0.19 | 12.38 | 12.55 | 0.6677 | 0.6747 | 1.0 -> 1.0 | 14.74 -> 14.813 | 0/0 |
| low_key__05 | grayscale | 0.7 | 0.19 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.075 -> 0.075 | 0/0 |
| low_key__05 | group | 2.8 | 0.32 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.075 -> 0.075 | 0/0 |
| low_key__05 | transparent | 0.7 | 0.22 | 13.89 | 13.89 | 0.6313 | 0.6313 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| textured__09 | good | 0.7 | 0.19 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.081 -> 0.081 | 0/0 |
| textured__09 | dark | 0.7 | 0.52 | 5.83 | 5.81 | 0.4804 | 0.4781 | 1.0 -> 1.0 | 0.0 -> 0.0 | 0/0 |
| textured__09 | noisy | 0.7 | 0.53 | 25.71 | 28.6 | 0.9076 | 0.9757 | 6.0 -> 2.0 | 3.111 -> 0.617 | 0/0 |
| textured__09 | blurred | 0.7 | 0.17 | 20.04 | 20.09 | 0.7841 | 0.7888 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |
| textured__09 | compressed | 0.7 | 0.19 | 24.76 | 24.76 | 0.9594 | 0.9594 | 1.0 -> 1.0 | 1.823 -> 1.823 | 0/0 |
| textured__09 | overexposed | 0.7 | 0.19 | 13.9 | 14.02 | 0.8185 | 0.8222 | 0.0 -> 0.0 | 71.017 -> 71.045 | 0/0 |
| textured__09 | grayscale | 0.7 | 0.19 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.081 -> 0.081 | 0/0 |
| textured__09 | group | 2.79 | 0.32 | 99.0 | 99.0 | 1.0 | 1.0 | 0.0 -> 0.0 | 0.081 -> 0.081 | 0/0 |
| textured__09 | transparent | 0.7 | 0.18 | 7.27 | 7.27 | 0.6342 | 0.6342 | 0.0 -> 0.0 | 0.0 -> 0.0 | 0/0 |

## Stages that fell back

- adaptive_denoise: Denoising removed too little noise to justify changing the image.

## Identity and time

| Photo | Case | Identity | Similarity | Time (s) |
|---|---|---|---|---|
| bright__13 | good | inconclusive | None | 0.25 |
| bright__13 | dark | inconclusive | None | 0.38 |
| bright__13 | noisy | inconclusive | None | 0.43 |
| bright__13 | blurred | inconclusive | None | 0.12 |
| bright__13 | compressed | inconclusive | None | 0.12 |
| bright__13 | overexposed | inconclusive | None | 0.12 |
| bright__13 | grayscale | inconclusive | None | 0.12 |
| bright__13 | group | inconclusive | None | 0.27 |
| bright__13 | transparent | inconclusive | None | 0.13 |
| clean_mid__01 | good | inconclusive | None | 0.18 |
| clean_mid__01 | dark | inconclusive | None | 0.62 |
| clean_mid__01 | noisy | inconclusive | None | 0.59 |
| clean_mid__01 | blurred | inconclusive | None | 0.2 |
| clean_mid__01 | compressed | inconclusive | None | 0.17 |
| clean_mid__01 | overexposed | inconclusive | None | 0.19 |
| clean_mid__01 | grayscale | inconclusive | None | 0.19 |
| clean_mid__01 | group | inconclusive | None | 0.44 |
| clean_mid__01 | transparent | inconclusive | None | 0.18 |
| clean_mid__02 | good | inconclusive | None | 0.27 |
| clean_mid__02 | dark | inconclusive | None | 0.74 |
| clean_mid__02 | noisy | inconclusive | None | 0.68 |
| clean_mid__02 | blurred | inconclusive | None | 0.26 |
| clean_mid__02 | compressed | inconclusive | None | 0.25 |
| clean_mid__02 | overexposed | inconclusive | None | 0.25 |
| clean_mid__02 | grayscale | inconclusive | None | 0.26 |
| clean_mid__02 | group | inconclusive | None | 0.51 |
| clean_mid__02 | transparent | inconclusive | None | 0.27 |
| jpeg_artifacts__01 | good | inconclusive | None | 0.18 |
| jpeg_artifacts__01 | dark | inconclusive | None | 0.58 |
| jpeg_artifacts__01 | noisy | inconclusive | None | 0.56 |
| jpeg_artifacts__01 | blurred | inconclusive | None | 0.19 |
| jpeg_artifacts__01 | compressed | inconclusive | None | 0.18 |
| jpeg_artifacts__01 | overexposed | inconclusive | None | 0.17 |
| jpeg_artifacts__01 | grayscale | inconclusive | None | 0.17 |
| jpeg_artifacts__01 | group | inconclusive | None | 0.4 |
| jpeg_artifacts__01 | transparent | inconclusive | None | 0.18 |
| low_key__05 | good | inconclusive | None | 0.19 |
| low_key__05 | dark | inconclusive | None | 0.55 |
| low_key__05 | noisy | inconclusive | None | 0.59 |
| low_key__05 | blurred | inconclusive | None | 0.17 |
| low_key__05 | compressed | inconclusive | None | 0.18 |
| low_key__05 | overexposed | inconclusive | None | 0.19 |
| low_key__05 | grayscale | inconclusive | None | 0.19 |
| low_key__05 | group | inconclusive | None | 0.32 |
| low_key__05 | transparent | inconclusive | None | 0.22 |
| textured__09 | good | inconclusive | None | 0.19 |
| textured__09 | dark | inconclusive | None | 0.52 |
| textured__09 | noisy | inconclusive | None | 0.53 |
| textured__09 | blurred | inconclusive | None | 0.17 |
| textured__09 | compressed | inconclusive | None | 0.19 |
| textured__09 | overexposed | inconclusive | None | 0.19 |
| textured__09 | grayscale | inconclusive | None | 0.19 |
| textured__09 | group | inconclusive | None | 0.32 |
| textured__09 | transparent | inconclusive | None | 0.18 |

Peak process memory: 528 MB.
