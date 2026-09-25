# FaceCloud Backend

[![CI](https://github.com/ijaz-student021/FaceCloud_backend/actions/workflows/ci.yml/badge.svg)](https://github.com/ijaz-student021/FaceCloud_backend/actions/workflows/ci.yml)

FaceCloud is a facial-image platform: users upload photos, the backend judges
their quality, enhances them (low-light recovery, denoising, upscaling, face
restoration, colorization, style presets), groups the faces it finds by
identity, and keeps everything private to the owner. This repository is the
Django REST Framework API and the Celery workers behind it.

It is the backend of a final-year project, designed around three principles:

1. Preserve the original image and create new, traceable processing artifacts.
2. Enforce ownership, role, and tenant boundaries in the backend.
3. Apply enhancement conservatively, so facial identity, natural appearance,
   resolution, and photographic detail are not destroyed.

Every feature has a document under [`docs/features`](docs/features) that
records what was built, how it was verified, and what its known limits are.
[`docs/FEATURES.md`](docs/FEATURES.md) is the authoritative, always-current
feature catalogue and status table; this README does not duplicate it.

## What it does

| Area | Summary |
|---|---|
| Accounts | Pending registration with six-digit email verification, JWT login with refresh rotation, per-account login backoff, password reset, logout from all devices, admin / moderator / user roles, suspension, audit trail |
| Uploads | Single, multiple and ZIP upload; content-based JPG/PNG validation; ZIP-slip and ZIP-bomb protection; UUID storage paths; owner-only access |
| Quality control | Sharpness, exposure, contrast, noise and resolution scoring with explainable accept / review / reject decisions |
| Enhancement | Adaptive pipeline: Zero-DCE exposure, adaptive denoise, EDSR / NAFNet / Real-ESRGAN / PureScale / PureVision restoration, guarded GFPGAN face restoration, sharpening, colorization, style presets and sliders, final output-contract validation |
| Identity | YuNet face detection, SFace embeddings, identity grouping, a guard that rejects enhancements that change who a face looks like, ZIP export per identity |
| Organizations | Multi-tenant workspaces: owner / moderator / member roles, hashed-token invitations, protected-owner and last-owner rules, an immutable per-organization audit trail, organization-wide data export |
| Moderation | Organization-scoped review queue for uncertain AI decisions (quality, duplicate, identity), decision write-back to the underlying record, assignment and escalation notifications, batch retry / cancel, organization reports, and duplicate-group merging |
| Privacy | Biometric consent history, verified erasure, retention purge, personal and organization-wide data export, AES-256-GCM encryption of embeddings and stored images with key rotation, temporary-file cleanup |
| Model comparison | One image through several enhancement profiles side by side, with metrics, timing, previews, an explainable recommendation and a CSV report |
| Sharing | Expiring, revocable public links to a result, optionally watermarked, with view counts and no viewer identity stored |
| Operations | Duplicate detection, processing reports (PDF), live batch progress, notifications, activity timeline, system health, admin analytics and administration (failed-job visibility and retry, platform-wide audit feed) |

## Processing pipeline

```text
Authenticated request
        |
        v
Single image / multiple images / ZIP archive
        |
        v
Content validation and secure original storage
        |
        v
Celery + Redis background processing
        |
        v
Orientation, colour, format and size standardization
        |
        v
Scene analysis and adaptive enhancement routing
        |
        +--> Dark image: Zero-DCE / private curve model + guarded blending
        |
        +--> Normal/bright image: conservative OpenCV correction
        |
        v
Optional denoise, EDSR/NAFNet/Real-ESRGAN/PureScale/PureVision restoration
        |
        v
Optional guarded GFPGAN face restoration (disabled by default)
        |
        v
Output-quality guards, colorization, style, identity guard, output-contract
validation, face validation and duplicate evidence
        |
        v
Accepted / Rejected / Needs review / Failed
        |
        v
Dashboard data, progress, notifications, reports and secure downloads
```

Original uploads are never overwritten. Standardized, enhanced, edited,
comparison, report, and package artifacts are stored separately with
owner-scoped access.

## Layout

```
config/            settings, URL routing, Celery app, storage encryption
accounts/          users, JWT authentication, six-digit signup, roles, audit events
uploads/           ingestion, storage, preprocessing
validation/        face detection and validation
quality_control/   quality scoring and decisions
enhancement/       enhancement pipeline, model adapters, identity guard
duplicates/        perceptual-hash duplicate detection
identities/        face embeddings, identity groups, encrypted storage
organizations/     multi-tenant workspaces, membership, invitations, audit trail
moderation/        organization-scoped review queue, decisions, reports, batch actions
privacy/           consent, erasure, retention, personal and organization export, cleanup
sharing/           expiring, revocable share links
events/            automatic event/timeline grouping of a user's photos
collages/          face-aware photo collages from an event or a person
thumbnails/        cached, encrypted-at-rest JPEG previews of uploaded photos
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

### Six-digit signup verification

Registration is deliberately a pending workflow: no permanent account exists
until the code is verified.

```text
POST /api/auth/register/
        |
        v
PendingRegistration with hashed password and hashed six-digit code
        |
        v
Plain-text and HTML verification email
        |
        v
POST /api/auth/verify-email/
        |
        v
Locked row + transaction.atomic()
        |
        v
Verified User + audit events + cleared pending secrets
```

Codes are generated with Python's cryptographic `secrets` module and stored
only as Django hashes, with a configurable code lifetime, resend cooldown,
hourly-send limit, and maximum failed attempts (`SIGNUP_VERIFICATION_*` in
`.env.example`). See
[`docs/features/01-authentication-and-authorization.md`](docs/features/01-authentication-and-authorization.md)
and [`docs/PRD_6_DIGIT_SIGNUP_EMAIL_VERIFICATION.md`](docs/PRD_6_DIGIT_SIGNUP_EMAIL_VERIFICATION.md).

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/auth/register/` | Start a pending registration and send a code |
| POST | `/api/auth/verify-email/` | Verify the code and create the account |
| POST | `/api/auth/verify-email/resend/` | Invalidate and replace the current code |
| GET | `/api/auth/registrations/{registration_id}/status/` | Read masked registration status |
| GET | `/api/auth/verify-email/change/{uidb64}/{token}/` | Verify an existing account's changed email |
| POST | `/api/auth/login/` | Obtain JWT access and refresh tokens |
| POST | `/api/auth/refresh/` | Rotate a refresh token |

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

Private checkpoints, user media, runtime outputs, datasets, credentials, and
local database backups must never be committed to Git.

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

`config/test_end_to_end.py` is one realistic pass through the whole product over
the real HTTP API (sign up by emailed code, log in, upload, progress, thumbnails,
encrypted downloads, events, enhancement, collage, notifications, isolation from
another user). It runs the background jobs in-process, needs no Redis, and uses a
throwaway database. It needs the sample photos in `Dataset/` and `People/`, so it
is skipped when they are missing and by the quick loop above:

```powershell
python manage.py test config.test_end_to_end --tag slow
```

### Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and pull
request, and weekly (to catch vulnerabilities published later):

| Job | Checks |
|---|---|
| Tests and schema | `check`, no missing migrations, OpenAPI schema with zero warnings, the test suite (without the `slow` tests) against PostgreSQL |
| Dependency audit and static analysis | `pip-audit` on `requirements.txt`; `bandit` failing on medium or high findings |
| Production configuration | The app refuses an unsafe production environment, and starts and passes `check --deploy` with a correct one |

[`.github/dependabot.yml`](.github/dependabot.yml) opens weekly update pull
requests. The jobs need no model files or secrets, and a clean checkout passes.

### Contributing

`main` is branch-protected: all three CI checks above must pass, and direct
pushes (including from admins) are refused. The workflow is:

```powershell
git checkout main && git pull
git checkout -b your-branch-name
# make changes, commit
git push -u origin your-branch-name
gh pr create --base main
```

Open the PR, wait for CI to go green, then merge. Merged branches are deleted
automatically.

## Security posture

- Every endpoint requires authentication unless it is explicitly public
  (registration, verification, login, token refresh, password reset).
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

### Do not commit

`.env` files or credentials, PostgreSQL or SQLite data, user-uploaded media,
generated reports or packages, private model checkpoints, training datasets,
virtual environments, or runtime caches / local backups. Only sanitized
configuration examples and code should be published.

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
- [`docs/FRONTEND_API_GUIDE.md`](docs/FRONTEND_API_GUIDE.md): how a frontend uses the API (auth, upload and polling, enhancement, people, sharing, organizations, moderator area)
- [`docs/API_ERRORS.md`](docs/API_ERRORS.md): error shapes the frontend can rely on
- [`docs/features`](docs/features): per-feature design, verification and limits
- [`docs/structured`](docs/structured): architecture, decisions, tasks
- [`docs/12_DAY_COMPLETION_PLAN.md`](docs/12_DAY_COMPLETION_PLAN.md): delivery plan
- [`docs/PRD_6_DIGIT_SIGNUP_EMAIL_VERIFICATION.md`](docs/PRD_6_DIGIT_SIGNUP_EMAIL_VERIFICATION.md): signup verification PRD
