"""Show the same photo under the default policy and under the optional learned policy, side by side.

    python scripts/compare_policies.py --image portrait.jpg --darken 0.28 --out compare.jpg

``--darken`` multiplies the photo's brightness first (to make a low-light test from a good photo).
The two runs are separate processes, because the policy is a setting read at start-up:
the learned policy (``ENHANCEMENT_RENDER_POLICY=learned_v1``) lets each model whose own setting
is on run (Zero-DCE and EDSR need only OpenCV; the PyTorch models need their environment).
Nothing is written but the output image and a few lines of text.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_one(image_path, darken, level, out_path):
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import cv2
    import django
    import numpy as np

    django.setup()
    from django.conf import settings

    from enhancement.services import enhance_bgr

    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"Cannot read {image_path}")
    longest = max(image.shape[:2])
    if longest > 1600:
        scale = 1600 / longest
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    if darken != 1.0:
        image = np.clip(image.astype(np.float32) * darken, 0, 255).astype(np.uint8)
    output, applied, parameters = enhance_bgr(image, level)
    cv2.imwrite(out_path, output)
    print(json.dumps({
        "policy": settings.ENHANCEMENT_RENDER_POLICY, "level": str(applied),
        "mean_luminance": [parameters["quality_before"]["mean_luminance"], parameters["quality_after"]["mean_luminance"]],
        "algorithm": parameters["algorithm"],
    }))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--darken", type=float, default=1.0)
    parser.add_argument("--level", default="auto", choices=("auto", "low", "medium", "high"))
    parser.add_argument("--out", default="policy_compare.jpg")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.worker:
        run_one(arguments.image, arguments.darken, arguments.level, arguments.worker)
        return 0
    import cv2
    import numpy as np

    panels = []
    for policy in ("lightweight_v1", "learned_v1"):
        temporary = f"{arguments.out}.{policy}.jpg"
        environment = {**os.environ, "ENHANCEMENT_RENDER_POLICY": policy, "DJANGO_DEBUG": os.environ.get("DJANGO_DEBUG", "True")}
        subprocess.run(
            [sys.executable, __file__, "--image", arguments.image, "--darken", str(arguments.darken),
             "--level", arguments.level, "--worker", temporary],
            check=True, env=environment,
        )
        image = cv2.imread(temporary)
        os.remove(temporary)
        height = 900
        image = cv2.resize(image, (round(image.shape[1] * height / image.shape[0]), height))
        panels.append(cv2.putText(image, policy, (12, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2))
    cv2.imwrite(arguments.out, np.hstack(panels))
    print(f"Wrote {arguments.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
