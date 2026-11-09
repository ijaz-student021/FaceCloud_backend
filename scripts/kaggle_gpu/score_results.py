"""Score the faces Kaggle brought back, on this machine, with the same identity model the application uses (SFace), and print the table.

    python scripts/kaggle_gpu/score_results.py --kit ~/kaggle_face_kit --results ~/kaggle_face_results [--out ~/kaggle_face_report]

For every model and degradation (``out/<model>/<kind>/<id>.png`` under ``--results``) it reports

* identity: SFace cosine similarity between the restored face and the CLEAN face (the truth the engine never has), as mean / minimum, next to the same figure for the degraded face itself (``degraded``),
* what the application's identity gate would see: the restored face against the degraded one,
* face detail: Laplacian variance of the central part of the face relative to the clean face (1.0 = as sharp; noise raises it, so read it with the noise rows in mind),
* LPIPS to the clean face when ``lpips`` and torch are installed, and seconds per image from the Kaggle run's ``timings.json``,

and writes a before / after montage per degradation. SFace similarity is evidence, not proof; the faces are aligned crops, so a model that was built for whole photographs may look worse than it is.
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / ".runtime" / "models"


class Identity:
    """SFace through OpenCV, as the application uses it: YuNet finds the face, ``alignCrop`` aligns it, the cosine compares the embeddings."""

    def __init__(self, yunet, sface):
        self.detector = cv2.FaceDetectorYN.create(str(yunet), "", (512, 512), 0.5, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(sface), "")

    def embed(self, image):
        self.detector.setInputSize((image.shape[1], image.shape[0]))
        _status, faces = self.detector.detect(image)
        if faces is None or len(faces) == 0:
            return None
        best = max(faces, key=lambda row: row[2] * row[3])
        return self.recognizer.feature(self.recognizer.alignCrop(image, best))

    def similarity(self, first, second):
        if first is None or second is None:
            return None
        return float(self.recognizer.match(first, second, cv2.FaceRecognizerSF_FR_COSINE))


def face_detail(image):
    """Laplacian variance of the central 60 % of a 512 px aligned face (the part inside the face and hair)."""
    height, width = image.shape[:2]
    centre = image[int(height * 0.2):int(height * 0.8), int(width * 0.2):int(width * 0.8)]
    return float(cv2.Laplacian(cv2.cvtColor(centre, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def summarise(values):
    values = [value for value in values if value is not None]
    return (float(np.mean(values)), float(np.min(values))) if values else (None, None)


def fmt(value, digits=2):
    return "-" if value is None else f"{value:.{digits}f}"


def load_lpips():
    try:
        import lpips
        import torch

        network = lpips.LPIPS(net="alex", verbose=False)

        def distance(first, second):
            def tensor(image):
                return torch.from_numpy(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).permute(2, 0, 1).float().unsqueeze(0) / 127.5 - 1.0

            with torch.no_grad():
                return float(network(tensor(first), tensor(second)).item())

        return distance
    except Exception:  # LPIPS is optional; the table says "-" without it
        return None


def score(kit, results, identity, lpips_distance):
    config = json.loads((kit / "config.json").read_text(encoding="utf-8"))
    out_dir = results / "out" if (results / "out").is_dir() else results
    timings_path = results / "timings.json"
    timings = json.loads(timings_path.read_text(encoding="utf-8")) if timings_path.is_file() else {}
    clean = {name: cv2.imread(str(kit / "clean" / f"{name}.png")) for name in config["ids"]}
    clean_embeddings = {name: identity.embed(image) for name, image in clean.items()}
    rows = []
    for kind in config["kinds"]:
        degraded = {name: cv2.imread(str(kit / "lq" / kind / f"{name}.png")) for name in config["ids"]}
        degraded_embeddings = {name: identity.embed(image) for name, image in degraded.items()}
        rows.append({"model": "degraded", "kind": kind, "found": len(degraded), "expected": len(degraded), "to_clean": [identity.similarity(clean_embeddings[n], degraded_embeddings[n]) for n in degraded],
                     "to_degraded": [], "detail": [face_detail(degraded[n]) / face_detail(clean[n]) for n in degraded], "lpips": [lpips_distance(clean[n], degraded[n]) for n in degraded] if lpips_distance else [], "seconds": None})
        for model_dir in sorted(path for path in out_dir.iterdir() if path.is_dir()):
            files = {path.stem: path for path in (model_dir / kind).glob("*.png")} if (model_dir / kind).is_dir() else {}
            restored = {name: cv2.imread(str(path)) for name, path in files.items() if name in clean}
            restored = {name: image for name, image in restored.items() if image is not None}
            embeddings = {name: identity.embed(image) for name, image in restored.items()}
            timing = (timings.get(model_dir.name) or {}).get(kind) or {}
            rows.append({"model": model_dir.name, "kind": kind, "found": len(restored), "expected": len(config["ids"]),
                         "to_clean": [identity.similarity(clean_embeddings[n], embeddings[n]) for n in restored], "to_degraded": [identity.similarity(degraded_embeddings[n], embeddings[n]) for n in restored],
                         "detail": [face_detail(restored[n]) / face_detail(clean[n]) for n in restored], "lpips": [lpips_distance(clean[n], restored[n]) for n in restored] if lpips_distance else [],
                         "seconds": timing.get("seconds_per_image")})
    return rows


def table(rows):
    lines = ["| degradation | model | faces | identity vs clean (mean / min) | what the gate sees (vs the degraded face) | face detail vs clean | LPIPS to clean | s per image |", "|---|---|---|---|---|---|---|---|"]
    for row in rows:
        clean_mean, clean_min = summarise(row["to_clean"])
        gate_mean, _gate_min = summarise(row["to_degraded"])
        lpips_mean, _ = summarise(row["lpips"])
        detail_mean, _ = summarise(row["detail"])
        lines.append(f"| {row['kind']} | {row['model']} | {row['found']}/{row['expected']} | {fmt(clean_mean)} / {fmt(clean_min)} | {fmt(gate_mean)} | {fmt(detail_mean)} | {fmt(lpips_mean, 3)} | {fmt(row['seconds'], 1)} |")
    return "\n".join(lines)


def montage(kit, results, out, kinds, count=4):
    out_dir = results / "out" if (results / "out").is_dir() else results
    models = sorted(path for path in out_dir.iterdir() if path.is_dir())
    ids = json.loads((kit / "config.json").read_text(encoding="utf-8"))["ids"][:count]
    for kind in kinds:
        rows = []
        for name in ids:
            tiles = [("clean", cv2.imread(str(kit / "clean" / f"{name}.png"))), ("degraded", cv2.imread(str(kit / "lq" / kind / f"{name}.png")))]
            tiles += [(model.name, cv2.imread(str(model / kind / f"{name}.png"))) for model in models]
            tiles = [(label, cv2.resize(image, (256, 256), interpolation=cv2.INTER_AREA) if image is not None else np.zeros((256, 256, 3), np.uint8)) for label, image in tiles]
            for label, image in tiles:
                cv2.putText(image, label[:18], (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
            rows.append(np.hstack([image for _label, image in tiles]))
        cv2.imwrite(str(out / f"montage_{kind}.jpg"), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 90])


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kit", required=True)
    parser.add_argument("--results", required=True, help="The folder `kaggle_run.py fetch` wrote (holds out/ and timings.json).")
    parser.add_argument("--out", help="Write the table and the montages here.")
    parser.add_argument("--yunet", default=str(MODELS_DIR / "yunet_s_dynamic.onnx"))
    parser.add_argument("--sface", default=str(MODELS_DIR / "face_recognition_sface_2021dec.onnx"))
    args = parser.parse_args(argv)
    kit, results = Path(args.kit).expanduser().resolve(), Path(args.results).expanduser().resolve()
    for path in (Path(args.yunet), Path(args.sface)):
        if not path.is_file():
            print(f"{path} is missing; pass --yunet / --sface.")
            return 2
    rows = score(kit, results, Identity(args.yunet, args.sface), load_lpips())
    report = table(rows)
    print(report)
    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        (out / "RESULTS.md").write_text(report + "\n", encoding="utf-8")
        montage(kit, results, out, json.loads((kit / "config.json").read_text(encoding="utf-8"))["kinds"])
        print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
