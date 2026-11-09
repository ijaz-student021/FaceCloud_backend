# Evidence log — 2026-10-14

Everything ran on an Apple M1 (macOS), at commit `464e5ae`, with the
uncommitted `scripts/evaluate_pipeline_cases.py` changes preserved. Every
database used was a throwaway; the local `db.sqlite3` was read only.

## Environments

| | Web (`venv/`) | Worker (`.runtime/venvs/gfpgan`) |
|---|---|---|
| Python | 3.14 | 3.12 |
| Django / DRF / Celery | 6.1.2 / 3.18.3 / 5.6.3 | same |
| torch | 2.14.0 (MPS) | 2.14.0 (MPS) |
| OpenCV / numpy | 5.0.0 / 2.5.4 | same |
| gfpgan / basicsr / facexlib | – | 1.3.8 / 1.4.2 / 0.3.0 |

## Checks (`checks.log`)

| Check | Result |
|---|---|
| `manage.py check` | No issues |
| `makemigrations --check --dry-run` | No changes |
| `showmigrations` on local `db.sqlite3` | **91 unapplied**: the local dev DB is not migrated (not touched) |
| OpenAPI schema `--validate` | exit 0 |
| `uv pip check` (both envs) | Compatible |
| `scripts/model_manifest.py verify` | 12 OK, 1 missing (private curve), 2 extra files in `provenance/` |
| Tests, web env | 1407 OK, 4 skipped |
| Tests, worker env incl. slow | 1411 OK, 3 skipped |
| Tests, throwaway PostgreSQL | 1407 OK |
| `pip-audit` web env | No known vulnerabilities |
| `pip-audit` worker env | basicsr 1.4.2 PYSEC-2026-1215, no fix version (`pip-audit.txt`) |

## Queue re-run (`queue-rerun.log`) [queue]

`scripts/check_queue_integration.py`, run directory
`.runtime/queue_integration/2026-10-14-audit-rerun`; own Redis, own PostgreSQL
database (dropped afterwards), real Celery workers in both environments.
All five scenarios passed (routing/full flow, duplicate delivery, DB connection
killed, cancellation mid-batch, worker SIGKILL; recovery 108 s). Not covered:
reaper, a killed batch worker, a long-running duplicate (P1-2), Redis failure.

## Probes [classical] / [MPS]

| Probe | Result |
|---|---|
| 16-bit grayscale PNG gradient through upload → preprocess → enhance | 3 grey levels out (P1-1); 16-bit RGB: 256 levels |
| ICC-tagged JPEG through upload | Profile dropped, no conversion (P2-5) |
| Laplacian sharpness vs resolution, same photo | Harsh-sun portrait 523 @800 px → 106 @6000 px; still life 968 → 69 (`mild_blur` at 24 MP) |
| Laplacian sharpness vs exposure | Sharp photo ×0.25 → `severe_blur` |
| Warm loader cost per call | GFPGAN 471 ms, NAFNet 244, colorization 87, Real-ESRGAN 41, PureVision 36, PureScale 12, YuNet 3.8, SFace + healthcheck 44 |

## Unpaginated generic list views [static]

No `DEFAULT_PAGINATION_CLASS`; these set no `pagination_class`:
`organizations/views.py:40, 52`; `enhancement/views.py:210, 230, 648`;
`sharing/views.py:36`; `uploads/views.py:104, 158`; `quality_control/views.py:34`;
`accounts/views.py:870`; `identities/views.py:100, 123, 235`;
`downloads/views.py:90, 110`; `duplicates/views.py:60, 82`;
`notifications/views.py:22`; `reports/views.py:98`.
Paginated: collages, my-images, failed batches, event photos.

## Other settings facts [static]

No `CACHES` (LocMem, per-process throttles). No Dockerfile, compose or Procfile.
`CELERY_TASK_ROUTES` is None locally (GFPGAN queue off). Encryption at rest off
locally. Share tokens stored as SHA-256 hashes. Local `.env`: DEBUG on, SQLite,
eager Celery; values not reproduced here.

## Not done

No CUDA run. No real-photo ground-truth set. No frontend audit. No load test.
