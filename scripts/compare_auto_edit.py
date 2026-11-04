"""Compare the editor's Auto with a hand edit of the same photograph.

    python scripts/compare_auto_edit.py --source enhanced.jpg [--baseline unenhanced.jpg] --edit '{"brightness": -65, "contrast": 18, "shadows": -18, "saturation": 14, "warmth": 9}' [--out folder]

``--source`` is the photograph the editor opens (the enhanced result), ``--baseline`` the unenhanced upload (without it a dark photograph's black level cannot be compared and its exposure is left alone),
``--edit`` the sliders a person set by hand (the numbers on the editor's sliders). Prints what Auto decided and why, the sliders side by side, and for the source, Auto's render and the hand edit
the measurements Auto works from (black floor, subject brightness, colour of the lit part) and how far apart the renders are. Only the tone sliders are compared (the model-backed ones are dropped
unless ``--with-models``). Nothing is written but ``--out``. Photographs of real people stay on this machine: nothing is uploaded and nothing here is committed.

Each person's edit is one more data point for ``enhancement/auto_edit.py``: its constants marked "owner" were read from a single edit.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_SLIDERS = ("lowlight", "denoise", "deblur", "face_restore")


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--edit", required=True, help="JSON object of the hand-set sliders.")
    parser.add_argument("--with-models", action="store_true", help="Keep the model-backed sliders of the hand edit (slow; needs the models).")
    parser.add_argument("--out", help="Write the three renders here (not inside the repository).")
    args = parser.parse_args(argv)
    if args.out and ROOT in Path(args.out).resolve().parents:
        print("--out must not be inside the repository: the photographs may show real people.")
        return 2

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_DEBUG", "True")
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    import django

    django.setup()
    import cv2
    import numpy as np

    from enhancement.auto_edit import measure_scene
    from enhancement.services import apply_gallery_adjustments

    source = cv2.imread(args.source)
    baseline = cv2.imread(args.baseline) if args.baseline else None
    if source is None:
        print(f"{args.source} cannot be read as an image.")
        return 2
    edit = {name: value for name, value in json.loads(args.edit).items() if args.with_models or name not in MODEL_SLIDERS}

    auto_image, auto_meta = apply_gallery_adjustments(source, "auto", {"intent": "auto"}, baseline=(lambda: baseline) if baseline is not None else None)
    edit_image, edit_meta = apply_gallery_adjustments(source, "manual", edit)
    auto = auto_meta["auto"]
    print(f"nature: {auto['nature']}")
    for line in auto["rationale"]:
        print(f"  - {line}")
    print("\n{:14} {:>8} {:>8}".format("slider", "Auto", "edit"))
    for name in ("brightness", "contrast", "shadows", "highlights", "saturation", "warmth", "sharpness"):
        print("{:14} {:8.1f} {:8.1f}".format(name, auto_meta["applied"].get(name, 0.0), edit_meta["applied"].get(name, 0.0)))
    print("\n{:14} {:>9} {:>9} {:>11} {:>13}".format("", "floor", "p95", "chroma(lit)", "warmth(lit)"))
    for label, image in (("source", source), ("Auto", auto_image), ("hand edit", edit_image)):
        scene = measure_scene(image)
        floor = "-" if scene["dark_median"] is None else f"{scene['dark_median']:.1f}"
        print("{:14} {:>9} {:9.1f} {:11.1f} {:13.1f}".format(label, floor, scene["p95"], scene["chroma_lit"], scene["warmth_lit"]))

    def distance(first, second):
        return float(np.abs(first.astype(np.float32) - second.astype(np.float32)).mean())

    print(f"\nmean difference (of 255) between the hand edit and: Auto {distance(auto_image, edit_image):.2f}, the unedited source {distance(source, edit_image):.2f}")
    if args.out:
        folder = Path(args.out)
        folder.mkdir(parents=True, exist_ok=True)
        for label, image in (("source", source), ("auto", auto_image), ("hand_edit", edit_image)):
            cv2.imwrite(str(folder / f"{label}.jpg"), image, [cv2.IMWRITE_JPEG_QUALITY, 93])
        print(f"renders written to {folder}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
