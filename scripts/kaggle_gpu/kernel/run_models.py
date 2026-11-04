"""Runs ON KAGGLE (a GPU notebook of kernel type "script", internet on): restores the degraded faces of the kit with each model's own inference command.

NOT RUN ON KAGGLE YET. Every command below is the one in that model's README (read 2026-11-04) and the install lines are the usual ones, but Kaggle's Python / torch / CUDA versions move, so the first
run is expected to need a fix or two. That is why every model runs on its own, a failure is logged and the next model still runs, and ``log.txt`` (all output) is downloaded with the results.

Input: the kit's dataset (``config.json``, ``lq/<kind>/<id>.png``). Output in /kaggle/working: ``out/<model>/<kind>/<id>.png``, ``timings.json``, ``summary.json``, ``log.txt``.
"""
import json
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

WORK = Path("/kaggle/working")
SRC = WORK / "src"
OUT = WORK / "out"
SCRATCH = WORK / "scratch"
LOG = WORK / "log.txt"
NEG_PROMPT = "low quality, blurry, low-resolution, noisy, unsharp, weird textures"


def log(message):
    print(message, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def sh(command, cwd=None):
    """Run a shell command, append everything it prints to the log, and return its exit code."""
    log(f"$ {command}")
    process = subprocess.run(command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(process.stdout[-20000:] + f"\n[exit {process.returncode}]\n")
    return process.returncode


def find_kit():
    for config in sorted(Path("/kaggle/input").rglob("config.json")):
        if (config.parent / "lq").is_dir():
            return config.parent
    raise SystemExit("The kit's dataset is not attached to this notebook (no config.json with an lq folder under /kaggle/input).")


def patch_torchvision(root):
    """torchvision 0.17 removed ``transforms.functional_tensor``, which BasicSR (also the copy CodeFormer carries) still imports."""
    changed = 0
    for path in Path(root).rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "torchvision.transforms.functional_tensor" in text:
            path.write_text(text.replace("torchvision.transforms.functional_tensor", "torchvision.transforms.functional"), encoding="utf-8")
            changed += 1
    return changed


def installed_package_dir(name):
    result = subprocess.run([sys.executable, "-c", f"import {name}, os; print(os.path.dirname({name}.__file__))"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return result.stdout.strip() or None


def clone(name, url):
    target = SRC / name
    if not target.exists():
        sh(f"git clone --depth 1 {url} {target}")
    return target


def stage(kit, kind, ids):
    """A flat folder of this degradation's images for one tool."""
    folder = SCRATCH / "in" / kind
    folder.mkdir(parents=True, exist_ok=True)
    for name in ids:
        source = kit / "lq" / kind / f"{name}.png"
        if source.is_file():
            (folder / source.name).write_bytes(source.read_bytes())
    return folder


def collect(tool_output, destination, ids):
    """Copy each id's restored image out of a tool's output tree (tools differ in sub-folders and suffixes): ``restored_faces`` and ``final`` results are preferred."""
    destination.mkdir(parents=True, exist_ok=True)
    files = sorted(Path(tool_output).rglob("*.png")) + sorted(Path(tool_output).rglob("*.jpg"))
    found = 0
    for name in ids:
        matches = [path for path in files if path.stem == name or path.stem.startswith(f"{name}_")]
        matches.sort(key=lambda path: (0 if "restored_faces" in path.parts or "final_results" in path.parts else 1, len(path.stem)))
        if matches:
            (destination / f"{name}.png").write_bytes(matches[0].read_bytes())
            found += 1
    return found


def run_variant(label, kit, kinds, ids, command_for, timings, summary):
    """``command_for(input_folder, output_folder)`` is the tool's command for one degradation; images are timed per variant."""
    for kind in kinds:
        started = time.time()
        input_folder, output_folder = stage(kit, kind, ids), SCRATCH / "out" / label / kind
        command, directory = command_for(input_folder, output_folder)
        status = sh(command, cwd=directory)
        found = collect(output_folder, OUT / label / kind, ids)
        seconds = time.time() - started
        timings.setdefault(label, {})[kind] = {"seconds": round(seconds, 1), "images": found, "seconds_per_image": round(seconds / max(found, 1), 2)}
        summary.setdefault(label, {})[kind] = {"exit": status, "restored": found, "of": len(ids)}
        log(f"{label} / {kind}: {found} of {len(ids)} restored in {seconds:.0f} s (exit {status})")


def codeformer(kit, kinds, ids, timings, summary):
    root = clone("CodeFormer", "https://github.com/sczhou/CodeFormer")
    patch_torchvision(root)
    sh("pip install -q addict lmdb yapf lpips scikit-image gdown", cwd=root)
    sh("python basicsr/setup.py develop --no_cuda_ext", cwd=root)
    sh("python scripts/download_pretrained_models.py facelib", cwd=root)
    sh("python scripts/download_pretrained_models.py CodeFormer", cwd=root)
    for weight in (1.0, 0.7):
        run_variant(f"codeformer_w{weight}", kit, kinds, ids, lambda i, o, w=weight: (f"python inference_codeformer.py -w {w} --has_aligned --input_path {i} --output_path {o}", root), timings, summary)


def gfpgan(kit, kinds, ids, timings, summary):
    root = clone("GFPGAN", "https://github.com/TencentARC/GFPGAN")
    sh("pip install -q basicsr facexlib gfpgan", cwd=root)
    package = installed_package_dir("basicsr")
    if package:
        patch_torchvision(package)
    sh("mkdir -p experiments/pretrained_models && wget -q https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth -P experiments/pretrained_models", cwd=root)
    run_variant("gfpgan_v1.4", kit, kinds, ids, lambda i, o: (f"python inference_gfpgan.py -i {i} -o {o} -v 1.4 -s 1 --aligned", root), timings, summary)


def diffbir(kit, kinds, ids, timings, summary):
    root = clone("DiffBIR", "https://github.com/XPixelGroup/DiffBIR")
    sh("pip install -q -r requirements.txt", cwd=root)
    command = ("python -u inference.py --task face --upscale 1 --version v2 --sampler spaced --steps 50 --captioner none --pos_prompt '' "
               f"--neg_prompt '{NEG_PROMPT}' --cfg_scale 4.0 --input {{i}} --output {{o}} --device cuda --precision fp32")
    run_variant("diffbir_v2", kit, kinds, ids, lambda i, o: (command.format(i=i, o=o), root), timings, summary)


def pmrf(kit, kinds, ids, timings, summary):
    """Experimental: the README pins torch 2.3 / CUDA 11.8 and a matching ``natten`` wheel, which Kaggle's image probably does not have."""
    root = clone("PMRF", "https://github.com/ohayonguy/PMRF")
    sh("pip install -q opencv-python timm einops dctorch torch-ema torch-fidelity lpips piq huggingface_hub natten", cwd=root)
    package = installed_package_dir("basicsr")
    if package:
        patch_torchvision(package)
    run_variant("pmrf_k25", kit, kinds, ids, lambda i, o: (f"python inference.py --ckpt_path ohayonguy/PMRF_blind_face_image_restoration --ckpt_path_is_huggingface --lq_data_path {i} --output_dir {o} --batch_size 16 --num_flow_steps 25", root), timings, summary)


MODELS = {"codeformer": codeformer, "gfpgan": gfpgan, "diffbir": diffbir, "pmrf": pmrf}


def main():
    for folder in (SRC, OUT, SCRATCH):
        folder.mkdir(parents=True, exist_ok=True)
    kit = find_kit()
    config = json.loads((kit / "config.json").read_text(encoding="utf-8"))
    ids, kinds = config["ids"], config["kinds"]
    log(f"kit {kit}: {len(ids)} faces, degradations {kinds}, models {config['models']}")
    sh("nvidia-smi | head -15; python -c \"import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())\"")
    timings, summary = {}, {}
    for name in config["models"]:
        log(f"===== {name}")
        try:
            MODELS[name](kit, kinds, ids, timings, summary)
        except Exception:  # one model failing must not stop the others; the traceback is in the log
            log(traceback.format_exc())
            summary.setdefault(name, {})["error"] = traceback.format_exc().splitlines()[-1]
        (WORK / "timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
        (WORK / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    shutil.rmtree(SCRATCH, ignore_errors=True)          # the intermediate copies; the results are in out/
    log("done")


if __name__ == "__main__":
    main()
