# Private FaceCloud Model Training

## Purpose

The trainer creates a private FaceCloud curve-estimation model from unpaired
low-light images. It needs no normal-light target images. DARK FACE annotation
text files are for face detection and are not consumed by this enhancer.

## Dataset

```text
FaceCloud_dataset/
├── image/    # 6,000 PNG images used for training
└── label/    # detection annotations, unused by this trainer
```

A deterministic 90/10 split is saved in `split.json` for reproducibility.

## Windows smoke test

```powershell
.\venv\Scripts\Activate.ps1
pip install -r requirements-training.txt

python scripts\train_facecloud_curve_model.py `
  --dataset "C:\Users\csbah\Downloads\FaceCloud_dataset" `
  --output-dir ".runtime\training\facecloud_curve_smoke" `
  --image-size 128 `
  --batch-size 2 `
  --epochs 1 `
  --workers 0
```

This checks the pipeline. Full training should use a GPU.

## Full Google Colab training

Open `notebooks/FaceCloud_Private_Model_Training_Colab.ipynb` in Colab for the
recommended guided workflow. It detects `facecloud_backend/Dataset` from common
Google Drive or `/content` locations, verifies the GPU and data, resumes saved
checkpoints, plots losses, and validates the exported ONNX model.

The equivalent direct command is:

```bash
python scripts/train_facecloud_curve_model.py \
  --dataset /content/FaceCloud_dataset \
  --output-dir /content/drive/MyDrive/FaceCloud/private-model-v1 \
  --image-size 256 \
  --batch-size 16 \
  --epochs 100 \
  --workers 2
```

Reduce the batch size after a GPU out-of-memory error. Resume with:

```bash
python scripts/train_facecloud_curve_model.py \
  --dataset /content/FaceCloud_dataset \
  --output-dir /content/drive/MyDrive/FaceCloud/private-model-v1 \
  --epochs 100 --workers 2 \
  --resume /content/drive/MyDrive/FaceCloud/private-model-v1/last.pt
```

## Outputs

- `best.pt`: best validation checkpoint.
- `last.pt`: recovery checkpoint.
- `facecloud_curve_best.onnx`: deployment model returning 24 curve maps.
- `config.json`: training configuration.
- `split.json`: exact train and validation sets.
- `history.jsonl`: epoch metrics.

> **2026-10-15:** the application no longer loads any enhancement model
> (`enhancement/zero_dce.py` and the curve-model path were retired; see
> `docs/enhancement/retired-capabilities.md`). This page and its training script are
> kept as the record of the private curve-model *training* work, which runs in
> Colab; nothing in the backend consumes its output today.

The ONNX output was compatible with the eight-iteration curve application of the
removed `enhancement/zero_dce.py`.

## Commercial controls

- Retain explicit written permission for private commercial SaaS usage.
- Never commit the dataset or model weights to a public repository.
- Keep production weights in private access-controlled storage.
- Record the dataset version, code revision, split and evaluation results.
