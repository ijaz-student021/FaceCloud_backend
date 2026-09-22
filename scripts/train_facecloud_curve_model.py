"""Train a private FaceCloud curve model on unpaired low-light images."""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image, ImageFile
from torch.utils.data import DataLoader, Dataset

ImageFile.LOAD_TRUNCATED_IMAGES = False
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass(frozen=True)
class TrainingConfig:
    dataset: str
    output_dir: str
    image_size: int
    batch_size: int
    epochs: int
    learning_rate: float
    validation_fraction: float
    seed: int
    workers: int
    exposure_target: float


class LowLightDataset(Dataset):
    """Load RGB crops without changing the source dataset."""

    def __init__(self, files: list[Path], image_size: int, training: bool) -> None:
        if not files:
            raise ValueError("No supported images were found in the dataset.")
        self.files = files
        self.image_size = image_size
        self.training = training

    def __len__(self) -> int:
        return len(self.files)

    def __getitem__(self, index: int) -> torch.Tensor:
        path = self.files[index]
        try:
            with Image.open(path) as source:
                image = source.convert("RGB")
                width, height = image.size
                scale = self.image_size / min(width, height)
                image = image.resize(
                    (max(self.image_size, round(width * scale)),
                     max(self.image_size, round(height * scale))),
                    Image.Resampling.LANCZOS,
                )
                left = (image.width - self.image_size) // 2
                top = (image.height - self.image_size) // 2
                if self.training:
                    left = random.randint(0, image.width - self.image_size)
                    top = random.randint(0, image.height - self.image_size)
                image = image.crop((left, top, left + self.image_size, top + self.image_size))
                if self.training and random.random() < 0.5:
                    image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                array = np.asarray(image, dtype=np.float32) / 255.0
        except (OSError, ValueError) as error:
            raise RuntimeError(f"Could not decode training image: {path}") from error
        return torch.from_numpy(array).permute(2, 0, 1).contiguous()


class FaceCloudCurveNet(nn.Module):
    """Predict eight RGB adjustment curves while preserving input resolution."""

    def __init__(self, features: int = 32) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(3, features, 3, padding=1)
        self.conv2 = nn.Conv2d(features, features, 3, padding=1)
        self.conv3 = nn.Conv2d(features, features, 3, padding=1)
        self.conv4 = nn.Conv2d(features, features, 3, padding=1)
        self.conv5 = nn.Conv2d(features * 2, features, 3, padding=1)
        self.conv6 = nn.Conv2d(features * 2, features, 3, padding=1)
        self.curves = nn.Conv2d(features * 2, 24, 3, padding=1)

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        x1 = F.relu(self.conv1(image), inplace=True)
        x2 = F.relu(self.conv2(x1), inplace=True)
        x3 = F.relu(self.conv3(x2), inplace=True)
        x4 = F.relu(self.conv4(x3), inplace=True)
        x5 = F.relu(self.conv5(torch.cat((x3, x4), dim=1)), inplace=True)
        x6 = F.relu(self.conv6(torch.cat((x2, x5), dim=1)), inplace=True)
        return torch.tanh(self.curves(torch.cat((x1, x6), dim=1)))


def apply_curves(image: torch.Tensor, curves: torch.Tensor) -> torch.Tensor:
    output = image
    for curve in curves.chunk(8, dim=1):
        output = (output + curve * (output.square() - output)).clamp(0.0, 1.0)
    return output


def color_constancy_loss(image: torch.Tensor) -> torch.Tensor:
    red, green, blue = image.mean(dim=(2, 3)).unbind(dim=1)
    return ((red - green).square() + (red - blue).square() +
            (green - blue).square()).mean()


def exposure_loss(image: torch.Tensor, target: float) -> torch.Tensor:
    patches = F.avg_pool2d(image.mean(dim=1, keepdim=True), 16, 16)
    return (patches - target).square().mean()


def spatial_consistency_loss(original: torch.Tensor, enhanced: torch.Tensor) -> torch.Tensor:
    original = F.avg_pool2d(original.mean(dim=1, keepdim=True), 4, 4)
    enhanced = F.avg_pool2d(enhanced.mean(dim=1, keepdim=True), 4, 4)
    losses = []
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        original_delta = original - torch.roll(original, (dy, dx), dims=(2, 3))
        enhanced_delta = enhanced - torch.roll(enhanced, (dy, dx), dims=(2, 3))
        losses.append((original_delta - enhanced_delta).square().mean())
    return torch.stack(losses).mean()


def illumination_smoothness_loss(curves: torch.Tensor) -> torch.Tensor:
    horizontal = (curves[:, :, :, 1:] - curves[:, :, :, :-1]).square().mean()
    vertical = (curves[:, :, 1:, :] - curves[:, :, :-1, :]).square().mean()
    return horizontal + vertical


def gradient_preservation_loss(original: torch.Tensor, enhanced: torch.Tensor) -> torch.Tensor:
    original = original.mean(dim=1, keepdim=True)
    enhanced = enhanced.mean(dim=1, keepdim=True)
    return (F.l1_loss(enhanced[:, :, :, 1:] - enhanced[:, :, :, :-1],
                      original[:, :, :, 1:] - original[:, :, :, :-1]) +
            F.l1_loss(enhanced[:, :, 1:, :] - enhanced[:, :, :-1, :],
                      original[:, :, 1:, :] - original[:, :, :-1, :]))


def objective(original: torch.Tensor, curves: torch.Tensor, exposure_target: float):
    enhanced = apply_curves(original, curves)
    parts = {
        "spatial": spatial_consistency_loss(original, enhanced),
        "exposure": exposure_loss(enhanced, exposure_target),
        "color": color_constancy_loss(enhanced),
        "smoothness": illumination_smoothness_loss(curves),
        "detail": gradient_preservation_loss(original, enhanced),
    }
    total = (parts["spatial"] + 10.0 * parts["exposure"] +
             5.0 * parts["color"] + 200.0 * parts["smoothness"] +
             2.0 * parts["detail"])
    return total, parts


def discover_images(dataset: Path) -> list[Path]:
    image_root = dataset / "image"
    search_root = image_root if image_root.is_dir() else dataset
    return sorted(path for path in search_root.rglob("*")
                  if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)


def split_files(files: list[Path], validation_fraction: float, seed: int):
    shuffled = files.copy()
    random.Random(seed).shuffle(shuffled)
    validation_count = max(1, round(len(shuffled) * validation_fraction))
    return shuffled[validation_count:], shuffled[:validation_count]


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def run_epoch(model, loader, device, exposure_target, optimizer=None):
    training = optimizer is not None
    model.train(training)
    totals = {key: 0.0 for key in ("loss", "spatial", "exposure", "color", "smoothness", "detail")}
    samples = 0
    context = torch.enable_grad() if training else torch.inference_mode()
    with context:
        for images in loader:
            images = images.to(device, non_blocking=True)
            if training:
                optimizer.zero_grad(set_to_none=True)
            curves = model(images)
            loss, parts = objective(images, curves, exposure_target)
            if not torch.isfinite(loss):
                raise RuntimeError("Training produced a non-finite loss.")
            if training:
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
                optimizer.step()
            count = images.shape[0]
            totals["loss"] += loss.item() * count
            for key, value in parts.items():
                totals[key] += value.item() * count
            samples += count
    return {key: value / samples for key, value in totals.items()}


def save_checkpoint(path, model, optimizer, scheduler, epoch, best_loss, config):
    torch.save({"format": "facecloud_curve_net_v1", "epoch": epoch,
                "best_validation_loss": best_loss, "model_state": model.state_dict(),
                "optimizer_state": optimizer.state_dict(),
                "scheduler_state": scheduler.state_dict(), "config": asdict(config)}, path)


def export_onnx(model: nn.Module, path: Path, image_size: int) -> None:
    model = model.to("cpu").eval()
    example = torch.rand(1, 3, image_size, image_size)
    torch.onnx.export(
        model, example, path, input_names=["image"], output_names=["curves"],
        dynamic_axes={"image": {0: "batch", 2: "height", 3: "width"},
                      "curves": {0: "batch", 2: "height", 3: "width"}},
        opset_version=17,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("training_outputs/facecloud_curve"))
    parser.add_argument("--image-size", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--validation-fraction", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--exposure-target", type=float, default=0.60)
    parser.add_argument("--resume", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.dataset.is_dir():
        raise SystemExit(f"Dataset directory does not exist: {args.dataset}")
    if args.image_size < 64 or args.image_size % 16:
        raise SystemExit("--image-size must be at least 64 and divisible by 16.")
    if not 0.01 <= args.validation_fraction <= 0.5:
        raise SystemExit("--validation-fraction must be between 0.01 and 0.5.")
    set_seed(args.seed)
    files = discover_images(args.dataset)
    train_files, validation_files = split_files(files, args.validation_fraction, args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    config = TrainingConfig(str(args.dataset.resolve()), str(args.output_dir.resolve()),
                            args.image_size, args.batch_size, args.epochs, args.learning_rate,
                            args.validation_fraction, args.seed, args.workers, args.exposure_target)
    (args.output_dir / "config.json").write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
    (args.output_dir / "split.json").write_text(json.dumps({
        "train": [str(path.relative_to(args.dataset)) for path in train_files],
        "validation": [str(path.relative_to(args.dataset)) for path in validation_files],
    }, indent=2), encoding="utf-8")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    train_loader = DataLoader(LowLightDataset(train_files, args.image_size, True),
                              args.batch_size, shuffle=True, num_workers=args.workers,
                              pin_memory=device.type == "cuda", persistent_workers=args.workers > 0)
    validation_loader = DataLoader(LowLightDataset(validation_files, args.image_size, False),
                                   args.batch_size, shuffle=False, num_workers=args.workers,
                                   pin_memory=device.type == "cuda", persistent_workers=args.workers > 0)
    model = FaceCloudCurveNet().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate, weight_decay=1e-6)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, args.epochs))
    start_epoch, best_loss = 1, math.inf
    if args.resume:
        checkpoint = torch.load(args.resume, map_location=device, weights_only=True)
        model.load_state_dict(checkpoint["model_state"])
        optimizer.load_state_dict(checkpoint["optimizer_state"])
        scheduler.load_state_dict(checkpoint["scheduler_state"])
        start_epoch = int(checkpoint["epoch"]) + 1
        best_loss = float(checkpoint["best_validation_loss"])

    print(f"Device: {device}; images: {len(files)} ({len(train_files)} train, {len(validation_files)} validation)")
    history_path = args.output_dir / "history.jsonl"
    for epoch in range(start_epoch, args.epochs + 1):
        started = time.perf_counter()
        train_metrics = run_epoch(model, train_loader, device, args.exposure_target, optimizer)
        validation_metrics = run_epoch(model, validation_loader, device, args.exposure_target)
        scheduler.step()
        record = {"epoch": epoch, "seconds": round(time.perf_counter() - started, 2),
                  "learning_rate": optimizer.param_groups[0]["lr"],
                  "train": train_metrics, "validation": validation_metrics}
        with history_path.open("a", encoding="utf-8") as history:
            history.write(json.dumps(record) + "\n")
        improved = validation_metrics["loss"] < best_loss
        best_loss = min(best_loss, validation_metrics["loss"])
        save_checkpoint(args.output_dir / "last.pt", model, optimizer, scheduler,
                        epoch, best_loss, config)
        if improved:
            save_checkpoint(args.output_dir / "best.pt", model, optimizer, scheduler,
                            epoch, best_loss, config)
        print(f"Epoch {epoch:03d}/{args.epochs}: train={train_metrics['loss']:.5f} "
              f"val={validation_metrics['loss']:.5f} best={best_loss:.5f} "
              f"time={record['seconds']:.1f}s")

    best = torch.load(args.output_dir / "best.pt", map_location="cpu", weights_only=True)
    model.load_state_dict(best["model_state"])
    export_onnx(model, args.output_dir / "facecloud_curve_best.onnx", args.image_size)
    print(f"Training complete: {args.output_dir / 'facecloud_curve_best.onnx'}")


if __name__ == "__main__":
    main()
