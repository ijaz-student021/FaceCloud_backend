"""Where the time goes: the adaptive engine on real photographs, seconds per operation and per candidate.

    python scripts/time_pipeline.py [--style natural] [--level auto] [--long-edge 1000] [--device auto|cpu] [--no-biometric] PHOTO [PHOTO ...]

The photographs are reduced to ``--long-edge`` pixels (default 1000, the size the speed target is stated for; 0 = as they are), run through the same engine the upload runs
(settings from the ``.env`` as usual), and a table is printed per photograph: the analysis of the original, each operation (status, seconds, models), and for every candidate the
model's seconds and the validator's seconds, then what is left over (encoding, the identity reference, bookkeeping). ``--device cpu`` runs every model on the CPU, to compare with the
GPU default. Nothing is written: no database, no files.
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEVICE_SETTINGS = ("HD_RESTORATION_DEVICE", "RETINEXFORMER_DEVICE", "CODEFORMER_DEVICE", "DDCOLOR_DEVICE", "GFPGAN_DEVICE")


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("photos", nargs="+", help="Photograph files.")
    parser.add_argument("--style", default="natural", choices=("natural", "dynamic", "ai_max"))
    parser.add_argument("--level", default="auto", choices=("auto", "low", "medium", "high"))
    parser.add_argument("--long-edge", type=int, default=1000, help="Reduce the photograph to this long edge first (default 1000; 0 = as it is).")
    parser.add_argument("--device", default="auto", choices=("auto", "cpu"), help="cpu: every model on the CPU.")
    parser.add_argument("--no-biometric", action="store_true", help="As if the person had not given face-processing consent.")
    return parser.parse_args(argv)


def main(argv):
    args = parse(argv)
    if args.device == "cpu":
        for name in DEVICE_SETTINGS:
            os.environ[name] = "cpu"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_DEBUG", "True")
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    import django

    django.setup()
    import cv2

    from enhancement.adaptive.service import AdaptiveImageEnhancementService

    service = AdaptiveImageEnhancementService()
    for path in args.photos:
        image = cv2.imread(path)
        if image is None:
            print(f"{path}: cannot be read as an image")
            continue
        if args.long_edge and max(image.shape[:2]) > args.long_edge:
            ratio = args.long_edge / max(image.shape[:2])
            image = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
        started = time.perf_counter()
        _output, _level, parameters = service.enhance(image, args.level, args.style, allow_biometric_processing=not args.no_biometric)
        wall = time.perf_counter() - started
        adaptive = parameters["adaptive"]
        print(f"\n{Path(path).name}  {image.shape[1]}x{image.shape[0]}  style {args.style} / {args.level}  device {args.device}  ->  {adaptive['outcome']}, {wall:.1f} s")
        print(f"  {'step':28s} {'status':10s} {'seconds':>8s}  models")
        print(f"  {'analysis of the original':28s} {'':10s} {adaptive.get('analysis_seconds', 0):8.2f}")
        accounted = adaptive.get("analysis_seconds", 0)
        by_id = {c["id"]: c for c in adaptive["candidates"]}
        for stage in adaptive["stages"]:
            seconds = float(stage.get("seconds") or 0)
            validation = sum(by_id.get(cid, {}).get("validation_seconds", 0) for cid in stage.get("candidates", []))
            accounted += seconds + validation
            print(f"  {stage['operation']:28s} {stage['status']:10s} {seconds:8.2f}  {', '.join(stage.get('models_run') or [])}")
            for cid in stage.get("candidates", []):
                candidate = by_id.get(cid, {})
                verdict = "kept" if candidate.get("selected") else ("accepted" if candidate.get("accepted") else "refused")
                print(f"      {candidate.get('variant', ''):24s} {verdict:10s} model {candidate.get('seconds', 0):6.2f} s | validator {candidate.get('validation_seconds', 0):5.2f} s")
        print(f"  {'left over (encoding, identity reference, bookkeeping)':28s} {'':10s} {max(wall - accounted, 0):8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
