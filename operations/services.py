import hashlib
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from functools import lru_cache
from pathlib import Path

import redis
from django.conf import settings

from enhancement.policy import learned_stage_enabled
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.cache.backends.locmem import LocMemCache
from django.core.mail import get_connection
from django.db import connection, connections
from django.utils import timezone

from config.celery import app as celery_app
from uploads.models import UploadBatch


HEALTHY = "healthy"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"


def _elapsed_ms(started):
    return round((time.perf_counter() - started) * 1000, 2)


def _result(status, started, **details):
    return {"status": status, "latency_ms": _elapsed_ms(started), **details}


def check_api():
    started = time.perf_counter()
    return _result(HEALTHY, started)


def check_database():
    started = time.perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return _result(HEALTHY, started)
    except Exception:
        return _result(UNAVAILABLE, started, code="connection_failed")


def check_redis():
    started = time.perf_counter()
    client = None
    try:
        client = redis.Redis.from_url(
            settings.CELERY_BROKER_URL,
            socket_connect_timeout=settings.SYSTEM_HEALTH_TIMEOUT_SECONDS,
            socket_timeout=settings.SYSTEM_HEALTH_TIMEOUT_SECONDS,
        )
        client.ping()
        return _result(HEALTHY, started)
    except Exception:
        return _result(UNAVAILABLE, started, code="connection_failed")
    finally:
        if client is not None:
            client.close()


def check_celery():
    started = time.perf_counter()
    try:
        replies = celery_app.control.inspect(
            timeout=settings.SYSTEM_HEALTH_TIMEOUT_SECONDS
        ).ping()
        workers = len(replies or {})
        if workers == 0:
            return _result(DEGRADED, started, code="no_workers", workers=0)
        return _result(HEALTHY, started, workers=workers)
    except Exception:
        return _result(DEGRADED, started, code="inspection_failed", workers=0)


def check_storage():
    started = time.perf_counter()
    probe_name = f"health/.probe-{uuid.uuid4().hex}"
    saved_name = None
    try:
        saved_name = default_storage.save(probe_name, ContentFile(b"ok"))
        if not default_storage.exists(saved_name):
            return _result(UNAVAILABLE, started, code="write_verification_failed")
        details = {}
        if hasattr(default_storage, "path"):
            try:
                details["available_bytes"] = shutil.disk_usage(
                    default_storage.path("")
                ).free
            except (NotImplementedError, OSError):
                pass
        return _result(HEALTHY, started, **details)
    except Exception:
        return _result(UNAVAILABLE, started, code="write_failed")
    finally:
        if saved_name:
            try:
                default_storage.delete(saved_name)
            except Exception:
                pass


def check_email():
    started = time.perf_counter()
    connection_instance = None
    try:
        connection_instance = get_connection()
        opened = connection_instance.open()
        if opened is False:
            return _result(DEGRADED, started, code="connection_failed")
        return _result(HEALTHY, started)
    except Exception:
        return _result(DEGRADED, started, code="connection_failed")
    finally:
        if connection_instance is not None:
            try:
                connection_instance.close()
            except Exception:
                pass


@lru_cache(maxsize=32)
def _sha256_for_file(path, modified_ns, size):
    del modified_ns, size
    digest = hashlib.sha256()
    with open(path, "rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_state(path_value, expected_checksum):
    """``(status, details)`` for a model file: healthy, or degraded with a code. Never raises."""
    if not path_value:
        return DEGRADED, {"code": "not_configured"}
    try:
        path = Path(path_value)
        stat = path.stat()
        if not path.is_file() or stat.st_size == 0:
            return DEGRADED, {"code": "missing"}
        if expected_checksum:
            actual = _sha256_for_file(str(path), stat.st_mtime_ns, stat.st_size)
            if actual.lower() != expected_checksum.strip().lower():
                return DEGRADED, {"code": "checksum_mismatch"}
        return HEALTHY, {"size_bytes": stat.st_size}
    except (OSError, ValueError):
        return DEGRADED, {"code": "unreadable"}


def _model_status(path_value, expected_checksum, enabled=True, disabled_reason=""):
    """A model that can run must be present and intact. One that is switched off is not a fault, but it
    still reports whether its file is installed and verified, so a disabled model can be shown to be there."""
    state, details = _file_state(path_value, expected_checksum)
    if enabled:
        return {"status": state, **details}
    return {
        "status": "disabled",
        "installed": state == HEALTHY,
        "checksum_verified": state == HEALTHY and bool(expected_checksum),
        **({"reason": disabled_reason} if disabled_reason else {}),
        **({"code": details["code"]} if state != HEALTHY else {}),
    }


def check_models():
    started = time.perf_counter()
    from enhancement import policy

    off = f"disabled by the {policy.active_policy()} rendering policy" if policy.active_policy() == policy.LIGHTWEIGHT else "its setting is off"
    definitions = {
        "yunet": (settings.YUNET_MODEL_PATH, settings.YUNET_MODEL_SHA256, True),
        "sface": (settings.IDENTITY_MODEL_PATH, settings.IDENTITY_MODEL_SHA256, True),
        "zero_dce": (settings.ZERO_DCE_MODEL_PATH, settings.ZERO_DCE_MODEL_SHA256, learned_stage_enabled("ZERO_DCE_ENABLED")),
        "nafnet": (settings.NAFNET_MODEL_PATH, settings.NAFNET_MODEL_SHA256, learned_stage_enabled("NAFNET_DEBLUR_ENABLED")),
        "nafnet_sidd": (settings.NAFNET_SIDD_MODEL_PATH, settings.NAFNET_SIDD_MODEL_SHA256, learned_stage_enabled("NAFNET_DENOISE_ENABLED")),
        "retinexformer": (settings.RETINEXFORMER_MODEL_PATH, settings.RETINEXFORMER_MODEL_SHA256, learned_stage_enabled("RETINEXFORMER_ENABLED")),
        "codeformer": (settings.CODEFORMER_MODEL_PATH, settings.CODEFORMER_MODEL_SHA256, learned_stage_enabled("CODEFORMER_ENABLED")),
        "arcface": (settings.ARCFACE_MODEL_PATH, settings.ARCFACE_MODEL_SHA256, settings.IDENTITY_PROVIDER == "arcface_w600k_r50_v1"),
        "retinaface": (settings.GFPGAN_DETECTION_MODEL_PATH, settings.GFPGAN_DETECTION_MODEL_SHA256, settings.FACE_DETECTOR_PROVIDER == "retinaface_v1"),
        "realesrgan": (
            settings.REALESRGAN_MODEL_PATH, settings.REALESRGAN_MODEL_SHA256,
            learned_stage_enabled("HD_RESTORATION_ENABLED") and bool(settings.REALESRGAN_MODEL_PATH),
        ),
        "gfpgan": (
            settings.GFPGAN_MODEL_PATH, settings.GFPGAN_MODEL_SHA256,
            learned_stage_enabled("GFPGAN_ENABLED") and bool(settings.GFPGAN_MODEL_PATH),
        ),
    }
    models = {
        name: _model_status(path, checksum, enabled, "" if enabled else off)
        for name, (path, checksum, enabled) in definitions.items()
    }
    status = DEGRADED if any(
        item["status"] == DEGRADED for item in models.values()
    ) else HEALTHY
    return _result(status, started, models=models, render_policy=policy.active_policy())


def job_counts():
    return {
        "pending": UploadBatch.objects.filter(
            processing_status__in=["pending", "queued"]
        ).count(),
        "active": UploadBatch.objects.filter(processing_status="processing").count(),
        "failed": UploadBatch.objects.filter(processing_status="failed").count(),
    }


CHECKS = {
    "api": check_api,
    "database": check_database,
    "redis": check_redis,
    "celery": check_celery,
    "storage": check_storage,
    "email": check_email,
    "models": check_models,
}

REQUIRED_CHECKS = {"database", "redis", "storage"}

PROCESS_STARTED_AT = timezone.now()
PROCESS_STARTED_MONOTONIC = time.monotonic()

# How a check reads on the administrator's System Health screen: a name, and the word for its state.
SERVICE_NAMES = {
    "api": "API", "database": "Database", "redis": "Cache and message broker", "celery": "Background workers",
    "storage": "File storage", "email": "Email", "models": "AI models",
}
SERVICE_STATES = {HEALTHY: "operational", DEGRADED: "degraded", UNAVAILABLE: "down"}


def _services(checks, jobs, checked_at):
    """One row per check for the System Health screen: ``key``, ``name``, ``status`` (operational / degraded / down), the latency and, for the
    workers, how much work is waiting. Nothing here is measured that the checks did not already measure (no CPU or memory figures)."""
    waiting = None if jobs.get("pending") is None else jobs["pending"] + jobs["active"]
    rows = []
    for key, result in checks.items():
        row = {
            "key": key, "name": SERVICE_NAMES.get(key, key), "status": SERVICE_STATES.get(result["status"], result["status"]),
            "latency_ms": result.get("latency_ms"), "checked_at": checked_at,
            "detail": result.get("code") or result.get("reason") or "",
        }
        if key == "celery":
            row["queue_size"] = waiting
            row["workers"] = result.get("workers")
        if key == "storage":
            row["available_bytes"] = result.get("available_bytes")
        rows.append(row)
    return rows


def _failed_check(name, code, latency_ms=0.0):
    return {"status": UNAVAILABLE if name in REQUIRED_CHECKS else DEGRADED, "latency_ms": latency_ms, "code": code}


def _run_check(name, checker):
    """One check, in its own thread: a failure is the check's own result, and the thread's database connection is closed when it ends."""
    try:
        return checker()
    except Exception:
        return _failed_check(name, "check_failed")
    finally:
        connections.close_all()


def run_checks():
    """Every check at the same time, waiting at most ``SYSTEM_HEALTH_DEADLINE_SECONDS`` for all of them.

    They used to run one after another, so a stopped Redis made the answer wait for each timeout in turn (6 to 18 seconds; the worker inspection alone waits about 6). A check that has
    not answered by the deadline is reported as failed with the code ``timeout`` (``unavailable`` for a required service, ``degraded`` for the others) and its thread is left to finish
    on its own.
    """
    deadline = settings.SYSTEM_HEALTH_DEADLINE_SECONDS
    pool = ThreadPoolExecutor(max_workers=max(len(CHECKS), 1), thread_name_prefix="system-health")
    futures = {name: pool.submit(_run_check, name, checker) for name, checker in CHECKS.items()}
    done, _late = wait(futures.values(), timeout=deadline)
    pool.shutdown(wait=False, cancel_futures=True)
    return {
        name: future.result() if future in done else _failed_check(name, "timeout", round(deadline * 1000, 2))
        for name, future in futures.items()
    }


# The finished answer is kept for a few seconds, so several admin screens and the bell asking at once cost one round of checks. It lives in this process's memory on purpose: the cache
# server may be the very thing that is down, and asking it would be one more thing to wait for.
_health_cache = LocMemCache("system-health", {"TIMEOUT": None, "OPTIONS": {"MAX_ENTRIES": 4}})
HEALTH_CACHE_KEY = "system-health"


def clear_health_cache():
    _health_cache.clear()


def build_system_health(use_cache=False):
    """The system health answer. ``use_cache`` (what the endpoint passes) returns a copy made in the last ``SYSTEM_HEALTH_CACHE_SECONDS`` seconds, if there is one."""
    seconds = settings.SYSTEM_HEALTH_CACHE_SECONDS if use_cache else 0
    if seconds > 0:
        cached = _health_cache.get(HEALTH_CACHE_KEY)
        if cached is not None:
            return cached
    health = _build_system_health()
    if seconds > 0:
        _health_cache.set(HEALTH_CACHE_KEY, health, timeout=seconds)
    return health


def _build_system_health():
    started = time.perf_counter()
    checks = run_checks()

    if any(checks[name]["status"] == UNAVAILABLE for name in REQUIRED_CHECKS):
        overall = UNAVAILABLE
    elif any(result["status"] != HEALTHY for result in checks.values()):
        overall = DEGRADED
    else:
        overall = HEALTHY

    try:
        jobs = job_counts()
    except Exception:
        jobs = {"pending": None, "active": None, "failed": None}
        if overall == HEALTHY:
            overall = DEGRADED

    checked_at = timezone.now().isoformat()
    return {
        "status": overall,
        "version": settings.FACECLOUD_VERSION,
        "checked_at": checked_at,
        "response_time_ms": _elapsed_ms(started),
        "checks": checks,
        "jobs": jobs,
        "services": _services(checks, jobs, checked_at),
        "process": {
            "started_at": PROCESS_STARTED_AT.isoformat(),
            "uptime_seconds": int(time.monotonic() - PROCESS_STARTED_MONOTONIC),
        },
    }

