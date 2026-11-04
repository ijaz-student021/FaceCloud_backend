"""Build the input kit for a Kaggle GPU bake-off of face-restoration models: aligned 512 px faces, a degraded copy of each, and the files Kaggle needs.

    python scripts/kaggle_gpu/prepare_inputs.py --photos ~/lfw/lfw --count 40 --out ~/kaggle_face_kit [--models codeformer,gfpgan,diffbir,pmrf]

``--photos`` is a folder of photographs (searched recursively; one photograph per sub-folder is taken first, so a set such as Labeled Faces in the Wild gives different people). The default
use is a PUBLIC benchmark: nothing of anyone's private photographs leaves the machine. Using your own photographs is a decision for you and the people in them (a Kaggle dataset is stored on
Kaggle's servers; make it private and delete it afterwards); the ids written here are numbers, never file names.

Every face is found with OpenCV's YuNet (``--yunet``), warped to the 512 x 512 five-point template that CodeFormer, GFPGAN, PMRF and DiffBIR expect (the template and the ``estimateAffinePartial2D``
warp are facexlib's own), saved as ``clean/<id>.png`` and degraded into ``lq/<kind>/<id>.png`` for ``blur``, ``motion``, ``small`` and ``noise`` (see ``degrade_face``).
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]

PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
KINDS = ("blur", "motion", "small", "noise")
MODELS = ("codeformer", "gfpgan", "diffbir", "pmrf")
FACE_SIZE = 512
# facexlib.utils.face_restoration_helper: the five FFHQ landmarks on a 512 x 512 face (eye, eye, nose, mouth corner, mouth corner; image left to right).
TEMPLATE = np.array([[192.98138, 239.94708], [318.90277, 240.1936], [256.63416, 314.01935], [201.26117, 371.41043], [313.08905, 371.15118]], np.float32)
BORDER_COLOUR = (135, 133, 132)      # facexlib's border value


def find_photos(folder, count):
    """Up to ``count`` photographs: the first of each sub-folder (different people in a set such as LFW), then the rest, in a stable order."""
    folder = Path(folder).expanduser()
    files = sorted(path for path in folder.rglob("*") if path.suffix.lower() in PHOTO_TYPES)
    firsts, seen = [], set()
    for path in files:
        if path.parent not in seen:
            seen.add(path.parent)
            firsts.append(path)
    taken = set(firsts)
    rest = [path for path in files if path not in taken]
    return (firsts + rest)[: max(count * 3, count)]


def detect_landmarks(detector, image):
    """The five landmarks of the largest face as ``(5, 2)`` float32 in image order (left eye, right eye, nose, left mouth corner, right mouth corner), or ``None``."""
    height, width = image.shape[:2]
    detector.setInputSize((width, height))
    _status, faces = detector.detect(image)
    if faces is None or len(faces) == 0:
        return None
    best = max(faces, key=lambda row: row[2] * row[3])
    return np.array(best[4:14], np.float32).reshape(5, 2)      # YuNet lists the person's right eye first, which is the left of the image: the order the template uses


def align(image, landmarks):
    matrix = cv2.estimateAffinePartial2D(landmarks, TEMPLATE, method=cv2.LMEDS)[0]
    return cv2.warpAffine(image, matrix, (FACE_SIZE, FACE_SIZE), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=BORDER_COLOUR)


def degrade_face(kind, clean):
    """A degraded copy of a 512 px face: Gaussian blur (sigma 4), a horizontal motion blur (21 px kernel), Gaussian noise (sigma 25), or ``small`` (down to 0.25 and back up to 512 px).

    Stronger than the demonstration set's values for a 1000 px photograph, on purpose: a public set such as LFW is 250 px, so its "clean" faces are already soft at 512 px and a mild
    degradation leaves a model nothing to restore. Use the numbers to rank models, not as absolute quality.
    """
    if kind == "blur":
        return cv2.GaussianBlur(clean, (0, 0), 4.0)
    if kind == "motion":
        kernel = np.zeros((21, 21), np.float32)
        kernel[10, :] = 1.0 / 21.0
        return cv2.filter2D(clean, -1, kernel)
    if kind == "noise":
        noise = np.random.default_rng(25).normal(0, 25.0, clean.shape)
        return np.clip(clean.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    if kind == "small":
        small = cv2.resize(clean, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
        return cv2.resize(small, (FACE_SIZE, FACE_SIZE), interpolation=cv2.INTER_CUBIC)
    raise ValueError(kind)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--photos", required=True, help="A folder of photographs (public set recommended).")
    parser.add_argument("--count", type=int, default=40, help="How many faces (default 40).")
    parser.add_argument("--out", required=True, help="Where the kit is written (outside the repository).")
    parser.add_argument("--yunet", default=str(ROOT / ".runtime" / "models" / "yunet_s_dynamic.onnx"))
    parser.add_argument("--models", default=",".join(MODELS), help="Models the Kaggle run should try (default all).")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    if ROOT in out.parents or out == ROOT:
        print("--out must be outside the repository: the kit may hold photographs of real people.")
        return 2
    models = [name for name in args.models.split(",") if name]
    unknown = [name for name in models if name not in MODELS]
    if unknown:
        print(f"Unknown models: {', '.join(unknown)}. Known: {', '.join(MODELS)}")
        return 2
    if not Path(args.yunet).is_file():
        print(f"The YuNet model is not at {args.yunet}; pass --yunet.")
        return 2

    detector = cv2.FaceDetectorYN.create(args.yunet, "", (320, 320), 0.8, 0.3, 5000)
    (out / "clean").mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        (out / "lq" / kind).mkdir(parents=True, exist_ok=True)
    ids = []
    for path in find_photos(args.photos, args.count):
        if len(ids) >= args.count:
            break
        image = cv2.imread(str(path))
        landmarks = None if image is None else detect_landmarks(detector, image)
        if landmarks is None:
            continue
        clean = align(image, landmarks)
        name = f"{len(ids):04d}"
        cv2.imwrite(str(out / "clean" / f"{name}.png"), clean)
        for kind in KINDS:
            cv2.imwrite(str(out / "lq" / kind / f"{name}.png"), degrade_face(kind, clean))
        ids.append(name)
    if not ids:
        print("No face was found in the photographs.")
        return 1
    (out / "config.json").write_text(json.dumps({"ids": ids, "kinds": list(KINDS), "models": models, "face_size": FACE_SIZE}, indent=2), encoding="utf-8")
    (out / "dataset-metadata.json").write_text(json.dumps({"title": "facecloud-face-kit", "id": "USERNAME/facecloud-face-kit", "licenses": [{"name": "other"}]}, indent=2), encoding="utf-8")
    print(f"{len(ids)} faces x {len(KINDS)} degradations written to {out}. Next: python scripts/kaggle_gpu/kaggle_run.py push --kit {out} --username YOUR_KAGGLE_NAME")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
