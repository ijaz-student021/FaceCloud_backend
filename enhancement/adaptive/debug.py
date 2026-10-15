"""Development comparison mode: keep the stages a run actually executed, protected and temporary.

Off in production (``ADAPTIVE_DEBUG_ARTIFACTS=False``). When on, a run writes, through the default storage (so they are
encrypted at rest and, with ``STORAGE_BACKEND=s3``, private in the bucket) under ``debug/enhancement/<run id>/``:

    original.png, 01_quality_analysis.json, 02_low_light.png, 03_denoised.png, 04_deblurred.png, 05_face_restored.png,
    06_upscaled.png, 07_final.png, candidates.json

Only stages that executed and were accepted have a file; ``candidates.json`` lists every candidate, rejected ones included,
with the model, checkpoint, device, timing, quality and identity verdict. No endpoint serves these files: they are read from
the storage by an operator. ``manage.py cleanup_enhancement_debug`` deletes runs older than
``ADAPTIVE_DEBUG_RETENTION_HOURS``. The production API returns only the final image and the outcome.
"""
import json
import logging
import time

import cv2
import numpy as np
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)
ROOT = "debug/enhancement"


def _plain(value):
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


class DebugRecorder:
    def __init__(self, enabled, run_id):
        self.enabled = bool(enabled)
        self.run_id = run_id

    @property
    def location(self):
        return f"{ROOT}/{self.run_id}/" if self.enabled else None

    def _save(self, name, payload):
        path = f"{ROOT}/{self.run_id}/{name}"
        try:
            default_storage.save(path, ContentFile(payload))
        except Exception:  # a debug write must never fail an enhancement
            logger.exception("Could not write the debug artifact %s.", path)

    def image(self, name, image):
        if self.enabled:
            ok, encoded = cv2.imencode(".png", image)
            if ok:
                self._save(f"{name}.png", encoded.tobytes())

    def json(self, name, data):
        if self.enabled:
            self._save(f"{name}.json", json.dumps(data, indent=2, default=_plain).encode())


def cleanup(older_than_hours=None, now=None):
    """Delete debug runs older than the retention; returns the number of runs removed."""
    hours = float(settings.ADAPTIVE_DEBUG_RETENTION_HOURS if older_than_hours is None else older_than_hours)
    cutoff = (now or time.time()) - hours * 3600
    removed = 0
    try:
        runs, _files = default_storage.listdir(ROOT)
    except (FileNotFoundError, OSError):
        return 0
    for run in runs:
        folder = f"{ROOT}/{run}"
        _dirs, files = default_storage.listdir(folder)
        newest = max((default_storage.get_modified_time(f"{folder}/{name}").timestamp() for name in files), default=0)
        if newest and newest < cutoff:
            for name in files:
                default_storage.delete(f"{folder}/{name}")
            removed += 1
    return removed
