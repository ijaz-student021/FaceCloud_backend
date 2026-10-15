# Model sheet: every model on real photographs, 2026-10-15

`scripts/build_model_sheet.py` on 3 of the project's phone photographs (640 px long edge): one defect applied to each, the matching model
repairs it, scored against the clean photograph. Apple M1 (8 GiB, under memory pressure: times include model loads and swapping),
one model resident at a time. **0 of 8 models skipped.** PSNR in dB, SSIM 0-1. The photographs are private and are not in the repository.

| Defect | Photo | Input PSNR/SSIM | Model(s) | PSNR / SSIM | Seconds |
|---|---|---|---|---|---|
| lowlight | p1 | 12.68 / 0.408 | Retinexformer 16.18 / 0.783; Zero-DCE 22.82 / 0.804; OpenCV gamma 18.28 / 0.842 | | 70.49; 0.84; 0.26 |
| noise | p1 | 25.43 / 0.742 | NAFNet-SIDD 30.12 / 0.896; OpenCV NLM 30.75 / 0.892 | | 9.3; 0.28 |
| blur | p1 | 33.20 / 0.924 | NAFNet-GoPro 40.81 / 0.973; OpenCV unsharp 33.19 / 0.923 | | 11.75; 0.01 |
| lowres | p1 | 36.84 / 0.954 | Real-ESRGAN 34.09 / 0.936 | | 1.36 |
| lowlight | p2 | 7.36 / 0.378 | Retinexformer 14.69 / 0.814; Zero-DCE 14.93 / 0.821; OpenCV gamma 10.95 / 0.751 | | 1.85; 0.4; 0.01 |
| noise | p2 | 25.40 / 0.795 | NAFNet-SIDD 30.01 / 0.914; OpenCV NLM 31.61 / 0.903 | | 3.96; 0.25 |
| blur | p2 | 26.17 / 0.803 | NAFNet-GoPro 36.19 / 0.975; OpenCV unsharp 26.16 / 0.803 | | 3.12; 0.0 |
| lowres | p2 | 31.31 / 0.930 | Real-ESRGAN 28.44 / 0.884 | | 1.03 |
| lowlight | p3 | 8.86 / 0.337 | Retinexformer 18.15 / 0.793; Zero-DCE 18.18 / 0.833; OpenCV gamma 13.10 / 0.731 | | 1.05; 0.23; 0.0 |
| noise | p3 | 25.29 / 0.845 | NAFNet-SIDD 27.51 / 0.901; OpenCV NLM 29.31 / 0.896 | | 4.2; 0.27 |
| blur | p3 | 24.76 / 0.780 | NAFNet-GoPro 30.28 / 0.944; OpenCV unsharp 24.62 / 0.782 | | 3.15; 0.0 |
| lowres | p3 | 25.65 / 0.832 | Real-ESRGAN 23.98 / 0.808 | | 1.01 |

## Face restoration (one close portrait; similarity to the clean face, 1.0 = identical)

| Candidate | PSNR | SFace | ArcFace |
|---|---|---|---|
| degraded face (blur + JPEG q25) | 26.64 | 0.581 | 0.5868 |
| CodeFormer w=0.7 | 26.88 | 0.6585 | 0.5151 |
| CodeFormer w=0.3 | 26.82 | 0.6533 | 0.4805 |
| GFPGAN | 26.41 | 0.5381 | 0.4293 |

## Reading it, without flattering the models

* **Deblur is the clear win**: NAFNet-GoPro +5.5 to +10 dB over the blurred input; the OpenCV unsharp mask changes nothing.
* **Low light**: Retinexformer and Zero-DCE both lift (PSNR +3 to +9 dB, SSIM 0.34-0.41 -> 0.78-0.83). Zero-DCE scores equal or higher on PSNR because Retinexformer brightens beyond the original exposure; neither beats the other on every photograph. The engine therefore prefers the smaller lift when both pass.
* **Denoise** (synthetic Gaussian noise, sigma 14): NAFNet-SIDD +2 to +5 dB, but classical non-local means is within 1.5 dB and sometimes better. SIDD is trained on *real* sensor noise, which synthetic noise is not; no real noisy/clean pairs were available, so the real-noise advantage is unmeasured here.
* **Super-resolution**: Real-ESRGAN scores *lower* than plain bicubic on PSNR and SSIM (-1 to -3 dB), as GAN upscalers do: it invents texture. PSNR cannot say whether it looks better; the engine only uses it when the photograph is genuinely small and a consistency check passes.
* **Face restoration and identity** (one portrait, so only an illustration): SFace says CodeFormer raises similarity to the clean face (0.58 -> 0.66) and GFPGAN lowers it (-> 0.54); ArcFace says both lower it (0.59 -> 0.52 and 0.43). The two identity models disagree about restoration, which is why the engine gates it and why one embedding model is not proof.
