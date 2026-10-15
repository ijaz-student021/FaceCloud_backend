# FaceCloud Product Requirements Document

## 1. Product summary

FaceCloud is a privacy-conscious facial-image platform for secure upload,
conservative enhancement, face validation, duplicate detection, identity
grouping, reporting, and controlled downloads. The backend is implemented with
Django REST Framework and asynchronous Celery workers.

The authoritative feature inventory and feature-level completion status are
maintained in [FEATURES.md](FEATURES.md). This document defines product intent,
users, requirements, boundaries, and release criteria.

## 2. Product goals

1. Accept individual images, multiple files, and ZIP collections safely.
2. Preserve immutable originals and create auditable processing derivatives.
3. Improve visibility and presentation quality without silently changing identity.
4. Detect and validate every supported face in group photographs.
5. Group recurring identities within the correct user or tenant boundary.
6. Provide progress, reports, notifications, downloads, and moderation workflows.
7. Protect biometric data through ownership checks, isolation, retention controls,
   and explicit provenance.

## 3. Non-goals for the current release

- Face identification against public or third-party databases
- Cross-tenant face matching
- Treating generatively restored pixels as forensic evidence
- Using restored images as the authoritative recognition source
- Unlimited real-time camera processing
- Automatic publication or external sharing without user action

## 4. Users and roles

### User

Uploads and manages owned images, requests processing, reviews results, manages
identity groups, downloads outputs, and receives notifications.

### Moderator

Reviews uncertain quality and identity decisions within an authorized
organization. A moderator cannot inspect unrelated users or tenants.

### Administrator

Manages users, roles, suspension, platform configuration, operational health,
and authorized organization settings.

## 5. Core user journeys

### Upload and process

1. The user authenticates and uploads images or a ZIP file.
2. FaceCloud validates content, limits, and archive safety.
3. The original is stored under a protected UUID path.
4. A standardized processing copy is created.
5. Quality and face validation run before enhancement.
6. Accepted images enter enhancement and optional identity analysis.
7. The user follows progress and receives a completion notification.

### Review enhanced output

1. The user opens an original/enhanced comparison.
2. The system displays applied models, quality changes, warnings, and provenance.
3. The user downloads the selected derivative without replacing the original.

### Manage identities

1. YuNet detects faces and landmarks.
2. SFace generates embeddings from original or conservative source pixels.
3. Owner-scoped similarity produces a match, new group, or review decision.
4. The user or moderator can rename, merge, split, confirm, reject, or remove
   memberships through audited operations.

## 6. Functional requirements

### Accounts and authorization

- Verified registration, login, refresh, logout, password recovery, and deletion
- User, moderator, and administrator roles
- Owner and tenant checks on every protected resource
- Security-relevant audit events

### Image ingestion

- Single, multiple, and ZIP uploads
- Content-based validation rather than extension-only trust
- Configurable file, pixel, member-count, and archive-expansion limits
- Protection from traversal, nested archives, encryption, and decompression bombs
- Physical cleanup when an owned record is deleted

### Enhancement

- Low, medium, high, and automatic levels
- OpenCV exposure processing for measured lighting defects
- Adaptive denoising, bounded sharpening, and quality guards
- An editor with bounded previews and full-resolution saved versions
- An optional OpenAI advisor that suggests settings only (off by default)
- Learned restoration models (EDSR, GFPGAN, Real-ESRGAN, NAFNet, PureVision, PureScale,
  colourisation, Zero-DCE) retired 2026-10-15
- Original and derivatives stored separately with processing provenance

### Recognition and grouping

- YuNet multi-face detection and five-point landmarks
- SFace embeddings and cosine similarity
- User/organization-scoped comparisons only
- Configurable match, new-group, and ambiguity thresholds
- Moderator review for uncertain assignments
- No embeddings generated from generative restoration outputs

### Operations

- Redis/Celery background jobs with retry and cancellation policies
- Live batch progress, reports, notifications, and protected downloads
- Health checks, structured failures, and idempotent task behavior

## 7. Non-functional requirements

### Security and privacy

- Least-privilege API access and tenant isolation
- Secrets and model licenses excluded from source control
- Checksum-pinned local model artifacts
- Restricted checkpoint loading where supported
- Consent, retention, deletion, and export controls for biometric information

### Reliability

- Processing failures must preserve the original
- Optional-model failures must fall back safely
- Background jobs must be repeatable without duplicate output records
- Corrupt or incompatible model output must be rejected by fidelity guards

### Performance

- Ordinary API requests should not run heavyweight inference synchronously
- CPU-safe tiling and output-pixel ceilings are mandatory
- Generative and HD workloads should use a dedicated GPU queue in production

### Observability

- Record stage order, duration, selected model, checksum prefix, decision, and error
- Provide batch-level progress and final reports
- Do not record raw embeddings, secrets, or private image data in logs

## 8. Success metrics

- Upload acceptance/rejection accuracy
- Processing completion and safe-fallback rate
- Median and 95th-percentile processing duration per stage
- Quality acceptance and moderator-review rates
- Identity false-match and false-new-group rates on a consented evaluation set
- Percentage of model outputs rejected by safety guards
- Zero confirmed cross-owner or cross-tenant data disclosures

## 9. Release criteria

A feature is complete only when implementation, migrations, tests,
documentation, access controls, failure handling, and applicable operational QA
are complete. Model-driven features additionally require checksum verification,
license evidence, representative visual evaluation, and threshold calibration.

## 10. Remaining product scope

This matrix reconciles the PRD with the current 47-feature catalogue. “Backend
complete” still means frontend integration or production QA may remain.

| Features | Capability | Current state | Remaining requirement |
|---|---|---|---|
| 10–19 | Enhancement, quality, validation, duplicates and identities | Backend complete/baseline | Frontend integration, model rollout QA and recognition calibration |
| 20 | Background batch processing | Partial | Recognition jobs, retries, cancellation, resource controls and monitoring |
| 21–23 | Progress, reports and protected downloads | Backend complete | Frontend polling, failure, history and regeneration flows |
| 24 | Identity-group ZIP export | Planned | Safe folders, metadata, report and audit event |
| 25 | Watermark options | Planned | Export-only branding, position, opacity and preview |
| 26 | Secure share links | Planned | Tokens, expiry, revocation, tracking and view-only delivery |
| 27–28 | Moderation and activity history | Partial | Scoped queues, decisions, corrections and complete events |
| 29–31 | Notifications and user/moderator dashboards | Backend/integration baseline | Connect real APIs and remove demonstration data |
| 32–35 | Administration and operations | Planned/partial | Analytics, controls, health and model comparison |
| 36–40 | Live-camera workflow | Planned | Detection, enhancement, comparison, transport and privacy |
| 41 | Facial-data privacy | Partial | Consent, retention, encryption, export and erasure |
| 43 | Private cloud storage | Planned | Private objects, protected delivery, lifecycle and migration |
| 44 | API/developer support | Partial | OpenAPI completion, examples and deployment guide |
| 45 | Testing and QA | Partial/continuous | Load, performance, camera, restoration and integration testing |
| 46 | Organizations and tenants | Planned | Membership, roles, isolation, policies and administration |
| 47 | Gallery editor | Backend complete | Frontend sliders, version history and final UI testing |
