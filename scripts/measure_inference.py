"""Measure latency and peak memory of each heavy model, per image size and device, each in a fresh process.

    python scripts/measure_inference.py --images folder_of_photos --out report.json [--sizes 640,1280,2048] [--device cpu] [--repeats 3]

Why a fresh process per measurement: peak resident memory is a high-water mark of the whole process, so models measured one after
another in a single process would inherit each other's peaks. Each (model, size) therefore runs in its own interpreter and reports

* ``cold_seconds``: the first call, which includes loading the checkpoint (what the first job on a new worker pays);
* ``warm_seconds``: every later call (what a worker with the model resident pays) and its p50 / max;
* ``peak_rss_mb``: peak resident memory of the process, with the Django/OpenCV/PyTorch baseline reported separately so the model's
  own cost can be read off;
* ``device``: where it really ran (cpu / mps / cuda).

These numbers describe **this machine only**. They are the right way to size a different machine (run this script there) and the wrong
way to guess at one: an Apple M1 does not predict a Hostinger VPS vCPU, nor an NVIDIA GPU. ``--device cpu`` is the closest proxy
available here for a CPU-only worker, and is still an Arm laptop CPU, not a VPS.
"""
import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ("retinexformer", "nafnet_sidd", "nafnet_gopro", "realesrgan_x2")


def peak_rss_mb():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 0)


def worker(arguments):
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    for name in ("RETINEXFORMER_DEVICE", "HD_RESTORATION_DEVICE"):
        os.environ[name] = arguments.device
    import cv2
    import django
    import numpy as np

    django.setup()
    from django.test.utils import override_settings

    path = sorted(p for p in arguments.images.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})[0]
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    scale = arguments.size / max(image.shape[:2])
    image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    baseline = peak_rss_mb()
    with override_settings(RETINEXFORMER_ENABLED=True, NAFNET_DENOISE_ENABLED=True, NAFNET_DEBLUR_ENABLED=True, HD_RESTORATION_ENABLED=True):
        if arguments.model == "retinexformer":
            from enhancement.lowlight import enhance_with_retinexformer as run
        elif arguments.model == "nafnet_sidd":
            from enhancement.hd_restoration import denoise_with_nafnet as run
        elif arguments.model == "nafnet_gopro":
            from enhancement.hd_restoration import deblur_with_nafnet as run
        else:
            from enhancement.hd_restoration import upscale_with_realesrgan

            def run(picture):
                return upscale_with_realesrgan(picture, output_scale=2)

        timings, device, tiles = [], None, None
        for _ in range(arguments.repeats + 1):
            started = time.perf_counter()
            output, metadata = run(image)
            timings.append(round(time.perf_counter() - started, 2))
            device, tiles = metadata.get("device"), metadata.get("tiles")
    warm = sorted(timings[1:])
    print(json.dumps({
        "model": arguments.model, "size": arguments.size, "input": [image.shape[1], image.shape[0]], "output": [output.shape[1], output.shape[0]],
        "device": device, "tiles": tiles, "cold_seconds": timings[0], "warm_seconds": timings[1:],
        "warm_p50": warm[len(warm) // 2] if warm else None, "warm_max": warm[-1] if warm else None,
        "baseline_rss_mb": baseline, "peak_rss_mb": peak_rss_mb(), "cpu_count": os.cpu_count(),
    }))
    _ = np


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--sizes", default="640,1280,2048")
    parser.add_argument("--models", default=",".join(MODELS))
    parser.add_argument("--device", default="cpu", help="cpu, mps, cuda or auto")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--model", help=argparse.SUPPRESS)
    parser.add_argument("--size", type=int, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.worker:
        return worker(arguments)
    results = []
    for model in arguments.models.split(","):
        for size in (int(s) for s in arguments.sizes.split(",")):
            command = [sys.executable, __file__, "--worker", "--images", str(arguments.images), "--model", model, "--size", str(size),
                       "--device", arguments.device, "--repeats", str(arguments.repeats)]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=3600)
            line = next((ln for ln in reversed(completed.stdout.splitlines()) if ln.startswith("{")), None)
            result = json.loads(line) if line else {"model": model, "size": size, "error": (completed.stderr or "no output")[-300:]}
            results.append(result)
            print(json.dumps(result), flush=True)
    if arguments.out:
        arguments.out.write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
