# FaceCloud Backend

FaceCloud is a secure, AI-assisted platform for facial-image ingestion,
enhancement, quality validation, organization, moderation, reporting, and
protected delivery. This repository contains the Django REST Framework backend,
background workers, processing services, database models, APIs, automated tests,
and technical documentation.

The backend is designed around three principles:

1. Preserve the original image and create new, traceable processing artifacts.
2. Enforce ownership, role, and tenant boundaries in the backend.
3. Apply enhancement conservatively so that facial identity, natural appearance,
   resolution, and photographic detail are not destroyed.

## Current status

- 47 numbered product features are tracked.
- 23 features are complete or have complete backend foundations.
- 7 features are partially implemented.
- 17 features are planned.
- 171 automated backend tests pass against the current PostgreSQL-backed project.
- The next major implementation area is private, owner-scoped face recognition
  and identity grouping.

Status meanings:

- **Complete**: implemented, migrated, tested, and documented.
- **Complete backend**: the backend workflow is implemented; frontend integration
  remains.
- **Partial**: useful foundations exist, but the complete workflow is unfinished.
- **Planned**: approved scope that has not yet been implemented.

## Implemented processing pipeline

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
        +--> Dark image: private FaceCloud curve model + guarded blending
        |
        +--> Normal/bright image: conservative OpenCV correction
        |
        v
Optional noise reduction and EDSR x2 restoration
        |
        v
Optional guarded GFPGAN face restoration (private deployment enablement)
        |
        v
Output-quality guards, face validation and duplicate evidence
        |
        v
Accepted / Rejected / Needs review / Failed
        |
        v
Dashboard data, progress, notifications, reports and secure downloads
```

Original uploads are never overwritten. Standardized, enhanced, edited, report,
and package artifacts are stored separately with owner-scoped access.

## Complete feature catalogue

### Identity, access, and security

| # | Feature | Status | Explanation |
|---|---|---|---|
| 01 | User authentication and account management | Complete | Provides pending registration, secure six-digit email verification, username/email login, JWT access and refresh rotation, password recovery, profile management, logout, suspension checks, and account deletion. A permanent user is created only after successful verification. |
| 02 | Role-based authorization | Complete | Enforces user, moderator, and administrator roles, trusted dashboard routing, owner-only resources, privileged-role protection, and token revocation after sensitive changes. |
| 03 | Authentication audit trail | Complete | Records registration, verification, login, password, logout, role, suspension, and deletion events with timestamps, request identifiers, IP addresses, and structured details. |

### Upload and secure ingestion

| # | Feature | Status | Explanation |
|---|---|---|---|
| 04 | Single-image upload | Complete | Accepts exactly one verified image, records technical metadata, stores it under a secure UUID name, and exposes only protected owner downloads. |
| 05 | Multiple-image upload | Complete | Accepts 2–150 images, validates every file independently, supports partial success, and records per-file rejection reasons and batch totals. |
| 06 | ZIP image upload | Complete | Safely processes archives with ZIP-slip, ZIP-bomb, compression-ratio, encrypted-entry, symlink, nested-archive, corruption, count, and uncompressed-size protections. |
| 07 | Secure image storage and cleanup | Complete | Uses owner-scoped paths, UUID object names, protected downloads, Django storage abstraction, and physical cleanup after image, batch, cascade, administrator, or account deletion. |
| 08 | Upload batch management | Complete | Tracks upload type, status, counters, total size, nested image results, timestamps, retrieval, and owner-authorized deletion. |

### Image preparation and enhancement

| # | Feature | Status | Explanation |
|---|---|---|---|
| 09 | Image preprocessing and standardization | Complete | Corrects orientation, verifies formats, creates canonical PNG/JPEG processing copies, preserves originals, normalizes colour and metadata, and supports safe display-name editing. |
| 10 | Enhancement-level selection | Complete backend | Supports low, medium, high, and automatic enhancement for individual images and batches, with the applied level retained in result metadata and reports. |
| 11 | OpenCV enhancement | Complete baseline | Provides adaptive gamma, brightness, contrast, CLAHE, sharpness, and optional denoising with separate outputs, timing, and quality measurements. |
| 12 | Private AI enhancement | Complete backend | Runs the private FaceCloud curve model through OpenCV DNN, applies luminance-aware blending and destructive-output guards, supports EDSR restoration, and contains a checksum-pinned optional GFPGAN adapter. |
| 13 | Before-and-after comparison | Complete backend | Returns protected original, standardized, and enhanced URLs plus normalized quality differences, model metadata, dimensions, timing, warnings, and download information. |

### Quality and facial validation

| # | Feature | Status | Explanation |
|---|---|---|---|
| 14 | Image quality assessment | Complete backend | Measures luminance, blur, sharpness, contrast, noise, resolution, shadow/highlight clipping, normalized scores, and configurable final-quality decisions. |
| 15 | Face detection and validation | Complete baseline | Detects faces, supports individual and 3–5-person images, maps boxes to full resolution, and records per-face size, location, blur, luminance, clipping, boundary, and detector provenance. |
| 16 | Processing status workflow | Complete foundation | Persists uploaded, pending, processing, accepted, rejected, needs-review, failed, completed, and cancelled states with stages, task IDs, timestamps, and reasons. |
| 17 | Duplicate image and face detection | Complete baseline | Combines owner-scoped SHA-256, perceptual image hashes, and face-region evidence. Suspected matches are review evidence and never cause automatic deletion. |

### Recognition and identity organization

| # | Feature | Status | Explanation |
|---|---|---|---|
| 18 | Face recognition and identity grouping | Planned | Will extract commercially approved private embeddings, compare faces only within an authorized owner or organization scope, create identity groups, and route uncertain matches for review. |
| 19 | Identity group management | Planned | Will let authorized users rename groups, merge groups, move or remove images, select profile images, delete invalid groups, and inspect confidence and audit history. |

### Background processing and progress

| # | Feature | Status | Explanation |
|---|---|---|---|
| 20 | Background batch processing | Partial | Celery/Redis preprocessing and enhancement jobs, isolation, counters, and failures are implemented; advanced recognition jobs, cancellation, resource policies, and retries remain. |
| 21 | Live batch progress tracker | Complete backend | Exposes real processed/remaining counts, accepted/rejected/review/failed totals, active stage, percentage, timing, failure details, cooperative cancellation, and retry APIs. |

### Results, reports, and distribution

| # | Feature | Status | Explanation |
|---|---|---|---|
| 22 | Processing reports | Complete backend | Generates versioned JSON, CSV, and PDF reports asynchronously from immutable snapshots with processing, quality, face, duplicate, timing, reason, checksum, and protected-link data. |
| 23 | Image and batch downloads | Complete backend | Delivers protected originals and results plus asynchronous expiring ZIP packages, manifests, embedded reports, collision-safe filenames, checksums, history, and physical cleanup. |
| 24 | Identity-group ZIP export | Planned | Will export one identity group with original and enhanced directories, group metadata, a report, secure generation, and a download audit event. |
| 25 | Watermark options | Planned | Will create optional small, username, FaceCloud, or custom watermarked copies without modifying the primary enhanced asset. |
| 26 | Secure share links | Planned | Will provide cryptographically secure, expiring, revocable, view-only links with access tracking and a 24-hour default. |

### Moderation, activity, and notifications

| # | Feature | Status | Explanation |
|---|---|---|---|
| 27 | Moderator review system | Partial | Role permissions exist; organization-scoped queues, membership management, low-confidence decisions, corrections, reprocessing, escalation, duplicate review, and immutable moderator history remain. |
| 28 | Complete activity timeline | Partial | Authentication activity exists; processing, download, export, group, share, moderator, and administrative events will extend it. |
| 29 | In-app and email notifications | Complete baseline | Provides owner-scoped notifications, unread counts, read/delete actions, preferences, idempotent processing publishers, verified-address email delivery, retries, and retention cleanup. |

### Dashboards and administration

| # | Feature | Status | Explanation |
|---|---|---|---|
| 30 | User dashboard | Complete backend / frontend integration remaining | Supplies owner-scoped statistics, recent activity, upload and processing history, My Images filters, result states, comparisons, reports, downloads, and account data. |
| 31 | Moderator dashboard | Planned frontend integration | Will expose only active-organization members and explicitly authorized organization/review content, with no cross-organization discovery or access to personal images, credentials, sessions, or cameras. |
| 32 | Admin analytics dashboard | Planned | Will show platform totals, success rate, rejection reasons, storage, latency, live sessions, charts, and operational filters. |
| 33 | Administrative management | Partial | User listing, role changes, suspension, and reactivation exist; global processing, storage, threshold, moderation, and audit operations remain. |
| 34 | System health page | Planned | Will monitor API, PostgreSQL, Redis, Celery, models, storage, email, latency, jobs, disk capacity, and application version. |

### Model evaluation and live camera

| # | Feature | Status | Explanation |
|---|---|---|---|
| 35 | Uploaded-image model comparison | Planned | Will compare original, OpenCV, and private-model results using brightness, blur, sharpness, face detection, processing time, recommendation, and an evaluation report. |
| 36 | Live camera face detection | Planned | Will manage camera permission and selection, live face boxes/count, face-position guidance, confidence, low-light/blur warnings, and valid-frame capture. |
| 37 | Real-time camera enhancement | Planned | Will provide controlled OpenCV/private-model previews, enhancement levels, frame-rate control, performance measurements, and enhanced-frame capture. |
| 38 | Live enhancement comparison | Planned | Will show original and enhanced feeds together with live quality, face-confidence, latency, FPS, recommendation, capture, and report data. |
| 39 | Live processing communication | Planned | Will implement authenticated WebSocket or rate-controlled frame transport with backpressure, old-frame dropping, sessions, timeouts, and monitoring. |
| 40 | Live camera privacy and security | Planned | Will require explicit permission, show active-camera state, avoid recording by default, store only confirmed captures, encrypt production traffic, and immediately clean temporary frames. |

### Platform, storage, privacy, and engineering

| # | Feature | Status | Explanation |
|---|---|---|---|
| 41 | Privacy and facial-data security | Partial | JWT, roles, ownership, throttling, secure filenames, protected downloads, and personal/organization/review scopes exist; consent, biometric retention, encryption-at-rest, export, and security reporting remain. |
| 42 | PostgreSQL data platform | Complete | Uses PostgreSQL 18 with an application role, environment configuration, migrated data, sequence validation, PostgreSQL test-database support, and rollback backup. |
| 43 | Private cloud storage | Planned | Will move production artifacts to a private S3-compatible bucket using secure keys, signed URLs, quotas, analytics, lifecycle rules, and deletion policies. |
| 44 | API documentation and developer support | Partial | Maintains project and per-feature documentation; OpenAPI/Swagger and live-processing documentation remain. |
| 45 | Testing and quality assurance | Partial and continuous | Currently includes 162 automated tests plus Django system, migration-drift, security, ownership, ZIP, cleanup, queue, and operational checks; production load and remaining feature tests will continue to grow. |
| 46 | Organization and tenant management | Planned | Will provide organizations, subscriptions, quotas, roles, invitations, scoped resources, explicit content ownership, tenant-isolated queries/tasks/downloads, review grants, audits, and safe backfill. |
| 47 | Non-destructive photo gallery editor | Complete backend | Supports automatic correction and manual brightness, contrast, shadows, saturation, and highlights; renders full-resolution immutable edit versions without replacing the original or primary enhancement. |

The detailed source of truth is
[`docs/FEATURES.md`](docs/FEATURES.md). Per-feature implementation and design
documents are stored under [`docs/features`](docs/features).

## Six-digit signup verification

Registration is deliberately a pending workflow:

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

Security controls include:

- Codes generated with Python's cryptographic `secrets` module.
- Verification codes and passwords are stored only as Django hashes.
- Configurable code lifetime, resend cooldown, hourly-send limit, and maximum
  failed attempts.
- Atomic user creation, pending-record consumption, and audit-event creation.
- Idempotent repeat verification and masked public status responses.
- Cleanup command for expired and consumed pending records.

Relevant endpoints:

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/auth/register/` | Start a pending registration and send a code |
| POST | `/api/auth/verify-email/` | Verify the code and create the account |
| POST | `/api/auth/verify-email/resend/` | Invalidate and replace the current code |
| GET | `/api/auth/registrations/{registration_id}/status/` | Read masked registration status |
| POST | `/api/auth/login/` | Obtain JWT access and refresh tokens |
| POST | `/api/auth/refresh/` | Rotate a refresh token |

## AI and image-processing components

| Component | Purpose | Runtime state |
|---|---|---|
| Private FaceCloud curve model | Adaptive low-light correction | Enabled when the private ONNX checkpoint is configured |
| OpenCV enhancement | Conservative traditional correction and fallback | Enabled |
| EDSR x2 | Resolution restoration for eligible low-resolution inputs | Enabled when its private model file is configured |
| GFPGAN v1.4 adapter | Guarded face-only restoration for eligible degraded faces | Isolated worker and real-image smoke test passed; portable default remains off because private artifacts are not distributed |
| OpenCV face detector | Baseline face detection and validation | Enabled |
| Perceptual hashing | Owner-scoped duplicate evidence | Enabled |
| Face embedding provider | Recognition and identity grouping | Planned; no global face matching exists |

Private checkpoints, user media, runtime outputs, datasets, credentials, and local
database backups must never be committed to Git.

### Guarded GFPGAN deployment

GFPGAN is an optional, private-deployment stage—not an unconditional beauty
filter. The backend sends only eligible degraded faces to restoration, applies a
bounded blend, preserves the original pixel grid, and rejects unsafe results
using SSIM, pixel-change, colour, clipping, face-count, and output-shape guards.
It does not claim biometric identity verification.

The portable repository keeps GFPGAN disabled because the licensed checkpoint
and auxiliary detection/parsing weights are private. After configuring the
private files and hashes in `.env`, validate the installation with:

```powershell
python manage.py check_gfpgan --load-model
```

Run GFPGAN jobs in their isolated Celery queue:

```powershell
.\scripts\start_gfpgan_worker.ps1
```

This separation prevents the CPU-heavy restoration stage from blocking normal
uploads, reports, notifications, and other background jobs.

## Technology

- Python 3 and Django
- Django REST Framework
- PostgreSQL 18
- Simple JWT
- Pillow and OpenCV
- ONNX/OpenCV-DNN model inference
- Celery and Redis
- ReportLab and structured CSV/JSON reporting
- Private local storage with a future S3-compatible storage path

## Local development

### 1. Create and activate the environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure the environment

```powershell
Copy-Item .env.example .env
```

Set the PostgreSQL, email, Redis, security, and private model paths in `.env`.
Never commit `.env`.

### 3. Apply migrations

```powershell
python manage.py migrate
```

### 4. Start Redis

```powershell
docker start facecloud-redis
docker exec facecloud-redis redis-cli ping
```

The expected Redis response is `PONG`.

### 5. Start the Celery worker

Use a separate PowerShell terminal:

```powershell
.\venv\Scripts\Activate.ps1
$env:CELERY_TASK_ALWAYS_EAGER = "False"
.\venv\Scripts\celery.exe -A config worker --loglevel=info --pool=solo
```

When private GFPGAN restoration is enabled, also start its dedicated worker in a
third terminal:

```powershell
.\scripts\start_gfpgan_worker.ps1
```

### 6. Start Django

Use another PowerShell terminal:

```powershell
.\venv\Scripts\Activate.ps1
$env:CELERY_TASK_ALWAYS_EAGER = "False"
python manage.py runserver
```

## Verification

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --keepdb
```

The current verified full-suite result is:

```text
Found 171 test(s).
System check identified no issues (0 silenced).
Ran 171 tests
OK
```

## Repository structure

```text
accounts/          Authentication, authorization, profiles, dashboards and audits
uploads/           Uploads, batches, secure storage and preprocessing
enhancement/       Enhancement routing, models, comparison and gallery editing
quality_control/   Quality measurements and final decisions
validation/        Face detection and per-face validation
duplicates/        Exact and perceptual duplicate evidence
progress/          Processing progress, retry and cancellation
reports/           JSON, CSV and PDF report generation
downloads/         Secure image, report and batch-package delivery
notifications/     In-app notifications, preferences and email delivery
config/            Django, Celery and environment configuration
docs/              PRDs, feature catalogue and implementation documentation
scripts/           Controlled model-training and operational scripts
```

## Documentation

- [Complete feature catalogue](docs/FEATURES.md)
- [Six-digit verification PRD](docs/PRD_6_DIGIT_SIGNUP_EMAIL_VERIFICATION.md)
- [Authentication and authorization](docs/features/01-authentication-and-authorization.md)
- [Uploads and secure storage](docs/features/02-image-upload-and-secure-storage.md)
- [Preprocessing](docs/features/09-image-preprocessing-and-standardization.md)
- [Enhancement pipeline](docs/features/10-12-image-enhancement.md)
- [Quality decision workflow](docs/features/14-16-quality-decision-workflow.md)
- [Duplicate detection](docs/features/17-duplicate-image-and-face-detection.md)
- [Identity-grouping plan](docs/features/18-face-recognition-and-identity-grouping.md)
- [Progress tracking](docs/features/20-21-live-batch-progress.md)
- [Reports](docs/features/22-processing-reports.md)
- [Secure downloads](docs/features/23-secure-image-and-batch-downloads.md)
- [Notifications](docs/features/29-in-app-and-email-notifications.md)
- [User dashboard APIs](docs/features/30-user-dashboard-and-my-images.md)
- [Moderator organization design](docs/features/27-31-46-moderator-organization-panel.md)
- [PostgreSQL platform](docs/features/42-postgresql-data-platform.md)
- [Gallery editor](docs/features/47-nondestructive-gallery-editor.md)

## Security and repository hygiene

Do not commit:

- `.env` files or credentials
- PostgreSQL or SQLite data
- user-uploaded media
- generated reports or packages
- private model checkpoints
- training datasets
- virtual environments
- runtime caches or local backups

Only sanitized configuration examples and code should be published.
