# FaceCloud Backend

FaceCloud is a facial-image platform: users upload photos, the backend judges
their quality, enhances them (low-light recovery, denoising, upscaling, face
restoration, colorization, style presets), groups the faces it finds by
identity, and keeps everything private to the owner. This repository is the
Django REST Framework API and the Celery workers behind it.

It is the backend of a final-year project. Every feature has a document under
[`docs/features`](docs/features) that records what was built, how it was
verified, and what its known limits are.

## What it does

| Area | Summary |
|---|---|
| Accounts | Registration, email verification, JWT login with refresh rotation, password reset, logout from all devices, admin / moderator / user roles, suspension, audit trail |
| Uploads | Single, multiple and ZIP upload; content-based JPG/PNG validation; ZIP-slip and ZIP-bomb protection; UUID storage paths; owner-only access |
| Quality control | Sharpness, exposure, contrast, noise and resolution scoring with explainable accept / review / reject decisions |
| Enhancement | Adaptive pipeline: Zero-DCE exposure, adaptive denoise, EDSR / NAFNet / Real-ESRGAN / PureScale / PureVision restoration, GFPGAN face restoration, sharpening, colorization, style presets and sliders |
| Identity | YuNet face detection, SFace embeddings, identity grouping, a guard that rejects enhancements that change who a face looks like, ZIP export per identity |
| Privacy | Biometric consent history, verified erasure, retention purge, data export, AES-256-GCM encryption of embeddings and stored images with key rotation, temporary-file cleanup |
| Model comparison | One image through several enhancement profiles side by side, with metrics, timing, previews, an explainable recommendation and a CSV report |
| Sharing | Expiring, revocable public links to a result, optionally watermarked, with view counts and no viewer identity stored |
| Operations | Duplicate detection, processing reports (PDF), live batch progress, notifications, activity timeline, system health |

Feature status lives in [`docs/FEATURES.md`](docs/FEATURES.md).

## Layout

```
config/            settings, URL routing, Celery app
accounts/          users, JWT authentication, roles, audit events
uploads/           ingestion, storage, preprocessing
validation/        face detection and validation
quality_control/   quality scoring and decisions
enhancement/       enhancement pipeline, model adapters, identity guard
duplicates/        perceptual-hash duplicate detection
identities/        face embeddings, identity groups, encrypted storage
privacy/           consent, erasure, retention, export, cleanup
sharing/           expiring, revocable share links
reports/  downloads/  notifications/  activity/  operations/
scripts/           worker launcher, dataset audit, model training and export
notebooks/         model training notebooks
docs/              feature docs, architecture, decisions, testing guide
```

## Getting started

Requirements: Python 3.12+, PostgreSQL, Redis (Docker is the easiest way).

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # then edit database credentials and secrets
python manage.py migrate
python manage.py runserver
```

API documentation is served by drf-spectacular once the server is running.

### Background worker

Enhancement, identity analysis, reports and download packages run as Celery
tasks and do nothing until a worker is up:

```powershell
.\scripts\run_worker.ps1
```

This starts (or reuses) a local Redis container and runs the worker in the
foreground. Periodic maintenance (expiring downloads, retention, temp-file
cleanup, stuck-job recovery) runs from a separate scheduler; start exactly one
in its own terminal:

```powershell
.\scripts\run_beat.ps1
```

What runs, and how stuck jobs are recovered, is described in
[docs/features/20-background-job-operations.md](docs/features/20-background-job-operations.md).
The end-to-end manual flow is in
[`docs/MANUAL_TESTING_COMMANDS.md`](docs/MANUAL_TESTING_COMMANDS.md).

### Model files

Model weights are not committed. Place them under `.runtime/models/` (paths
and SHA-256 pins are in `.env.example`). Each adapter refuses to load a file
whose checksum does not match, and each feature stays off until its flag is
enabled. Licences matter here: PureScale and PureVision are
CC BY-NC-SA 4.0 (non-commercial), colorization is BSD-2-Clause. See the
licence notes in [`docs/features`](docs/features) and
[`docs/model-licenses`](docs/model-licenses).

## Testing

```powershell
python manage.py test                        # full suite
python manage.py test accounts privacy       # a subset
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py check --deploy              # production configuration audit
```

Tests run against a throwaway database and media folder, with a fast password
hasher (see `config/test_runner.py`), so they never touch your real users' files.
The few tests that run the real enhancement pipeline are tagged `slow`:

```powershell
python manage.py test --exclude-tag slow     # quick loop
python manage.py test                        # everything, before a commit
```

## Security posture

- Every endpoint requires authentication unless it is explicitly public
  (registration, login, token refresh, email verification, password reset).
  Only the `accounts` app declares public endpoints.
- Access tokens last 15 minutes; refresh tokens rotate and are blacklisted.
  Password change, reset, email change, suspension and role change bump a
  per-user `token_version` that revokes every outstanding token at once.
- Unverified, suspended and revoked accounts are rejected on every request,
  not just at login.
- Sensitive routes are rate limited per scope. `TRUSTED_PROXY_COUNT` must match
  the number of reverse proxies in front of Django, otherwise `X-Forwarded-For`
  is ignored so it cannot be used to dodge throttling.
- Repeated wrong passwords lock the account with an increasing delay (30 s up to
  15 min), for unknown accounts too, so it cannot be used to find real ones.
- Changing an account's email requires the current password.
- With `DJANGO_DEBUG=False` the app refuses to start on the built-in
  development secret key; CORS is open only when `DEBUG` is on.
- Stored images (originals, results, previews, reports, packages) and face
  embeddings are encrypted at rest (AES-256-GCM, rotatable keys); see
  [`docs/features/stored-image-encryption.md`](docs/features/stored-image-encryption.md).
  Run `manage.py check --deploy` before going live; the `privacy.*` checks
  cover consent, retention and key configuration.

The retention purge and temporary-file cleanup run from the Celery beat
schedule, so a beat process must be running in deployment.

## Before deploying

With `DJANGO_DEBUG=False` the application **refuses to start** unless the
configuration is production-safe, and lists every problem at once
([`config/production_guard.py`](config/production_guard.py)). It requires:

1. **Secrets.** A random `DJANGO_SECRET_KEY` (50+ characters), and separate
   random `IDENTITY_EMBEDDING_KEY` and `IMAGE_ENCRYPTION_KEY` (32+ characters
   each, all three different). Generate each with:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Set the embedding and image keys **before** the first data is stored. Changing
   one later is done with a new key id, the previous-keys list and the conversion
   commands: [embeddings](docs/features/41-privacy-and-facial-data.md),
   [images](docs/features/stored-image-encryption.md).
2. **Encryption at rest on:** `IMAGE_ENCRYPTION_ENABLED=True`. If you already have
   stored images, run `python manage.py encrypt_stored_media` once.
3. **Real hosts:** `DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS` (https, no
   wildcard, `CORS_ALLOW_ALL_ORIGINS=False`), `PUBLIC_BACKEND_URL` and
   `FRONTEND_URL` (https). Placeholders such as `example.com` are rejected.
4. **HTTPS:** `SECURE_SSL_REDIRECT=True` (or `TRUST_PROXY_SSL_HEADER=True` behind a
   proxy that terminates TLS), secure session and CSRF cookies, and
   `SECURE_HSTS_SECONDS` of at least one year. Set `TRUSTED_PROXY_COUNT` to the
   number of proxies in front of Django.
5. **Infrastructure:** PostgreSQL with a real password, an SMTP `EMAIL_BACKEND`
   and `EMAIL_HOST`, and `CELERY_TASK_ALWAYS_EAGER=False`.
6. **Privacy policy:** `BIOMETRIC_CONSENT_REQUIRED=True`. Also choose
   `FACIAL_DATA_RETENTION_DAYS`; `check --deploy` warns while it is 0.
7. Run the worker and exactly one beat process, and serve with
   `gunicorn config.wsgi -c gunicorn.conf.py` so share-link tokens are redacted
   from its access log.

Then run `python manage.py check --deploy` for Django's own checks.

## Documentation

- [`docs/FEATURES.md`](docs/FEATURES.md): feature catalogue and status
- [`docs/SECURITY_AND_PERFORMANCE_REVIEW.md`](docs/SECURITY_AND_PERFORMANCE_REVIEW.md): dependency audit, static analysis and performance findings, and how to repeat them
- [`docs/API_ERRORS.md`](docs/API_ERRORS.md): error shapes the frontend can rely on
- [`docs/features`](docs/features): per-feature design, verification and limits
- [`docs/structured`](docs/structured): architecture, decisions, tasks
- [`docs/12_DAY_COMPLETION_PLAN.md`](docs/12_DAY_COMPLETION_PLAN.md): delivery plan
