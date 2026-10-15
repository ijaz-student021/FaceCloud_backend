"""Download the optional model checkpoints from their public sources and check each against its pinned SHA-256.

    python scripts/fetch_models.py                 # everything that is missing
    python scripts/fetch_models.py --only zero_dce,realesrgan
    python scripts/fetch_models.py --list

The checkpoints are not in Git (``docs/gpu/model_manifest.json`` records their names,
sizes and checksums). A file is kept only if it hashes to the pinned value; anything
else is deleted, and the pin is never changed to accept a different file. Nothing is
downloaded for a file that is already present and correct.

The learned enhancement models are present in the backend but *disabled by default*
(``ENHANCEMENT_RENDER_POLICY=lightweight_v1``); this tool only puts the files in place.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "gpu" / "model_manifest.json"
ARCHIVE = Path.home() / "FaceCloud_retired_2026-10-15"

# key -> (destination under the repository, pinned in the manifest as root/relative, how to fetch)
MODELS = {
    "zero_dce": (".runtime/models/zero_dce_epoch99.onnx", "models/zero_dce_epoch99.onnx",
                 ("local", "zero_dce_epoch99.onnx")),  # exported locally from the official Epoch99.pth; see the provenance doc
    # The official link is a Google Drive file (megvii-research/NAFNet, docs/GoPro.md); needs `gdown`.
    "nafnet": (".runtime/models/NAFNet-GoPro-width64.pth", "models/NAFNet-GoPro-width64.pth",
               ("drive", "1S0PVRbyTakYY9a82kujgZLbMihfNBLfC")),
    "nafnet_sidd": (".runtime/models/NAFNet-SIDD-width64.pth", "models/NAFNet-SIDD-width64.pth",
                    ("drive", "14Fht1QQJ2gMlk4N1ERCRuElg8JfjrWWR")),  # megvii-research/NAFNet readme, SIDD-width64
    "retinexformer": (".runtime/models/Retinexformer_NTIRE.pth", "models/Retinexformer_NTIRE.pth",
                      ("drive", "1K-QR-A_CPe6iAgjE6_04Q20DkkVwaVta")),  # caiyuanhao1998/Retinexformer pretrained_weights/NTIRE.pth
    "codeformer": (".runtime/models/codeformer/codeformer.pth", "models/codeformer/codeformer.pth",
                   ("url", "https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/codeformer.pth")),
    "arcface": (".runtime/models/arcface/w600k_r50.onnx", "models/arcface/w600k_r50.onnx",
                ("zip", "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip|w600k_r50.onnx")),
    "realesrgan": (".runtime/models/RealESRGAN_x4plus.pth", "models/RealESRGAN_x4plus.pth",
                   ("url", "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth")),
    "gfpgan": (".runtime/models/gfpgan/GFPGANv1.4.pth", "models/gfpgan/GFPGANv1.4.pth",
               ("url", "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth")),
    "gfpgan_detector": ("gfpgan/weights/detection_Resnet50_Final.pth", "gfpgan_weights/detection_Resnet50_Final.pth",
                        ("url", "https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth")),
    "gfpgan_parser": ("gfpgan/weights/parsing_parsenet.pth", "gfpgan_weights/parsing_parsenet.pth",
                      ("url", "https://github.com/xinntao/facexlib/releases/download/v0.2.2/parsing_parsenet.pth")),
}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pinned():
    manifest = json.loads(MANIFEST.read_text())
    return {f"{root}/{name}": details for root, files in manifest["roots"].items() for name, details in files.items()}


def fetch(kind, source, target):
    if kind == "local":
        copy = ARCHIVE / source
        if not copy.is_file():
            raise RuntimeError(f"{copy} is not there; export it with the official Epoch99.pth (see docs/model-licenses/zero-dce-provenance.md)")
        shutil.copy2(copy, target)
    elif kind == "url":
        if not source.startswith("https://"):
            raise ValueError("only https sources are allowed")
        request = urllib.request.Request(source, headers={"User-Agent": "facecloud-fetch-models"})
        with urllib.request.urlopen(request, timeout=120) as response, open(target, "wb") as out:  # nosec B310 - https only, fixed table
            shutil.copyfileobj(response, out, 1024 * 1024)
    elif kind == "zip":
        import zipfile

        url, member = source.split("|")
        if not url.startswith("https://"):
            raise ValueError("only https sources are allowed")
        with tempfile.TemporaryDirectory() as scratch:
            archive = Path(scratch) / "archive.zip"
            request = urllib.request.Request(url, headers={"User-Agent": "facecloud-fetch-models"})
            with urllib.request.urlopen(request, timeout=300) as response, open(archive, "wb") as out:  # nosec B310 - https only, fixed table
                shutil.copyfileobj(response, out, 1024 * 1024)
            with zipfile.ZipFile(archive) as bundle, bundle.open(member) as source_file, open(target, "wb") as out:  # nosec B310
                shutil.copyfileobj(source_file, out, 1024 * 1024)
    elif kind == "drive":
        subprocess.run(["uvx", "gdown", source, "-O", str(target)], check=True)  # nosec B603 B607 - fixed table
    else:
        raise ValueError(kind)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", default="", help="Comma-separated keys (see --list).")
    parser.add_argument("--list", action="store_true")
    arguments = parser.parse_args(argv)
    pins = pinned()
    if arguments.list:
        for key, (destination, pin, (kind, source)) in MODELS.items():
            state = "present" if (ROOT / destination).is_file() else "missing"
            print(f"{key:16s} {state:8s} {destination}")
        return 0
    chosen = [key.strip() for key in arguments.only.split(",") if key.strip()] or list(MODELS)
    unknown = [key for key in chosen if key not in MODELS]
    if unknown:
        print(f"Unknown: {', '.join(unknown)}", file=sys.stderr)
        return 2
    failed = 0
    for key in chosen:
        destination, pin, (kind, source) = MODELS[key]
        target = ROOT / destination
        expected = pins[pin]["sha256"]
        if target.is_file() and sha256(target) == expected:
            print(f"ok       {key} (already correct)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as scratch:
            candidate = Path(scratch) / target.name
            try:
                fetch(kind, source, candidate)
            except Exception as error:  # reported, then the next one
                print(f"FAILED   {key}: {type(error).__name__}: {str(error)[:160]}")
                failed += 1
                continue
            if sha256(candidate) != expected:
                print(f"REFUSED  {key}: the downloaded file does not match the pinned SHA-256; discarded")
                failed += 1
                continue
            shutil.move(str(candidate), str(target))
        print(f"fetched  {key} -> {destination}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
