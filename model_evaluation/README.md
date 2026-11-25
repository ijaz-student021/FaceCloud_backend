# Model evaluation notebooks

One notebook per model that FaceCloud AI runs, plus an overview that traces how the application uses them. Every number comes from running the application's own functions (`enhancement.*`, `validation.*`, `identities.*`, `search.*`) on the project's photographs; nothing is typed in by hand.

## What these notebooks are and are not
* They **evaluate pretrained models** (selection, benchmarking, integration). **No model running in the application was fine-tuned**; notebook 11 documents one small, real fine-tuning experiment (Zero-DCE) that was not adopted.
* Ground truth is constructed where possible (a sharp photo is blurred / noised / shrunk / softened, then restored and compared with the original), and for the rest the proxy used is stated in the notebook (for example search labels come from face counts and measured brightness).
* Figures only use the project's **public stock (Unsplash) photographs**; aggregate statistics (counts, score distributions) are read from the whole database.
* Results that are not flattering are reported as measured (Real-ESRGAN scores below bicubic on PSNR/SSIM, NAFNet-SIDD does not beat non-local means on heavy Gaussian noise, the "bright photo" search query fails on its proxy label).

## Scorecard (from `outputs/scorecard.json`)
| Model | Notebook | Task | Headline scores |
|---|---|---|---|
| YuNet | [01](notebooks/) | Face detection | recall vs clean reference: blur σ2 100%, noise σ25 50%, JPEG q15 88%; 8 ms/photo |
| SFace | [02](notebooks/) | Identity | ROC-AUC 0.997, EER 0.024; true match 96% / false match 0.0% at threshold 0.45; 98% of 139 stored enhancements pass the 0.80 identity gate |
| Zero-DCE | [03](notebooks/) | Low-light | brightness x3.0 (gamma baseline x2.1), noise amplification x2.6, 0.20 s |
| NAFNet GoPro | [04](notebooks/) | Motion deblur | PSNR 27.1 → 30.9 dB (unsharp mask 27.1), SSIM 0.81 → 0.90 |
| NAFNet SIDD | [05](notebooks/) | Denoise (off) | σ15: PSNR 25.6 → 32.2 dB (NLM 28.4); σ30: SIDD 20.6 vs NLM 22.9 (NLM better); real grain noise 14.83 → 1.48 |
| Real-ESRGAN | [06](notebooks/) | Upscale | PSNR bicubic 28.3 / compact 25.3 / x4plus 25.9 / as-used blend 28.1 dB; sharpness ratio bicubic 0.01 vs compact 0.21; compact 0.6 s vs x4plus 1.3 s |
| CodeFormer | [07](notebooks/) | Face restoration | identity 0.768 → 0.782, SSIM 0.829 → 0.839, 83% pass the 0.65 gate |
| DDColor | [08](notebooks/) | Colorization | chroma error 6.63 (gray) → 7.04; colorfulness 26 vs original 38 |
| SigLIP | [09](notebooks/) | Photo search | mean ROC-AUC 0.67, mean precision@10 0.65; at the app floor 0.06: precision 0.69, recall 0.37 (proxy labels) |
| Quality control | [10](notebooks/) | Classical CV | 306 photos scored; 2 rejected for blur; coarse-blur line 0.3 |

Evaluation metrics used: PSNR, SSIM, MAE, sharpness ratio, noise estimate, colorfulness, Lab chroma error, ROC-AUC, EER, true/false match rate, recall under degradation, average precision, precision@k, seconds per image.

## Files
* `notebooks/00_overview_and_integration.ipynb`: model inventory, engines run/accepted across all stored results, one result traced end to end (scores before, models run, scores after, identity gate), the full scorecard, and a summary.
* `notebooks/11_zero_dce_finetuning.ipynb` + `finetune/`: a real fine-tuning run of the official Zero-DCE weights on 35 low-light project photographs (30 train / 5 validation, 40 epochs, checkpoints `finetune/zero_dce/run_01/*.pt`, loss history, before/after evaluation). The private training photographs are not committed (`finetune/.gitignore`).
* `notebooks/01_...` to `10_...`: one per model (YuNet, SFace, Zero-DCE, NAFNet GoPro, NAFNet SIDD, Real-ESRGAN, CodeFormer, DDColor, SigLIP) and the classical quality-control thresholds.
* `outputs/`: every chart (PNG), per-notebook JSON, and `scorecard.json`.
* `common.py`: shared helpers and metric functions. `defs/` + `build_notebooks.py`: the notebooks are generated from these so they can be rebuilt.

## Re-running
```
cd <project root>
.venv/bin/pip install nbformat nbclient nbconvert ipykernel   # once
.venv/bin/python model_evaluation/build_notebooks.py --run     # rebuild and execute all (about 5 minutes on an Apple M1)
```
Or open any notebook in VS Code / Jupyter with the project's `.venv` as the kernel. Needs the model files in `.runtime/models` and the database.
