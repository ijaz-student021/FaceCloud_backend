"""Read-only readiness check to run first on the GPU machine.

    python scripts/gpu_preflight.py --expect-gpu
    python scripts/gpu_preflight.py --expect-gpu --gpu-smoke   # also run a tiny CUDA computation

It reports the Python, PyTorch and GPU in use, whether every model file exists and
matches its pinned checksum, which enhancement stages are switched on, and whether
PostgreSQL, Redis and enough disk space are available. It changes nothing.

Exit code 0 means no FAIL. A missing model for a stage that is switched off is only a
WARN (it must be fixed before that stage is enabled); a missing model for a switched-on
stage, a checksum mismatch, or (with --expect-gpu) no CUDA device is a FAIL.
"""
import argparse
import hashlib
import os
import platform
import shutil
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
MIN_FREE_GB = 20
CHUNK = 4 * 1024 * 1024


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def judge_model(path, expected_sha256, needed, verify=True):
    """``(status, note)`` for one model file; ``needed`` means its stage is switched on."""
    path = Path(path)
    if not path.is_file():
        return (FAIL if needed else WARN), ("missing (needed now)" if needed else "missing (needed before enabling)")
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        return WARN, "present, no checksum pinned"
    if not verify:
        return PASS, "present (checksum not verified)"
    return (PASS, "checksum matches") if sha256_of(path) == expected else (FAIL, "CHECKSUM MISMATCH")


def model_table(settings):
    """One row per model file: ``(stage, flag_on, path, expected sha256 or "")``."""
    def on(name):
        return bool(getattr(settings, name, False))

    return [
        ("zero_dce", on("ZERO_DCE_ENABLED"), settings.ZERO_DCE_MODEL_PATH, ""),
        ("yunet_detector", True, settings.YUNET_MODEL_PATH, settings.YUNET_MODEL_SHA256),
        ("sface_identity", on("IDENTITY_AUTO_ANALYZE") or on("IDENTITY_GUARD_ENABLED"),
         settings.IDENTITY_MODEL_PATH, settings.IDENTITY_MODEL_SHA256),
        ("nafnet_deblur", on("HD_RESTORATION_ENABLED") and on("NAFNET_DEBLUR_ENABLED"),
         settings.NAFNET_MODEL_PATH, settings.NAFNET_MODEL_SHA256),
        ("realesrgan_hd", on("HD_RESTORATION_ENABLED"), settings.REALESRGAN_MODEL_PATH, settings.REALESRGAN_MODEL_SHA256),
        ("gfpgan", on("GFPGAN_ENABLED"), settings.GFPGAN_MODEL_PATH, settings.GFPGAN_MODEL_SHA256),
        ("gfpgan_detector", on("GFPGAN_ENABLED"), settings.GFPGAN_DETECTION_MODEL_PATH, settings.GFPGAN_DETECTION_MODEL_SHA256),
        ("gfpgan_parser", on("GFPGAN_ENABLED"), settings.GFPGAN_PARSING_MODEL_PATH, settings.GFPGAN_PARSING_MODEL_SHA256),
    ]


def check_torch(expect_gpu, smoke):
    rows = []
    try:
        import torch
    except Exception as error:  # noqa: BLE001 - report any import problem
        return [(FAIL, "PyTorch", f"cannot import: {error}")]
    rows.append((PASS, "PyTorch", f"{torch.__version__} (CUDA build: {torch.version.cuda or 'none, CPU build'})"))
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        rows.append((PASS, "GPU", f"{props.name}, {props.total_memory / 1024 ** 3:.1f} GB, "
                                  f"{torch.cuda.device_count()} device(s)"))
        if smoke:
            try:
                value = float((torch.ones(1024, 1024, device="cuda") @ torch.ones(1024, 1024, device="cuda")).sum())
                torch.cuda.synchronize()
                rows.append((PASS if value == 1024.0 ** 3 else FAIL, "CUDA smoke test", f"matmul result {value:.0f}"))
            except Exception as error:  # noqa: BLE001
                rows.append((FAIL, "CUDA smoke test", str(error)))
    else:
        rows.append((FAIL if expect_gpu else WARN, "GPU", "CUDA is not available to PyTorch"))
    return rows


def check_services(settings):
    rows = []
    try:
        from django.db import connection

        connection.ensure_connection()
        rows.append((PASS, "PostgreSQL", f"{connection.settings_dict['NAME']} on {connection.settings_dict['HOST'] or 'local'}"))
    except Exception as error:  # noqa: BLE001
        rows.append((FAIL, "PostgreSQL", str(error).splitlines()[0][:120]))
    broker = urlparse(str(settings.CELERY_BROKER_URL))
    try:
        with socket.create_connection((broker.hostname or "127.0.0.1", broker.port or 6379), timeout=2):
            rows.append((PASS, "Redis (Celery broker)", f"{broker.hostname}:{broker.port or 6379} reachable"))
    except OSError as error:
        eager = bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False))
        rows.append((WARN if eager else FAIL, "Redis (Celery broker)",
                     f"not reachable ({error.__class__.__name__})" + ("; eager mode is on" if eager else "")))
    free_gb = shutil.disk_usage(ROOT).free / 1024 ** 3
    rows.append((PASS if free_gb >= MIN_FREE_GB else WARN, "Disk space", f"{free_gb:.0f} GB free (want {MIN_FREE_GB}+)"))
    return rows


def check_stages(settings):
    rows = []
    for name, label in (
        ("ZERO_DCE_ENABLED", "Zero-DCE exposure"), ("ADAPTIVE_DENOISE_ENABLED", "Adaptive denoise"),
        ("HD_RESTORATION_ENABLED", "HD restoration (Real-ESRGAN)"), ("NAFNET_DEBLUR_ENABLED", "NAFNet deblur"),
        ("GFPGAN_ENABLED", "GFPGAN face restoration"), ("GFPGAN_QUEUE_ENABLED", "GFPGAN on its own queue"),
        ("IDENTITY_GUARD_ENABLED", "Identity guard"),
    ):
        rows.append((PASS, label, "ON" if getattr(settings, name, False) else "off"))
    return rows


def run(expect_gpu=False, smoke=False, verify_checksums=True):
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    sys.path.insert(0, str(ROOT))
    django.setup()
    from django.conf import settings

    sections = [
        ("Environment", [(PASS, "Python", f"{platform.python_version()} on {platform.system()} {platform.machine()}")]
         + check_torch(expect_gpu, smoke)),
        ("Stages (from the current environment)", check_stages(settings)),
    ]
    models = []
    for stage, needed, path, sha in model_table(settings):
        status, note = judge_model(path, sha, needed, verify_checksums)
        models.append((status, f"{stage}{'' if needed else ' (off)'}", f"{note}  [{Path(path).name}]"))
    sections.append(("Model files", models))
    sections.append(("Services", check_services(settings)))
    return sections


def render(sections):
    lines, worst = [], PASS
    for title, rows in sections:
        lines.append(f"\n== {title} ==")
        for status, name, note in rows:
            lines.append(f"  [{status}] {name:<34} {note}")
            if status == FAIL:
                worst = FAIL
            elif status == WARN and worst == PASS:
                worst = WARN
    return "\n".join(lines), worst


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expect-gpu", action="store_true", help="Treat a missing CUDA device as a failure.")
    parser.add_argument("--gpu-smoke", action="store_true", help="Run a small computation on the GPU.")
    parser.add_argument("--skip-checksums", action="store_true", help="Do not hash the model files (faster).")
    args = parser.parse_args(argv)
    text, worst = render(run(args.expect_gpu, args.gpu_smoke, not args.skip_checksums))
    print(text)
    print(f"\nResult: {worst}")
    return 1 if worst == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
