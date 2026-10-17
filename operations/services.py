import hashlib
import shutil
import time
import uuid
from functools import lru_cache
from pathlib import Path

import redis
from django.conf import settings

from enhancement.policy import learned_stage_enabled
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.mail import get_connection
from django.db import connection
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
        "edsr": (
            settings.AI_RESTORATION_EDSR_MODEL_PATH, settings.AI_RESTORATION_EDSR_MODEL_SHA256,
            learned_stage_enabled("AI_RESTORATION_ENABLED"),
        ),
        "nafnet": (settings.NAFNET_MODEL_PATH, settings.NAFNET_MODEL_SHA256, learned_stage_enabled("NAFNET_DEBLUR_ENABLED")),
        "realesrgan": (
            settings.REALESRGAN_MODEL_PATH, settings.REALESRGAN_MODEL_SHA256,
            learned_stage_enabled("HD_RESTORATION_ENABLED") and bool(settings.REALESRGAN_MODEL_PATH),
        ),
        "gfpgan": (
            settings.GFPGAN_MODEL_PATH, settings.GFPGAN_MODEL_SHA256,
            learned_stage_enabled("GFPGAN_ENABLED") and bool(settings.GFPGAN_MODEL_PATH),
        ),
        "purevision": (settings.PUREVISION_MODEL_PATH, settings.PUREVISION_MODEL_SHA256, learned_stage_enabled("PUREVISION_ENABLED")),
        "purescale": (settings.PURESCALE_MODEL_PATH, settings.PURESCALE_MODEL_SHA256, learned_stage_enabled("PURESCALE_ENABLED")),
        "colorization": (
            settings.COLORIZATION_MODEL_PATH, settings.COLORIZATION_MODEL_SHA256, learned_stage_enabled("COLORIZATION_ENABLED"),
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


def build_system_health():
    started = time.perf_counter()
    checks = {}
    for name, checker in CHECKS.items():
        try:
            checks[name] = checker()
        except Exception:
            checks[name] = {
                "status": UNAVAILABLE if name in REQUIRED_CHECKS else DEGRADED,
                "latency_ms": 0.0,
                "code": "check_failed",
            }

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

    return {
        "status": overall,
        "version": settings.FACECLOUD_VERSION,
        "checked_at": timezone.now().isoformat(),
        "response_time_ms": _elapsed_ms(started),
        "checks": checks,
        "jobs": jobs,
    }

