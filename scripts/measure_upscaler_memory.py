"""Measure time and peak memory of the Real-ESRGAN tiling paths on this machine.

Builds the real RRDBNet architecture with *random* weights (no checkpoint needed),
so the numbers describe memory and speed, never image quality.

    python scripts/measure_upscaler_memory.py --megapixels 1.0 --mode output_scale
    python scripts/measure_upscaler_memory.py --megapixels 1.0 --mode native

``native`` is the previous behaviour: accumulate the full 4x result, then reduce.
``output_scale`` reduces each tile first (what the pipeline does now). Run each
mode in its own process: peak memory is a per-process high-water mark.

``--stub-model`` swaps the network for a trivial 4x upscaler. That isolates the
memory of the tiling buffers, which is what differs between the two modes, and
makes large inputs quick to measure.
"""
import argparse
import os
import resource
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--megapixels", type=float, default=1.0)
    parser.add_argument("--mode", choices=("native", "output_scale"), default="output_scale")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--tile", type=int, default=256)
    parser.add_argument("--overlap", type=int, default=24)
    parser.add_argument("--stub-model", action="store_true")
    arguments = parser.parse_args()

    import django

    django.setup()
    import cv2
    import numpy as np
    import torch
    from torch import nn
    from torch.nn import functional

    from enhancement import runtime
    from enhancement.hd_restoration import _build_realesrgan

    device = torch.device(runtime.auto_device(torch) if arguments.device == "auto" else arguments.device)
    height = int((arguments.megapixels * 1e6 * 3 / 4) ** 0.5)
    width = int(height * 4 / 3)
    image = np.random.default_rng(1).integers(0, 256, (height, width, 3), dtype=np.uint8)
    if arguments.stub_model:
        class Stub(nn.Module):
            def forward(self, value):
                return functional.interpolate(value, scale_factor=4, mode="nearest")

        model = Stub().eval().to(device)
    else:
        model = _build_realesrgan(nn, functional).eval().to(device)

    tracemalloc.start()
    started = time.perf_counter()
    if arguments.mode == "native":
        native = runtime.tiled_inference(torch, image, model, device, 4, arguments.tile, arguments.overlap)
        output = cv2.resize(native, (width * 2, height * 2), interpolation=cv2.INTER_AREA)
    else:
        output = runtime.tiled_inference(
            torch, image, model, device, 4, arguments.tile, arguments.overlap, output_scale=2,
        )
    elapsed = time.perf_counter() - started
    buffers_mb = tracemalloc.get_traced_memory()[1] / (1024 * 1024)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    device_mb = None
    if device.type == "cuda":
        device_mb = torch.cuda.max_memory_allocated() / (1024 * 1024)
    elif device.type == "mps":
        device_mb = torch.mps.driver_allocated_memory() / (1024 * 1024)
    print(
        f"mode={arguments.mode} device={device} input={width}x{height} ({width * height / 1e6:.2f} MP) "
        f"output={output.shape[1]}x{output.shape[0]} time={elapsed:.1f}s peak_host={peak_mb:,.0f}MB "
        f"peak_array_buffers={buffers_mb:,.0f}MB"
        + (f" device_memory={device_mb:,.0f}MB" if device_mb is not None else "")
    )


if __name__ == "__main__":
    main()
