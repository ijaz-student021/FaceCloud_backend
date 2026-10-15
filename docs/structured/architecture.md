# FaceCloud System Architecture

## 1. Context

FaceCloud is a Django REST API backed by PostgreSQL and private file storage.
Redis brokers asynchronous work to Celery. OpenCV and local model adapters
perform image enhancement, validation, duplicate analysis, and identity grouping.

## 2. Logical architecture

```text
Web/mobile client
       |
       v
Django REST API
  |-- accounts and authorization
  |-- uploads and protected downloads
  |-- quality and face validation
  |-- enhancement and gallery editing
  |-- duplicates and identities
  |-- reports and notifications
       |
       +------------------+
       |                  |
       v                  v
 PostgreSQL          Private storage
 metadata, state     originals, derivatives
       |
       v
 Redis -> Celery workers
          |-- preprocessing/quality worker
          |-- enhancement worker
          |-- identity worker
          `-- dedicated GPU restoration worker (target architecture)
```

## 3. Main bounded modules

| Module | Responsibility |
|---|---|
| `accounts` | Authentication, roles, profiles, audit and administration |
| `uploads` | Ingestion, batches, preprocessing, protected originals and progress |
| `validation` | Face detection, landmarks and image suitability decisions |
| `quality_control` | Quality metrics, scores and final processing decisions |
| `enhancement` | Conservative enhancement, optional restoration and edit versions |
| `duplicates` | Owner-scoped image and face-region duplicate detection |
| `identities` | Embeddings, matching, identity groups and membership management |
| `reports` | JSON, CSV and PDF processing reports |
| `downloads` | Secure image and batch packages |
| `notifications` | In-app and optional email notifications |

## 4. Processing flow

```text
Upload
  -> content validation
  -> immutable original storage
  -> standardized derivative
  -> face validation + quality decision
  -> conservative enhancement
  -> duplicate analysis
  -> optional identity analysis
  -> report/notification/download availability
```

Identity analysis reads the original or conservative standardized derivative.
Generative display outputs must never become recognition inputs.

## 5. Enhancement architecture

```text
Source
 -> quality analysis and automatic level
 -> OpenCV exposure correction (only for a measured lighting defect)
 -> output guard A
 -> measured adaptive denoising
 -> bounded sharpening
 -> output guard B
 -> per-face fidelity check
 -> optional style preset, then the identity guard (YuNet/SFace)
 -> auditable derivative
```

Enhancement is OpenCV and Pillow only. The learned stages of earlier versions
(Zero-DCE/FaceCloud Curve, EDSR, NAFNet, Real-ESRGAN, GFPGAN, PureVision,
PureScale, colourisation) were retired on 2026-10-15 (ADR-024). The optional
OpenAI advisor only suggests slider values; it never touches pixels. The original
is never overwritten.

## 6. Recognition architecture

```text
Original/conservative image
 -> YuNet detection and five landmarks
 -> aligned face crop
 -> SFace 128-dimensional embedding
 -> encrypted or protected storage representation
 -> owner/tenant-scoped cosine comparison
 -> automatic match | new group | moderator review
```

## 7. Data principles

- PostgreSQL stores authoritative metadata and processing state.
- Storage backends hold image bytes under UUID-derived object names.
- Foreign keys and ownership constraints connect all derivatives to originals.
- Face embeddings are biometric data and require stronger retention and access
  controls than ordinary image metadata.
- Model artifacts live outside version control and are addressed by path plus
  expected SHA-256.

## 8. Security boundaries

1. The API authenticates every protected request.
2. Querysets enforce owner, moderator-organization, or administrator scope.
3. Downloads are served through authorization checks rather than public media URLs.
4. ZIP content is treated as hostile input.
5. Workers receive record identifiers, not arbitrary filesystem instructions.
6. Model outputs pass shape, pixel, fidelity, and quality validation.
7. Cross-tenant embedding comparison is forbidden.

## 9. Deployment topology

### Development

- Django development server
- Local PostgreSQL
- Redis Docker container
- CPU Celery worker
- Local private media and model directories

### Production target

- Reverse proxy and application server
- Managed PostgreSQL and Redis
- Private object storage
- Separate CPU and GPU Celery queues
- Centralized metrics, error monitoring, and structured logs
- Encrypted backups and documented recovery procedure

## 10. Verification

```powershell
.\venv\Scripts\python.exe manage.py check
.\venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\venv\Scripts\python.exe manage.py test --keepdb
```

## 11. Planned architecture extensions

- **Sharing/export:** identity ZIPs, export-only watermarks and expiring tokens
  extend the existing downloads domain (Features 24–26).
- **Moderation/tenancy:** organization membership, scoped review queues, decisions
  and audit history serve Features 27, 31 and 46.
- **Operations:** analytics, platform controls, health probes and model readiness
  serve Features 32–35.
- **Live processing:** authenticated sessions, backpressure, frame expiry and
  explicit consent serve Features 36–40.
- **Privacy lifecycle:** consent, retention, encryption policy, export and erasure
  cover originals, derivatives and embeddings for Feature 41.
- **Storage:** private object storage, protected delivery and lifecycle tooling
  implement Feature 43.
