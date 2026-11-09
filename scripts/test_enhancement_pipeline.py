"""Does the adaptive engine produce a visible result on a photograph, preset by preset? Real models, the settings of the .env, no database; nothing is written unless --save.

    python scripts/test_enhancement_pipeline.py [--presets auto standard pro] [--expect-enlarged] [--expect-lit] [--save DIR] [--long-edge N] [--no-biometric] PHOTO [PHOTO ...]

The presets are the API's: auto = (auto, natural), standard = (high, natural), pro = (high, ai_max). For every photograph and preset the engine runs as the upload runs it and the script prints the
stages (model, status, seconds), every REFUSED candidate with the gate that failed, what it read and the limit it was held to, the sizes, and then checks:

  * AI: no learned stage was replaced by a classical one (``processing_summary.classical_fallback`` is false);
  * APPLIED: something was applied (the result is not the original);
  * ENLARGED (with --expect-enlarged): a photograph whose short edge is under QUALITY_REVIEW_MIN_EDGE comes back larger;
  * LIT (with --expect-lit): the main face (the frame when there is no face) has a median luminance at least 10 grey levels higher than before.

A check that does not apply to a photograph (a large one cannot be "enlarged") is reported as n/a, never as passed. The exit status is 1 when a check fails, so the script can sit in a deployment check.
"""
import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRESETS = {"auto": ("auto", "natural"), "standard": ("high", "natural"), "pro": ("high", "ai_max")}
LIT_GAIN = 10.0


class RunLine(logging.Handler):
    """Keeps the engine's own run line (``adaptive_enhancement {...}``): the readings of every refused candidate and the sizes."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.runs = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("adaptive_enhancement "):
            self.runs.append(json.loads(message.split(" ", 1)[1]))


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("photos", nargs="+", help="Photograph files.")
    parser.add_argument("--presets", nargs="+", choices=sorted(PRESETS), default=["standard", "pro"], help="Default: standard pro.")
    parser.add_argument("--expect-enlarged", action="store_true", help="A small photograph must come back larger.")
    parser.add_argument("--expect-lit", action="store_true", help="The main face (or the frame) must come back brighter.")
    parser.add_argument("--save", metavar="DIR", help="Write each result next to this folder as <photo>_<preset>.png.")
    parser.add_argument("--long-edge", type=int, default=0, help="Reduce the photograph to this long edge first (default 0 = as it is).")
    parser.add_argument("--no-biometric", action="store_true", help="As if the person had not given face-processing consent.")
    return parser.parse_args(argv)


def brightness(report):
    """``(what, median luminance)``: the main face's when the analysis found one, else the frame's."""
    faces = report.get("faces") or []
    if faces and (faces[0].get("luminance") or {}).get("p50") is not None:
        return "face", float(faces[0]["luminance"]["p50"])
    return "frame", float((report.get("exposure") or {}).get("scene_median") or 0.0)


def main(argv):
    args = parse(argv)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_DEBUG", "True")
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    import django

    django.setup()
    import cv2
    from django.conf import settings

    from enhancement.adaptive.service import AdaptiveImageEnhancementService
    from enhancement.restoration import processing_summary

    run_line = RunLine()
    engine_log = logging.getLogger("enhancement.adaptive.service")
    engine_log.addHandler(run_line)
    engine_log.setLevel(logging.INFO)
    save = Path(args.save) if args.save else None
    if save:
        save.mkdir(parents=True, exist_ok=True)
    failures = 0
    for path in args.photos:
        image = cv2.imread(path)
        if image is None:
            print(f"\n{path}: cannot be read as an image")
            failures += 1
            continue
        if args.long_edge and max(image.shape[:2]) > args.long_edge:
            ratio = args.long_edge / max(image.shape[:2])
            image = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
        short_edge = min(image.shape[:2])
        for preset in args.presets:
            level, style = PRESETS[preset]
            print(f"\n=== {Path(path).name}  {image.shape[1]}x{image.shape[0]}  preset {preset} ({level}/{style})")
            started = time.perf_counter()
            try:
                output, _applied_level, parameters = AdaptiveImageEnhancementService().enhance(image, level, style, allow_biometric_processing=not args.no_biometric)
            except Exception as error:      # one photograph must not stop the survey: the failure is the finding
                print(f"  FAIL  the engine raised {type(error).__name__}: {error}")
                failures += 1
                continue
            seconds = time.perf_counter() - started
            adaptive = parameters["adaptive"]
            run = run_line.runs.pop() if run_line.runs else {}
            for stage in adaptive["stages"]:
                print(f"  {stage['operation']:24s} {stage['status']:9s} {float(stage.get('seconds') or 0):6.1f} s  {', '.join(stage.get('models_run') or [])}")
            for refusal in run.get("refused", []):
                print(f"  REFUSED {refusal['operation']} ({refusal['variant']}): {', '.join(refusal['reasons'])}")
                for gate, readings in refusal["failed"].items():
                    print(f"      {gate}: {readings}")
            print(f"  size {image.shape[1]}x{image.shape[0]} -> {output.shape[1]}x{output.shape[0]} in {seconds:.1f} s; {adaptive['outcome']}")
            summary = processing_summary(parameters) or {}
            checks = []
            checks.append(("AI", not summary.get("classical_fallback"),
                           "learned stages: " + (", ".join(f"{item['stage']}={item['engine']}" for item in summary.get("stages", []) if item["kind"] == "ai") or "none")
                           + ("; a classical engine stood in for a learned one" if summary.get("classical_fallback") else "")))
            checks.append(("APPLIED", bool(adaptive["operations_applied"]), "applied: " + (", ".join(adaptive["operations_applied"]) or "nothing")))
            if args.expect_enlarged:
                if short_edge >= settings.QUALITY_REVIEW_MIN_EDGE:
                    checks.append(("ENLARGED", None, f"short edge {short_edge} px is not under {settings.QUALITY_REVIEW_MIN_EDGE}: the engine does not enlarge it"))
                else:
                    checks.append(("ENLARGED", output.shape[1] > image.shape[1], f"{image.shape[1]} px -> {output.shape[1]} px wide"))
            if args.expect_lit:
                what, before = brightness(adaptive["quality_report"])
                _what, after = brightness(adaptive["final_quality_report"])
                checks.append(("LIT", after - before >= LIT_GAIN, f"median luminance of the {what} {before:.0f} -> {after:.0f} (needs +{LIT_GAIN:.0f})"))
            for name, passed, detail in checks:
                verdict = "n/a " if passed is None else "PASS" if passed else "FAIL"
                failures += passed is False
                print(f"  {verdict}  {name:9s} {detail}")
            if save:
                target = save / f"{Path(path).stem}_{preset}.png"
                cv2.imwrite(str(target), output)
                print(f"  saved {target}")
    print(f"\n{'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
