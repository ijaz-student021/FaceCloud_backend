"""Fine-tune the official Zero-DCE (Epoch99) weights on the project's own low-light photographs.

Real training, nothing simulated: the project's trainer (scripts/train_facecloud_curve_model.py: zero-reference losses, train/validation split, checkpoints, ONNX export) is
reused, but the network starts from the official Epoch99 weights instead of random ones. The result is a candidate model; it is NOT installed in the application.
    python model_evaluation/finetune/finetune_zero_dce.py [--epochs 40] [--lr 5e-5]
"""
import argparse
import importlib.util
import json
import math
import sys
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
import torch
from torch.utils.data import DataLoader

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
spec = importlib.util.spec_from_file_location("trainer", ROOT / "scripts" / "train_facecloud_curve_model.py")
trainer = importlib.util.module_from_spec(spec)
sys.modules["trainer"] = trainer          # dataclasses look the module up by name
spec.loader.exec_module(trainer)

OFFICIAL = Path.home() / "FaceCloud_retired_2026-10-15" / "provenance" / "Epoch99_official.pth"
ONNX = ROOT / ".runtime" / "models" / "zero_dce_epoch99.onnx"
MAPPING = {f"e_conv{i}": f"conv{i}" for i in range(1, 7)} | {"e_conv7": "curves"}


def load_official(model):
    state = torch.load(OFFICIAL, map_location="cpu", weights_only=True)
    renamed = {}
    for key, value in state.items():
        layer, kind = key.rsplit(".", 1)
        renamed[f"{MAPPING[layer]}.{kind}"] = value
    model.load_state_dict(renamed)
    return model


def verify_against_onnx(model):
    """The loaded network must reproduce the ONNX file the application runs (same weights, same output)."""
    rng = np.random.default_rng(0)
    sample = rng.random((1, 3, 128, 160)).astype(np.float32) * 0.5
    net = cv2.dnn.readNetFromONNX(str(ONNX)); net.setInput(sample); reference = net.forward()
    with torch.inference_mode():
        ours = model.cpu().eval()(torch.from_numpy(sample)).numpy()
    return float(np.abs(ours - reference).max()), ours.shape == reference.shape


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--image-size", type=int, default=256)
    parser.add_argument("--validation-fraction", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--save-every", type=int, default=10)
    parser.add_argument("--out", type=Path, default=HERE / "zero_dce" / "run_01")
    args = parser.parse_args()
    dataset = HERE / "zero_dce" / "dataset"
    out = args.out; out.mkdir(parents=True, exist_ok=True)
    trainer.set_seed(args.seed)
    files = trainer.discover_images(dataset)
    train_files, validation_files = trainer.split_files(files, args.validation_fraction, args.seed)
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    config = trainer.TrainingConfig(str(dataset), str(out), args.image_size, args.batch_size, args.epochs, args.lr, args.validation_fraction, args.seed, 0, 0.60)
    (out / "config.json").write_text(json.dumps(asdict(config) | {"initial_weights": "official Zero-DCE Epoch99 (Li et al., TPAMI 2021)", "device": str(device), "dataset_name": "FaceCloud low-light photographs (project library)"}, indent=2))
    (out / "split.json").write_text(json.dumps({"train": [p.name for p in train_files], "validation": [p.name for p in validation_files]}, indent=2))

    model = load_official(trainer.FaceCloudCurveNet())
    max_diff, same_shape = verify_against_onnx(model)
    print(f"official weights loaded; max difference from the ONNX the application runs: {max_diff:.2e} (shape match {same_shape})")
    assert max_diff < 1e-3 and same_shape, "weights do not reproduce the ONNX model"
    model = model.to(device)
    train_loader = DataLoader(trainer.LowLightDataset(train_files, args.image_size, True), args.batch_size, shuffle=True)
    validation_loader = DataLoader(trainer.LowLightDataset(validation_files, args.image_size, False), args.batch_size, shuffle=False)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=1e-6)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, args.epochs))
    history = out / "history.jsonl"; history.write_text("")
    # epoch 0 = the official weights before any training: the honest baseline every later epoch is compared with
    base_train = trainer.run_epoch(model, train_loader, device, 0.60); base_val = trainer.run_epoch(model, validation_loader, device, 0.60)
    history.open("a").write(json.dumps({"epoch": 0, "seconds": 0, "learning_rate": args.lr, "train": base_train, "validation": base_val}) + "\n")
    print(f"images {len(files)} ({len(train_files)} train / {len(validation_files)} validation) on {device}")
    print(f"epoch 000 (official weights, no training): train={base_train['loss']:.5f} val={base_val['loss']:.5f}")
    best = base_val["loss"]
    trainer.save_checkpoint(out / "epoch_000_official.pt", model, optimizer, scheduler, 0, best, config)
    for epoch in range(1, args.epochs + 1):
        started = time.perf_counter()
        train_m = trainer.run_epoch(model, train_loader, device, 0.60, optimizer); val_m = trainer.run_epoch(model, validation_loader, device, 0.60)
        scheduler.step()
        record = {"epoch": epoch, "seconds": round(time.perf_counter() - started, 2), "learning_rate": optimizer.param_groups[0]["lr"], "train": train_m, "validation": val_m}
        history.open("a").write(json.dumps(record) + "\n")
        improved = val_m["loss"] < best
        best = min(best, val_m["loss"])
        trainer.save_checkpoint(out / "last.pt", model, optimizer, scheduler, epoch, best, config)
        if improved:
            trainer.save_checkpoint(out / "best.pt", model, optimizer, scheduler, epoch, best, config)
        if epoch % args.save_every == 0:
            trainer.save_checkpoint(out / f"epoch_{epoch:03d}.pt", model, optimizer, scheduler, epoch, best, config)
        print(f"epoch {epoch:03d}/{args.epochs}: train={train_m['loss']:.5f} val={val_m['loss']:.5f} best={best:.5f} {record['seconds']:.1f}s{'  *' if improved else ''}")
    if (out / "best.pt").exists():
        saved = torch.load(out / "best.pt", map_location="cpu", weights_only=True); model.load_state_dict(saved["model_state"])
        trainer.export_onnx(model, out / "facecloud_zero_dce_finetuned_best.onnx", args.image_size)
        print("best epoch", saved["epoch"], "-> exported", out / "facecloud_zero_dce_finetuned_best.onnx")
    else:
        print("no epoch improved on the official weights' validation loss: no model exported")


if __name__ == "__main__":
    main()
