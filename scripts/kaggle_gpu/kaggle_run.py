"""Send the kit to Kaggle, run the models on a free GPU there, and bring the restored faces back. A thin wrapper over the official ``kaggle`` command line.

    pip install kaggle                                    # once, in any Python (not the project's venv if you prefer)
    # put your API token where the CLI looks (Kaggle -> Settings -> API; ~/.kaggle/kaggle.json, mode 600) and never share it
    python scripts/kaggle_gpu/kaggle_run.py push   --kit ~/kaggle_face_kit --username YOUR_KAGGLE_NAME [--dry-run]
    python scripts/kaggle_gpu/kaggle_run.py status --username YOUR_KAGGLE_NAME [--wait]
    python scripts/kaggle_gpu/kaggle_run.py fetch  --username YOUR_KAGGLE_NAME --out ~/kaggle_face_results

``push`` uploads the kit as a PRIVATE dataset (``<you>/facecloud-face-kit``) and starts a private GPU notebook (``<you>/facecloud-face-bakeoff``, internet on, so it can clone the models and download
their weights; Kaggle asks for a verified phone number for GPU and internet). The run is a batch job: it queues, runs for minutes to an hour, and cannot be called like a web service. ``fetch``
downloads ``out/<model>/<kind>/<id>.png``, ``timings.json``, ``summary.json`` and ``log.txt``. Delete the dataset on Kaggle when you are done if it held anything but public photographs.
``--dry-run`` only prints the commands.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET_SLUG = "facecloud-face-kit"
KERNEL_SLUG = "facecloud-face-bakeoff"
DONE_STATES = ("COMPLETE", "ERROR", "CANCEL")


def dataset_metadata(username):
    return {"title": DATASET_SLUG, "id": f"{username}/{DATASET_SLUG}", "licenses": [{"name": "other"}]}


def kernel_metadata(username):
    """The Kaggle kernel descriptor: a private script with a GPU and internet, reading the kit's dataset (fields as in the kaggle-api docs/kernels_metadata.md)."""
    return {
        "id": f"{username}/{KERNEL_SLUG}", "title": KERNEL_SLUG, "code_file": "run_models.py", "language": "python", "kernel_type": "script",
        "is_private": "true", "enable_gpu": "true", "enable_internet": "true", "dataset_sources": [f"{username}/{DATASET_SLUG}"],
        "competition_sources": [], "kernel_sources": [], "model_sources": [],
    }


def have_credentials():
    """Whether a Kaggle token seems to be configured (only the presence of a file or variable is checked; nothing is read or printed)."""
    return (Path.home() / ".kaggle" / "kaggle.json").is_file() or bool(os.environ.get("KAGGLE_KEY") or os.environ.get("KAGGLE_API_TOKEN"))


def run(command, dry):
    print("$ " + " ".join(str(part) for part in command), flush=True)
    if dry:
        return 0, ""
    process = subprocess.run([str(part) for part in command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(process.stdout.strip())
    return process.returncode, process.stdout


def preflight(dry):
    if dry:
        return True
    if not shutil.which("kaggle"):
        print("The `kaggle` command is not installed: pip install kaggle")
        return False
    if not have_credentials():
        print("No Kaggle token found. Kaggle -> Settings -> API -> create a token, save it as ~/.kaggle/kaggle.json (chmod 600). Do not paste it anywhere.")
        return False
    return True


def push(args):
    kit = Path(args.kit).expanduser().resolve()
    if not (kit / "config.json").is_file():
        print(f"{kit} is not a kit (no config.json): run prepare_inputs.py first.")
        return 2
    if not preflight(args.dry_run):
        return 2
    metadata = dataset_metadata(args.username)
    (kit / "dataset-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    exists = run(["kaggle", "datasets", "status", metadata["id"]], args.dry_run)[0] == 0 and not args.dry_run
    verb = ["datasets", "version", "-p", kit, "-m", "kit", "--dir-mode", "zip"] if exists else ["datasets", "create", "-p", kit, "--dir-mode", "zip"]
    code, _out = run(["kaggle", *verb], args.dry_run)
    if code:
        return code
    kernel = kit.parent / f"{kit.name}_kernel"
    kernel.mkdir(exist_ok=True)
    shutil.copy(HERE / "kernel" / "run_models.py", kernel / "run_models.py")
    (kernel / "kernel-metadata.json").write_text(json.dumps(kernel_metadata(args.username), indent=2), encoding="utf-8")
    print("The dataset needs a minute to become usable on Kaggle; if the push below says it cannot find it, wait and run `push` again.")
    code, _out = run(["kaggle", "kernels", "push", "-p", kernel], args.dry_run)
    if code == 0:
        print(f"Started. Watch it with: python scripts/kaggle_gpu/kaggle_run.py status --username {args.username} --wait")
    return code


def status(args):
    if not preflight(args.dry_run):
        return 2
    reference = f"{args.username}/{KERNEL_SLUG}"
    while True:
        code, out = run(["kaggle", "kernels", "status", reference], args.dry_run)
        if not args.wait or args.dry_run or code or any(state in out.upper() for state in DONE_STATES):
            return code
        time.sleep(30)


def fetch(args):
    if not preflight(args.dry_run):
        return 2
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    code, _out = run(["kaggle", "kernels", "output", f"{args.username}/{KERNEL_SLUG}", "-p", out], args.dry_run)
    if code == 0:
        print(f"Results in {out}. Read log.txt first, then: python scripts/kaggle_gpu/score_results.py --kit KIT --results {out}")
    return code


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, function in (("push", push), ("status", status), ("fetch", fetch)):
        command = sub.add_parser(name)
        command.add_argument("--username", required=True, help="Your Kaggle user name.")
        command.add_argument("--dry-run", action="store_true", help="Only print the commands.")
        command.set_defaults(function=function)
        if name == "push":
            command.add_argument("--kit", required=True)
        if name == "status":
            command.add_argument("--wait", action="store_true", help="Poll every 30 s until the run finishes.")
        if name == "fetch":
            command.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
