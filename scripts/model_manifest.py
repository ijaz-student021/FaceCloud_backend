"""Record and verify the model files FaceCloud needs, so a copy to another machine can be proven intact.

The models live outside Git (``.runtime/models`` and ``gfpgan/weights``). Before a
GPU session, write the manifest on the machine that has the models; after copying
them to the GPU machine, verify it there.

    python scripts/model_manifest.py write
    python scripts/model_manifest.py verify

The manifest holds only file names, sizes and SHA-256 checksums, so it is safe to
commit (``docs/gpu/model_manifest.json``). Verification exits non-zero when any file
is missing or different, and it lists files present but not in the manifest.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "gpu" / "model_manifest.json"
# name in the manifest -> folder, relative to the repository root
ROOTS = {
    "models": Path(".runtime") / "models",
    "gfpgan_weights": Path("gfpgan") / "weights",
}
CHUNK = 4 * 1024 * 1024


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def scan(base):
    """``{relative posix path: {"size", "sha256"}}`` for every file under ``base``."""
    files = {}
    if not base.is_dir():
        return files
    for path in sorted(item for item in base.rglob("*") if item.is_file()):
        files[path.relative_to(base).as_posix()] = {"size": path.stat().st_size, "sha256": sha256_of(path)}
    return files


def build_manifest(repo_root):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "File names, sizes and SHA-256 checksums only. The model files themselves are not in Git.",
        "roots": {name: scan(Path(repo_root) / folder) for name, folder in ROOTS.items()},
    }


def verify_manifest(repo_root, manifest):
    """Return ``{"ok", "missing", "mismatched", "extra"}`` lists of ``root/relative`` names."""
    result = {"ok": [], "missing": [], "mismatched": [], "extra": []}
    for name, folder in ROOTS.items():
        expected = manifest.get("roots", {}).get(name, {})
        actual = scan(Path(repo_root) / folder)
        for relative, details in expected.items():
            label = f"{name}/{relative}"
            found = actual.get(relative)
            if found is None:
                result["missing"].append(label)
            elif found["sha256"] != details["sha256"] or found["size"] != details["size"]:
                result["mismatched"].append(label)
            else:
                result["ok"].append(label)
        result["extra"].extend(f"{name}/{relative}" for relative in actual if relative not in expected)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["write", "verify"])
    parser.add_argument("--repo", type=Path, default=ROOT, help="Repository root (default: this repository).")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)

    if args.command == "write":
        manifest = build_manifest(args.repo)
        total = sum(len(files) for files in manifest["roots"].values())
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {total} file(s) to {args.manifest}")
        for name, files in manifest["roots"].items():
            print(f"  {name}: {len(files)} file(s)")
        return 0

    if not args.manifest.is_file():
        print(f"No manifest at {args.manifest}. Run `write` on the machine that has the models.")
        return 2
    result = verify_manifest(args.repo, json.loads(args.manifest.read_text(encoding="utf-8")))
    print(f"OK: {len(result['ok'])}   missing: {len(result['missing'])}   "
          f"different: {len(result['mismatched'])}   not in manifest: {len(result['extra'])}")
    for key, label in (("missing", "MISSING"), ("mismatched", "DIFFERENT"), ("extra", "extra")):
        for name in result[key]:
            print(f"  {label}: {name}")
    return 1 if result["missing"] or result["mismatched"] else 0


if __name__ == "__main__":
    sys.exit(main())
