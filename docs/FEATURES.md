# FaceCloud Complete Feature Catalogue and Implementation Roadmap

## 1. Purpose

This document is the authoritative feature catalogue for FaceCloud. It combines the original FYP proposal, company-requested additions, security requirements, operational tooling, live camera processing, and supporting technical infrastructure.

Status definitions:

- **Complete**: implemented, migrated, tested, documented, and manually or operationally verified where applicable.
- **Partial**: foundational work exists, but the full user-facing workflow is not finished.
- **Planned**: approved project scope that has not yet been implemented.
- **Future**: optional extension after the required system is complete.

## 2. Current summary

| Area | Status |
|---|---|
| Authentication and account management | Complete, including six-digit pending signup verification |
| Role-based authorization | Complete |
| Authentication audit events | Complete |
| Single, multiple, and ZIP image upload | Complete |
| Secure local image storage and ownership | Complete |
| PostgreSQL 18 migration | Complete |
| Image preprocessing and format standardization | Complete |
| Celery preprocessing queue | Complete |
| Image enhancement | Complete backend: adaptive OpenCV, EDSR, and private FaceCloud curve inference |
| Non-destructive photo gallery editor | Complete backend; frontend slider wiring remains |
| Quality assessment, face validation, and final decisions | Complete backend baseline |
| Duplicate image and face-region detection | Complete backend baseline |
| Identity grouping and moderation | Complete backend; production threshold evaluation remains |
| User dashboard statistics and My Images API | Complete backend; frontend integration remains |
| Processing reports | Complete backend; frontend integration remains |
| Secure image and batch downloads | Complete backend; frontend integration remains |
| Admin analytics and administrative management | Complete backend; frontend dashboards remain |
| In-app and email notifications | Complete backend baseline; frontend integration remains |
| Live camera detection and enhancement | Planned |
| Frontend dashboards | Planned for frontend integration |

### Detailed documentation available

- [Remaining user-dashboard features and implementation order](USER_DASHBOARD_REMAINING_FEATURES.md)
- [Features 01–03: Authentication, accounts, authorization, and audit](features/01-authentication-and-authorization.md)
- [Features 04–08: Image uploads, batches, secure storage, and cleanup](features/02-image-upload-and-secure-storage.md)
- [Feature 09: Image preprocessing and standardization](features/09-image-preprocessing-and-standardization.md)
- [Features 10–12: Image-enhancement foundation and remaining processing work](features/10-12-image-enhancement.md)
- [Features 27, 31, and 46: Moderator organization panel and tenant privacy](features/27-31-46-moderator-organization-panel.md)
- [Feature 30: User dashboard statistics and My Images API](features/30-user-dashboard-and-my-images.md)
- [Feature 15: Face detection and validation](features/15-face-detection-and-validation.md)
- [Features 14–16: Quality scoring and final decision workflow](features/14-16-quality-decision-workflow.md)
- [Feature 17: Duplicate image and face-region detection](features/17-duplicate-image-and-face-detection.md)
- [Feature 18: Face recognition and identity-grouping implementation plan](features/18-face-recognition-and-identity-grouping.md)
- [Feature 19: Identity group management](features/19-identity-group-management.md)
- [Feature 28: Complete user activity timeline](features/28-user-activity-timeline.md)
- [Feature 42: PostgreSQL data platform](features/42-postgresql-data-platform.md)
- [Features 20–21: Background processing and live batch progress](features/20-21-live-batch-progress.md)
- [Feature 22: Processing reports](features/22-processing-reports.md)
- [Feature 23: Secure image and batch downloads](features/23-secure-image-and-batch-downloads.md)
- [Feature 29: In-app and email notifications](features/29-in-app-and-email-notifications.md)
- [Feature 47: Non-destructive photo gallery editor](features/47-nondestructive-gallery-editor.md)

## 3. Identity, access, and security foundation

### Feature 01: User authentication and account management — Complete

Provides the complete account lifecycle and proves the identity of API users.

- Registration with username, email, and password
- Six-digit email verification before permanent user creation
- Hashed pending password and code storage
- Verification expiry, attempt locking, resend cooldown, and hourly send limits
- Atomic verified-user creation and pending-secret cleanup
- Required, unique, normalized email addresses
- Strong password validation and secure hashing
- Email verification and verification-code resend
- Login using username, email, or identifier
- JWT access and refresh tokens
- Password change and password reset
- Profile viewing and editing
- Logout from one or all devices
- Account deletion with password confirmation
- Blocking of unverified, inactive, and suspended users

### Feature 02: Role-based authorization — Complete

Controls what an authenticated account is allowed to do.

- User, moderator, and administrator roles
- Trusted role stored in PostgreSQL
- User, moderator, and administrator dashboard routing values
- Administrator-only user and role management
- Prevention of self-assigned privileged roles
- Reusable administrator and moderator permission classes
- Owner-only resource access
- Token revocation after sensitive account changes

### Feature 03: Authentication audit trail — Complete

Records security-relevant account activity for accountability and diagnosis.

- Registration and verification events
- Successful and failed login events
- Password change and reset events
- Logout events
- Role-change events
- Suspension and reactivation events
- Account-deletion events
- Identifier, timestamp, IP address, and structured details

## 4. Upload and secure ingestion

### Feature 04: Single-image upload — Complete

Accepts one authenticated facial-image upload and creates a secure metadata record.

- Dedicated single-image frontend card specification
- Exactly one image enforced by the API
- JPG/JPEG, PNG, WebP, BMP, TIFF, and static GIF support
- Up to 400 MB per image by default
- Actual content verification with Pillow
- Corrupt and fake-image rejection
- Width, height, MIME type, byte size, and checksum metadata
- Secure UUID storage filename
- Protected owner-only download

### Feature 05: Multiple-image upload — Complete

Accepts several image files in one request and validates each independently.

- Up to 150 images per batch by default
- At least two images enforced by the API
- Phone photo-picker and desktop multi-file picker specification
- Per-file acceptance or rejection
- Partial success when some files are invalid
- Per-file rejection explanations
- Batch counters and total-size metadata
- Owner-only batch retrieval and deletion

### Feature 06: ZIP image upload — Complete

Safely accepts large image collections packaged as ZIP archives.

- Up to 2 GB uploaded ZIP size
- Dedicated ZIP-only frontend card and exactly-one-archive API validation
- Up to 5 GB total uncompressed content
- Up to 150 archive members
- Path-traversal protection
- ZIP-bomb and compression-ratio protection
- Nested ZIP, encrypted entry, and symbolic-link rejection
- Corrupt and empty archive rejection
- Streamed temporary extraction without trusting archive paths

### Feature 07: Secure image storage and cleanup — Complete

Stores files separately from database metadata while enforcing user ownership.

- User-scoped storage paths
- Secure UUID object names
- Original filename retained only as metadata
- Authenticated listing and downloads
- Owner-only details and deletion
- Physical cleanup after image, batch, admin, cascade, or account deletions
- Django storage abstraction for future private S3 storage

### Feature 08: Upload batch management — Complete

Tracks the result of every single, multiple, or ZIP ingestion operation.

- Batch type and owner
- Uploaded, partial, rejected, and failed statuses
- Total, accepted, and rejected counters
- Total input size
- Creation and update timestamps
- Nested image results
- Batch retrieval and deletion

## 5. Image preparation and enhancement

### Feature 09: Image preprocessing and standardization — Complete

Creates safe, consistent processing copies before AI enhancement.

All uploaded images pass through a dedicated format-conversion gateway before
enhancement. PNG and JPEG images can continue in their existing format. Other
approved image formats are decoded and converted to a canonical PNG or JPEG
processing copy. The original upload is never overwritten.

Single-image, multiple-image, and ZIP uploads all trigger this pipeline
automatically with `auto` format selection. Users may later request PNG or JPEG
explicitly through the preprocessing API.

```text
[Uploaded image]
        |
        v
[Validate real file type and safety limits]
        |
        v
[Format-conversion gateway]
  PNG/JPEG -> keep or normalize
  Other approved format -> convert to PNG/JPEG
        |
        v
[Orientation, color, size and metadata standardization]
        |
        v
[Enhancement pipeline]
```

- Orientation correction
- Resolution and dimension normalization
- Large-image resizing
- Color-space and format normalization
- Mandatory conversion of every approved non-PNG/JPEG input into PNG or JPEG
  before enhancement
- Initial conversion allowlist: JPG/JPEG, PNG, WebP, BMP, and TIFF
- Static GIF support by converting its first frame; animated output is outside the
  enhancement scope
- Optional HEIC/HEIF and AVIF support only when their verified decoding libraries
  are installed and covered by security tests
- Unsupported, corrupted, animated, or unsafe files are rejected with a clear reason
- The server chooses a safe canonical format automatically when the user does not:
  PNG for transparency or lossless requirements, otherwise JPEG
- User-selected output format for each image or batch
- Safe image renaming with validated names and the correct generated extension
- User-editable batch/folder display name for organizing images
- UUID-based internal storage paths remain system-managed to prevent path traversal,
  name collisions, and accidental overwrites
- Metadata handling
- Original-image preservation
- Processing-copy generation
- Memory-conscious handling of large inputs

### Feature 10: Enhancement-level selection — Complete backend

Lets users control enhancement strength while preserving an automatic option.
The choices, persistence, single-image API, batch API, and applied-level metadata
are implemented. Frontend integration remains separate.

- Low enhancement
- Medium enhancement
- High enhancement
- Automatic enhancement
- Per-image or batch-level selection
- Preview and selected-level metadata
- Report integration

### Feature 11: OpenCV enhancement — Complete baseline

Provides a fast traditional baseline for correction and model comparison.
The production luminance policy, separate output storage, Celery batch execution,
owner-only APIs, secure downloads, and automated tests are implemented.

- Gamma and brightness correction
- Contrast adjustment
- Histogram equalization and CLAHE
- Sharpness adjustment
- Optional noise reduction
- CPU-based execution
- Processing-time and quality measurements

### Feature 12: Zero-DCE AI enhancement — Complete backend

Uses deep learning to improve low-light images while preserving facial detail.

- Private FaceCloud Epoch99 checkpoint exported reproducibly to ONNX
- Lightweight OpenCV-DNN CPU inference
- Low-light routing with adaptive OpenCV fallback
- Quality guard against destructive luminance or clipping growth
- Batch inference
- Provider, model, curve, dimensions, license, and fallback metadata
- Enhanced-image persistence
- Safe missing/corrupt-model failure handling
- Private GFPGAN v1.4 face-restoration adapter for eligible blurry or
  low-resolution faces
- Checksum-pinned private checkpoint with no automatic model download
- Face-only restoration with the background upsampler disabled
- Same-pixel-grid validation, conservative restoration weight and output blend
- Adaptive restoration guard using structural similarity, bounded pixel change,
  chroma shift, and highlight-clipping growth
- Checksum-pinned detector and parser artifacts with no runtime downloads
- Dedicated Python 3.11 Celery worker preflight and `gfpgan` queue
- Safe fallback when the optional runtime, checkpoint, face, or output is invalid

The GFPGAN integration and isolated worker have passed local model and real-image
pipeline validation. Portable configuration remains disabled because licensed
model artifacts are not distributed; authorized deployments can enable the
private queue after preflight, while broader rollout still requires a consented
photographic evaluation set.

- PureScale (SAFMN) 2x super-resolution and PureVision (RRDBNet) same-resolution
  artifact-removal adapters, both checksum-pinned to third-party checkpoints
  from limitlesslab/AI-upscaling-models
- Independently gated behind `PURESCALE_ENABLED` and `PUREVISION_ENABLED`
  (both disabled by default); PureScale never stacks with the Real-ESRGAN
  super-resolution stage
- Fidelity guards mirroring the NAFNet luminance/chroma checks
- CC BY-NC-SA 4.0 (non-commercial) licensed checkpoints — evaluation/FYP use
  only until a commercial license is confirmed; see
  [purescale-purevision.md](features/purescale-purevision.md)
- Identity-preserving enhancement: the final image's faces are compared with
  the source's using the identity-grouping SFace embeddings, and the result is
  blended back toward the source when a face drifts; dark or undetectable
  faces are skipped, not penalised; see
  [identity-preserving-enhancement.md](features/identity-preserving-enhancement.md)
- Automatic black-and-white colorization (SIGGRAPH17, BSD-2-Clause,
  permissive), gated behind `COLORIZATION_ENABLED` and an automatic
  near-grayscale detector so colour photos are left untouched
- Runs as the final pipeline stage with a dedicated luminance-fidelity guard
  (Lab-space gamut clipping can otherwise distort the BT.601 luma this
  codebase measures elsewhere); see
  [colorization.md](features/colorization.md)

- Google Photos-style slider controls: `natural`/`dynamic`/`ai_max` style
  presets at enhancement time, plus a `warmth` (colour balance) tone
  control and a continuous 0-100% `intensity` blend slider in the gallery
  editor; see [slider-enhancement-controls.md](features/slider-enhancement-controls.md)

### Feature 13: Before-and-after comparison — Complete backend

Allows users to judge enhancement results visually and quantitatively.
The dedicated owner-scoped comparison API returns original, standardized, and
enhanced metadata and secure URLs plus source-resolution-normalized quality
measurements. It handles incomplete, missing-file, unauthenticated, and
cross-user requests safely. The interactive frontend view remains to be wired.

- Original and enhanced previews
- Side-by-side comparison
- Before-and-after slider
- Zoom and inspection
- Enhancement-level display
- Quality-score differences
- Selected-result download
- Processing duration, algorithm, output dimensions, file sizes, and warnings
- Authenticated endpoint: `GET /api/enhancements/results/{id}/comparison/`

## 6. Quality and facial validation

### Feature 14: Image quality assessment — Complete backend

Calculates objective signals that determine whether an image is usable. The
adaptive OpenCV router measures luminance, contrast, blur, estimated noise,
highlight/shadow clipping, resolution, normalized scores, and final decisions.

- Brightness score
- Blur and sharpness scores
- Contrast and clarity measurements
- Resolution and noise validation
- Underexposure and overexposure detection
- Configurable thresholds
- Overall quality score

### Feature 15: Face detection and validation — Complete baseline

Validates individual portraits and supported group images containing up to five people.

The OpenCV baseline, persisted decisions, automatic execution, owner-only API,
My Images and dashboard integration, review-safe decisions, detector adapter,
and feature-flagged full-resolution per-face measurements are complete.

- Face detection and count
- No-face review and support for group images containing 3–5 people
- Face-size and position validation
- Boundary and minimum-size checks
- Detector score with explicit non-probability confidence provenance
- Standardized-to-full-resolution coordinate mapping
- Per-face blur, luminance, highlight clipping, crop size, and boundary state
- Landmark and pose/occlusion availability without fabricated values
- Clear validation feedback

### Feature 16: Processing status workflow — Complete quality-decision foundation

Tracks preprocessing, face validation, quality analysis, and final quality decisions.

- Uploaded
- Pending
- Processing
- Accepted
- Rejected
- Needs review
- Failed
- Completed
- Cancelled
- Current status, timestamps, task ID, and all decision reasons

### Feature 17: Duplicate image and face detection — Complete backend baseline

Detects redundant uploads and visually repeated facial regions without exposing
one user's images or fingerprints to another user. Detection produces warnings
and review evidence; it never deletes or rejects an upload automatically.

- Owner-scoped SHA-256 exact matching with automatic confirmation
- Owner-scoped 64-bit DCT perceptual-hash comparison for modified copies
- Face-region perceptual fingerprints generated from validated face boxes
- Asynchronous Celery execution after preprocessing and face validation
- `no_duplicate`, `duplicate`, and `needs_review` decisions
- Suspected, confirmed, and dismissed match states
- Owner-only result, match, rerun, and review APIs
- PostgreSQL constraints and transactional locking for idempotent processing
- Safe generic failures with no internal hash disclosure through the API
- My Images duplicate summary integration

Learned facial embeddings and duplicate identity-group merging remain part of
Features 18 and 19. Face-region fingerprints in this baseline are similarity
evidence only and are not biometric identity recognition.

## 7. Recognition and identity organization

### Feature 18: Face recognition and identity grouping — Complete backend

Automatically organizes accepted images by person.

- FaceNet, ArcFace, or compatible embeddings
- Similarity-score calculation
- Existing-identity matching
- New identity-group creation
- Confidence thresholds
- Uncertain-match moderation
- Original and enhanced image association

Implemented with verified local YuNet and SFace artifacts, five-landmark alignment,
128-dimensional protected embeddings, owner-scoped grouping and review APIs,
idempotent Celery processing, progress/dashboard/report integration, configuration
checks, and regression tests. Candidate similarity thresholds must still be
calibrated before customer production rollout.

### Feature 19: Identity group management — Complete backend

Allows users and moderators to correct automatic grouping decisions.

- Rename a group
- Merge groups
- Move an image to another group
- Remove an incorrect image
- Set a profile image
- Delete an invalid group
- View confidence and change history

Owner-scoped rename, merge, membership move/removal, profile selection, safe empty
group deletion, confidence evidence, and immutable change-history APIs are
implemented. Frontend controls can consume the `/api/identities/` contract.

## 8. Background processing and progress

### Feature 20: Background batch processing — Complete (backend)

Moves expensive work outside request-response execution, including recognition
jobs. See T-005 in `docs/structured/tasks.md` and
`identities/test_idempotency.py` for the recognition-job retry, cancellation,
concurrency and idempotency guarantees.

- Celery preprocessing worker and Redis configuration — Complete
- Per-image and per-batch enhancement jobs — Complete
- Controlled concurrency — Complete (per-user limits)
- Retry and timeout policies — Complete (`autoretry_for` with backoff on every
  task, project-wide `CELERY_TASK_TIME_LIMIT`/`SOFT_TIME_LIMIT`)
- Preprocessing and enhancement failure isolation and batch counters — Complete
- Cancellation where safe — Complete (batch-level `cancellation_requested`,
  checked per-image in the enhancement pipeline and per-face in identity
  analysis)
- Stuck-job recovery, queue monitoring, admin failed-job visibility — Complete
  (Features 20 operations, 33)
- CPU/GPU resource limits — open; GPU workloads have not been deployed yet

### Feature 21: Live batch progress tracker — Complete backend

Reports real processing progress for multiple-image and ZIP jobs.

- Total, processed, and remaining counts
- Accepted, rejected, review, and failed counts
- Current processing stage
- Percentage completion
- Estimated completion time
- Live frontend updates
- Final completion state
- Persistent per-image stage, percentage, timestamps, and failure details
- Owner-scoped progress, failed-image retry, and cooperative cancellation APIs
- Polling-ready updates for frontend refresh every 2–3 seconds

## 9. Results, reports, and distribution

### Feature 22: Processing reports — Complete backend

Produces durable, owner-scoped evidence for each processed image and completed
batch. JSON, CSV, and PDF files are generated asynchronously from one immutable,
versioned snapshot. Frontend Downloads-page integration remains.

- Filename and processing status
- Rejection or failure reason
- Brightness, blur, sharpness, and quality scores
- Face count and confidence
- Real multi-person identity-group summaries when identity analysis is available
- Duplicate result and enhancement level
- Processing time and protected result links
- CSV, PDF, and JSON formats
- Owner-only generation, listing, retrieval, regeneration, and downloads
- Terminal-state enforcement and idempotent active requests
- UUID-based protected report storage and physical cleanup
- CSV formula-injection protection and safe PDF rendering
- Celery generation with generic queue and rendering failures
- Database integrity constraints and stored SHA-256 file checksum

### Feature 23: Image and batch downloads — Complete backend

Provides secure owner-only delivery of individual artifacts and asynchronous,
expiring batch-result packages. Frontend Downloads-page integration remains.

- Individual original, standardized, enhanced, and report downloads
- Accepted-enhanced-image ZIP packages
- Complete batch ZIP with `original/`, `standardized/`, and `enhanced/` folders
- JSON, CSV, or PDF report embedded in every package
- Safe manifest describing included and unavailable artifacts
- Celery generation with active-request idempotency and generic failures
- Authenticated owner-only package listing, retrieval, deletion, and download
- 24-hour expiry and safe failed/expired-package regeneration
- Filename sanitization, collision handling, bounded ZIP generation, and ZIP64
- SHA-256 package checksums and UUID-based protected storage
- Download history for images, reports, and generated packages
- Physical file cleanup on expiry and deletion

### Feature 24: Identity-group ZIP export - Backend complete; frontend integration planned

Packages all images of one identity into a structured archive.

- `original/` directory
- `enhanced/` directory
- `report.csv`
- Group metadata
- Secure generated archive
- Download audit event

### Feature 25: Watermark options — Complete (backend)

Adds optional branding to enhanced output copies.

- No watermark
- Small watermark
- Username watermark
- FaceCloud watermark
- Custom text, position, and opacity
- Preview before export
- Original enhanced asset remains unmodified

### Feature 26: Secure share links — Complete (backend)

Allows controlled external viewing without exposing a user account.

- Cryptographically secure share token
- Configurable expiration
- Default 24-hour option
- View-only access
- Manual revocation
- Automatic expiry
- Access tracking

## 10. Moderation, activity, and notifications

### Feature 27: Moderator review system — Done backend (T-007 Phases A-G, 2026-09-23)

Organization membership/invitation management (T-008 Phase A), the review-
decision core (T-007 Phase C, including identity-membership write-back),
assignment/escalation notifications (Phase D), batch retry/cancel (Phase E),
org-scoped review-summary/moderator-activity reports (Phase F), and
duplicate-group merging (Phase G) are all done. Frontend remains (no
frontend repo in this workspace). See `moderation/` and
`docs/features/27-31-46-moderator-organization-panel.md`.

- Organization-scoped moderator access — Done (`OrganizationScopedMixin`,
  reused unchanged from `organizations/` for every moderation endpoint)
- Active-organization member directory with no visibility into other organizations — Done (T-008 Phase A)
- Personal user content remains private and is never exposed through organization membership alone — Done (unchanged by any phase; see Feature 46)
- Moderator access is limited to organization-owned content or content explicitly shared for review — Done for the three review sources (quality, duplicate, identity); general content browsing (batch monitoring) is open
- Out-of-scope users and resources return a non-disclosing not-found response — Done
- Invite, resend, cancel, deactivate, reactivate, and remove-member workflows — Done (T-008 Phase A)
- Membership removal instead of unsafe global-account deletion — Done (T-008 Phase A)
- Protected owner, administrator, and last-owner rules — Done (T-008 Phase A)
- Organization dashboard summary and operational metrics — Partial (review-queue counts done; batch/storage/member metrics open)
- Organization batch monitoring, retry, reprocessing, and safe cancellation — Partial (list/retry/cancel done, reusing `uploads.services.retry_batch`/`cancel_batch` exactly; reprocessing at a different enhancement level is open -- it doesn't exist anywhere in the codebase today, not even an owner-facing version)
- Duplicate image, face, and identity-group review — Done (both write back to the underlying record on decision; identity review reassigns/creates a group via `identities.services.review_membership()` with a reviewer/actor split that keeps group ownership attributed to the content's real owner)
- Moderator assignment, deadline, and escalation notifications — Partial (assignment and escalation both notify -- a new explicit `assign_review()` action notifies the assigned moderator, escalation notifies every active organization owner; deadline/SLA alerts are open, no deadline concept exists on `ModeratorReview`)
- Organization-scoped CSV, PDF, and JSON reports and exports — Partial (review-summary and moderator-activity reports done, async job + download, same pattern as Feature 41's organization export; rejection-reason, quality-score, batch, and duplicate-decision report types are open)
- Low-confidence review queue — Done (`ModeratorReview`, created by additive hooks in identities/quality_control/duplicates)
- Original/enhanced comparison — Open
- Approve, reject, correct, reprocess, escalate, or keep in review — Partial (approve/reject/keep-in-review/escalate work for all three sources; `correct` fully works for identity reviews — reassign to a specific group or start a new one — but is accepted as a no-op for quality/duplicate, which have no third outcome beyond their binary AI recommendation; `reprocess` has no defined outcome yet)
- Rejection-reason management — Done (`decision_reason` on every decision, audited)
- Identity correction — Done (2026-09-23)
- Duplicate-group merging — Done (`identities.management.merge_groups()` extended with the same reviewer/actor split `review_membership()` has; `moderation.services.groups_for_duplicate_match()` resolves which identity groups a duplicate match's two images belong to)
- Immutable membership and moderator audit history — Done (`OrganizationAuditEvent`, both phases)

### Feature 28: Complete user activity timeline — Complete backend

Merges the existing authentication audit trail with a new cross-app activity
feed into one owner-scoped, paginated timeline. See
[Feature 28: Complete user activity timeline](features/28-user-activity-timeline.md).

- Upload and batch events — implemented
- Processing results (quality, enhancement, batch completion) — implemented
- Downloads and exports (reports, packages, identity-group exports) — implemented
- Group changes (rename, merge, move, remove) — implemented
- Moderator decisions — reserved category; no events until Feature 27 exists
- Share-link activity — `share_link_created` and `share_link_revoked` events (never containing the token)
- Administrative actions — covered today via the existing `AuthEvent`
  role-change/suspension events merged into the same timeline

Frontend integration remains: `GET /api/user/activity/`.

### Feature 29: In-app and email notifications — Complete backend baseline

Provides an owner-scoped notification center, unread state, preferences, and
optional Celery email delivery for currently implemented processing workflows.

- Upload-received and processing-started events
- Enhancement and quality accepted/rejected/review/failed events
- Multiple-image and ZIP batch-completion summaries
- Report-ready/failed and download-package-ready/failed events
- Paginated list, filters, unread count, mark-read, mark-all-read, and delete APIs
- Persistent per-user processing, email, identity, and live-session preferences
- Verified-address email delivery with bounded retries and safe internal links
- Idempotency protection against duplicate worker and polling events
- Owner isolation, safe metadata allowlisting, and retention cleanup
- Extension publishers for identity, sharing, moderator, and live features when
  those owning workflows are implemented

## 11. User-facing dashboards

### Feature 30: User dashboard — Planned frontend integration

Provides the primary workspace for normal users.

- Upload controls and progress
- Upload and processing history
- Accepted, rejected, and review states
- Before/after comparison
- Identity management
- Downloads, reports, shares, activity, and account settings

### Feature 31: Moderator dashboard — Planned frontend integration

Provides a protected organization-scoped workspace for membership management and
review decisions.

- Active organization and membership context
- Active-organization member directory only; no cross-organization user discovery
- Member invitation, activation, deactivation, and removal controls
- Organization-owned upload, processing, identity, and report visibility
- Explicit review-sharing workspace for user content submitted to moderators
- No access to members' personal images, passwords, tokens, sessions, or live cameras
- Dashboard cards for members, reviews, batches, storage, and recent activity
- Batch and duplicate management workspaces
- Moderator notification center
- Protected organization report and export controls
- Queue summary and filters
- Image, quality, identity, and duplicate review
- Approve, reject, correct, reprocess, and escalate actions
- Group corrections
- Membership and moderator activity history
- No access to global platform administration
- Backend-enforced tenant filtering on every list, detail, action, download, and export API

### Feature 32: Admin analytics dashboard — Complete (backend)

Provides system-wide operational and business visibility. See
[32-admin-analytics.md](features/32-admin-analytics.md).

- User, upload, enhancement, acceptance, and rejection totals — Complete
- Storage usage — Complete
- Common rejection reason — Complete
- Average processing time and system success rate — Complete
- Upload and enhancement charts — Complete
- Date, status, model, and owner filters — Complete
- Live sessions, quality/latency charts, role/moderator filters — deliberately
  not built; see the doc's "Deliberately not built" section
- Frontend dashboard page — remains

### Feature 33: Administrative management — Complete (backend)

Account management is complete; the operational monitoring and control pieces
are now complete too. See
[33-administrative-management.md](features/33-administrative-management.md).

- User listing, roles, suspension, and reactivation — Complete
- Upload and processing monitoring — Complete (failed-batch list, cross-kind failed-job list)
- Failed-job management — Complete for upload batches (admin retry, reusing the owner-facing logic); read-only visibility for every other job kind
- System-wide audit views — Complete (combined AuthEvent + ActivityEvent feed)
- Storage and threshold configuration, moderator monitoring — deliberately not
  built; see the doc's "Deliberately not built" section
- Frontend admin pages — remain

### Feature 34: System health page - Backend complete; frontend page planned

Shows whether every production dependency is operating correctly.

- API and database status
- Redis and Celery status
- AI model status
- Storage and email status
- Response and processing latency
- Pending, active, and failed jobs
- Available storage and application version

Implemented backend evidence:

- Administrator-only `GET /api/system/health/` endpoint
- Sanitized dependency results with `healthy`, `degraded`, and `unavailable` states
- HTTP 503 response when a required dependency is unavailable
- Automated authorization, aggregation, response-contract, and failure tests
- Implementation and API documentation in `docs/features/34-system-health.md`

## 12. Model evaluation and live camera scope

### Feature 35: Uploaded-image model comparison page — Complete (backend)

Supports FYP and company evaluation of enhancement approaches.

- Original input
- OpenCV result
- Zero-DCE result
- Brightness, sharpness, blur, quality, and detection comparison
- Processing-time comparison
- Recommended result
- Evaluation report

### Feature 36: Live camera face detection — Planned company requirement

Guides a user while a camera is active and validates the visible face.

- Explicit camera permission
- Start, stop, and camera selection
- Live face bounding box and count
- No-face and multiple-face warnings
- Face-size and position guidance
- Low-light and blur warnings
- Detection confidence
- Capture of a valid frame

### Feature 37: Real-time camera enhancement — Planned company requirement

Enhances controlled camera frames before capture.

- Original live preview
- OpenCV live preview
- Zero-DCE live preview
- Low, medium, high, and automatic levels
- Controlled frame rate
- Latency and FPS display
- CPU/GPU optimization
- Enhanced-frame capture

### Feature 38: Live enhancement comparison page — Planned company requirement

Compares live enhancement methods and measurements in one view.

- Original, OpenCV, and Zero-DCE feeds
- Side-by-side layout
- Live brightness, blur, and sharpness measurements
- Face-detection confidence
- Processing latency and FPS
- Recommended result
- Capture and comparison report

### Feature 39: Live processing communication — Planned

Provides authenticated, controlled transport for camera frames and results.

- WebSocket or rate-controlled frame API
- Resized and compressed frame input
- Backpressure and outdated-frame dropping
- Session start, timeout, and stop
- Detection coordinates and enhanced preview response
- Live performance monitoring

### Feature 40: Live camera privacy and security — Planned

Prevents unintended retention or misuse of camera data.

- Explicit permission and active-camera indicator
- No recording by default
- No automatic frame persistence
- Store only user-confirmed captures
- Encrypted production communication
- Temporary-frame cleanup
- Immediate session termination on camera close
- Consent and audit events

## 13. Platform, storage, and engineering quality

### Feature 41: Privacy and facial-data security — Partial

Account and upload controls exist; advanced facial-data controls remain.

- JWT, role, ownership, throttling, and secure filenames
- Protected downloads and deletion
- Three content scopes: personal, organization-owned, and explicitly shared for review
- Personal content visible only to its owner unless the owner grants a specific review scope
- Organization moderators restricted to their active organization and authorized content scopes
- No moderator or administrator access to passwords, tokens, or live-camera feeds
- Non-disclosing authorization failures to reduce user and resource enumeration
- Facial-data retention policy: `FACIAL_DATA_RETENTION_DAYS` with a purge command and Celery task — Complete backend
- Consent records: append-only, policy-versioned, enforced on identity analysis and the identity guard when `BIOMETRIC_CONSENT_REQUIRED` is on — Complete backend
- Facial-data erasure with verification (database recount and file check), also on consent withdrawal — Complete backend
- Privacy export (own data as JSON; embeddings never exported) — Complete backend
- Organization-wide export (async job, organization-owned data only, never a member's personal content) — Complete backend (2026-09-23)
- Security audit reporting: administrator aggregate report and `check --deploy` findings — Complete backend
- Encryption-at-rest configuration: embeddings use AES-256-GCM with key ids and a re-encryption command for rotation (legacy records stay readable); blank-key bug fixed and deploy checks added; stored images are now encrypted at rest too — Complete backend
- Temporary-processing cleanup: stale ZIP build files and orphaned media, orphans report-only unless enabled — Complete backend
- Frontend consent, erasure and export screens

Details: [41-privacy-and-facial-data.md](features/41-privacy-and-facial-data.md)

### Feature 42: PostgreSQL data platform — Complete

Provides the final relational database foundation for concurrent processing and analytics.

- PostgreSQL 18 database
- Dedicated application role
- Environment-based connection configuration
- SQLite-to-PostgreSQL data migration
- Sequence reset and record-count validation
- PostgreSQL test-database support
- Preserved rollback backup

### Feature 43: Private cloud storage — Partial (encryption at rest done)

Stored images are already encrypted at rest on local storage (AES-256-GCM, key ids and
rotation, conversion command): [stored-image-encryption.md](features/stored-image-encryption.md).
Moving media to a private object store, signed URLs and quotas remain.

Moves production assets from local media storage to private object storage.

- AWS S3 or compatible private bucket
- Secure object keys
- Signed download URLs
- Original, enhanced, report, and ZIP storage
- Storage quotas and analytics
- Lifecycle and deletion policies

### Feature 44: API documentation and developer support — Complete

Maintains stable integration contracts and implementation documentation.

- Root project README
- [`docs/API_ERRORS.md`](API_ERRORS.md): error shapes and what each 4xx means, checked by tests
- Schema generates with zero warnings (enforced by `config/test_openapi_schema.py`); 401 and 429 documented on every operation; request and error examples on register, login and enhancement
- Per-feature implementation documents
- Upload endpoint reference
- PostgreSQL platform documentation
- `drf-spectacular` wired in with a registered auth extension for
  `FaceCloudJWTAuthentication`; `GET /api/schema/`, `/api/docs/` (Swagger UI),
  and `/api/redoc/` are live
- Every raw `APIView` across the project (accounts, uploads, validation,
  quality_control, duplicates, notifications, downloads, reports, identities,
  enhancement — 55-56 views) has explicit `serializer_class` or
  `@extend_schema` request/response typing; `manage.py spectacular --validate
  --fail-on-warn` reports zero `Error`-level issues
- Remaining polish (optional, non-blocking): a handful of
  `SerializerMethodField` type hints and the `VerifyEmailView` path-parameter
  type default to `string`
- Planned live/WebSocket documentation (blocked on Features 36-40)

### Feature 45: Testing and quality assurance — Partial and continuous

Protects completed behavior from regression as the platform grows.

- 598 authentication, upload, preprocessing, enhancement, validation, quality,
  duplicate-detection, dashboard, report, download, ownership, cleanup, and queue tests
  passing against PostgreSQL
- Django system and migration-drift checks
- Security, ownership, ZIP, and cleanup tests
- Manual registration, token, profile, and real upload verification
- Planned enhancement, face, queue, moderator, storage, and live-load tests

### Feature 46: Organization and tenant management — In progress (T-008 Phases A-C complete, 2026-09-23)

Separates customer organizations and provides safe organization-level ownership,
membership, moderation, and data access. See the full design in
`docs/features/27-31-46-moderator-organization-panel.md`; `organizations/tests.py`
(32 tests) and `moderation/` (23 tests) cover the rules below.

- Organization creation, status, subscription, quota, and owner management — Done
  (admin-created orgs; self-service org creation is not offered, matching the
  spec's "administrator creates or approves organization" model)
- Owner, moderator, and member organization roles — Done
- Secure, expiring member invitations — Done (hashed tokens, resend/cancel/accept,
  role restricted to moderator/member — an invitation can never grant ownership)
- Organization-scoped uploads, images, jobs, groups, reports, and reviews — Partial
  (`UploadBatch`/`IdentityGroup` carry an `organization` field; `ModeratorReview`
  is the first real org-scoped query surface over that data -- quality,
  duplicate, and identity review queues. General batch/job monitoring and
  reports are still open)
- Explicit content ownership and visibility fields for personal, organization-owned, and review-shared data — Open
- Organization membership does not automatically grant access to a member's personal content — Done (every existing owner-scoped endpoint is unchanged by any phase; this is verified by the fact that no view's queryset was touched)
- Time-bounded, revocable review access where temporary moderator visibility is required — Open (a review is currently permanent once created, not time-bounded)
- Member activation, deactivation, removal, and resource-retention policies — Partial
  (activation/deactivation/removal with protected-owner and last-owner rules
  done; resource-retention/reassignment of a removed member's open reviews is
  not yet implemented)
- Prevention of cross-organization access and face matching by default — Partial
  (organization membership/invitation/audit APIs and moderator review APIs are
  isolated and tested — `OrganizationIsolationTests`,
  `ModeratorReviewIsolationTests`; general content isolation beyond the
  review workflow is open)
- Queryset, object-permission, download, report, and background-task tenant isolation — Partial (done for `ModeratorReview`; download/report/background-task isolation is open)
- Organization audit history — Done (`OrganizationAuditEvent`, immutable, written
  by every membership/invitation mutation)
- Safe backfill of existing users and content into personal organizations — Done
  (users: `organizations/migrations/0002_backfill_personal_organizations.py`;
  new users get one automatically at registration; the two content models that
  carry their own `organization` field -- `UploadBatch`, `IdentityGroup` -- are
  backfilled by `uploads/migrations/0007_backfill_batch_organization.py` and
  `identities/migrations/0005_backfill_group_organization.py`)
- Global deletion controlled by the account owner or FaceCloud administrator — Unchanged (already true; organizations do not add a new deletion path)

### Feature 47: Non-destructive photo gallery editor — Complete backend

Lets users refine a completed enhancement without replacing the original or the
primary AI result. The frontend can provide immediate client-side previews and
submit the chosen values for a full-resolution, asynchronous server render.

- Auto correction based on scene luminance percentiles and contrast
- Manual brightness, contrast, shadows, saturation, and highlights controls
- Slider API range from -100 to 100 with a recommended everyday range of -50 to 50
- Conservative clipping and contrast-loss guard
- Original pixel-grid preservation during the final render
- Immutable edit versions with one selectable current version
- Owner-only listing, status, activation, deletion, and download APIs
- Celery execution so large images do not block API requests
- Secure download history for edited outputs
- Original upload and primary enhancement are never overwritten

### Feature 48: Automatic event/timeline grouping — Done (backend)

Groups a user's photos into "moments" (an upload-time-gap-based timeline) for
a gallery moments view, similar to the automatic event grouping in most
consumer photo apps.

- Group photos into events by a configurable gap between consecutive
  `ImageUpload.created_at` timestamps within an owner (or, once relevant,
  within an organization)
- Event listing, detail and photo-list APIs (photo count, date range, cover
  photo) under `/api/events/`, in the new `events` app
- **Shipped as computed-on-demand, not stored.** Events are derived from the
  owner's uploaded photos on every call (one query of ids and timestamps), so
  a deleted or added photo can never leave stale events behind and there is no
  table to migrate or repair. An event's id is its first photo's id, which
  stays the same while later photos are appended to it. The gap defaults to
  `EVENT_GAP_HOURS` (6) and a caller may pass `gap_hours` (0.25-168); use the
  same value for the list, detail and photos calls.
- Not built: organization-wide events (owner-scoped only), user-editable event
  titles, and a real thumbnail (the cover is the original photo's download URL)

**Facial data usage: none.** Event grouping only reads upload timestamps —
no face detection, embedding, or identity data is read or written. Per
Feature 41's classification, this feature touches none of the three facial-
data categories, so it requires no biometric consent and works identically
for an account that has never granted identity-analysis consent at all.

### Feature 49: AI-generated photo collages — Done (backend)

Auto-generates a collage from a set of photos (an event's photos, or a
specific person's photos), using face-aware cropping so a collage never
crops through a detected face.

- Face-centered smart-crop: crop/resize a photo to a target aspect ratio
  using the image's own `IdentityFace.bounding_box`(es), keeping the
  detected face(s) inside the frame
- A small set of fixed collage layouts (3/4/5+ photos)
- Collage generation as a Celery job (consistent with every other
  file-producing feature in this codebase -- `downloads`, `reports`,
  Feature 46's organization export), not a synchronous request

**As shipped** (`collages` app, `/api/collages/`):
- Sources: an event (Feature 48, by event id and optional `gap_hours`) or a
  person (an existing active identity group). 3 to 6 photos are used; more are
  sampled evenly across the source, fewer than 3 is a clear 400. Layouts are
  fixed for 3, 4, 5 and 6 photos on a 1600x1200 JPEG canvas.
- Photos are cut from the **standardized (preprocessed) image**, because face
  boxes are stored in that image's coordinates. Unprocessed photos are skipped.
- The crop takes the largest window of the tile's shape and slides it so every
  detected face stays inside; if the faces together cannot fit the tile shape it
  keeps the largest face and records `faces_in_frame: false` for that photo.
- The face boxes come from the face-validation result (Haar/YuNet detection),
  not from `IdentityFace`. It is the same image-metadata class, and it works for
  every processed photo even while identity analysis (Feature 18) is switched
  off, so event collages never depend on identity data.
- One active collage per user; the file expires after `COLLAGE_TTL_HOURS` (72)
  and the beat task `collages.cleanup_expired` removes it. Stuck jobs are
  covered by the shared job reaper. Rate limit `collage`: 20/hour.
- The stored manifest holds only image ids, crop rectangles, `faces_in_frame`
  and, for a person collage, the group's display name (tests assert no
  embedding, score, centroid or distance ever appears in it).
- A person collage requires `biometric_processing_allowed`, checked at request
  time **and again when the job runs**, so a consent withdrawn in between stops
  it. It only reads memberships that already exist.
- Not built: notifications when a collage is ready (poll the detail endpoint),
  choosing photos by hand, custom layouts, organization collages.

**Facial data usage: mixed, and each part is already covered by an existing
rule.**
- The face-aware crop itself reads only `bounding_box`/`landmarks` --
  **image metadata** under Feature 41's classification ("they locate a face
  in an image... they cannot recognise a person"), not biometric data, so
  it needs no consent check of its own.
- A **person-specific** collage ("make a collage of this person") instead
  reads `IdentityGroup`/`IdentityMembership` rows, which are biometric data
  -- but this feature does not need a *new* consent gate for that, because
  those rows only exist for an owner who already passed the existing
  identity-analysis consent check (`biometric_processing_allowed`) when the
  grouping itself ran. The one new obligation is scoping: only ever query a
  person's *already-existing* group membership, never compute a new
  embedding or run new face matching as part of collage-building.
- A generated collage must never overlay or leak biometric identifiers
  (embeddings, similarity scores) into the output image or its metadata --
  only the crop coordinates and, for a person collage, the group's display
  name.

### Feature 50: Face-based visual search — Backlog

Lets a user upload or capture a query photo and find their own photos that
contain the same face -- "search by a photo" rather than by text.

- Detect and embed the largest (or all) face(s) in the uploaded query image
- Compare the query embedding against the searching user's own identity
  group centroids (reusing `identities.providers`/`_choose_group`'s
  similarity approach, not a new algorithm) and return matching photos
- Never persist the query image or its embedding beyond the request

**Facial data usage: this is the one feature here that introduces genuinely
new biometric processing, and it needs its own consent gate.** Unlike
Features 48/49, computing an embedding from a freshly uploaded query image
is new biometric processing in Feature 41's sense (it derives data that can
recognise a person), so it must be gated behind the same
`BIOMETRIC_CONSENT_REQUIRED`/`biometric_processing_allowed` check that
identity analysis already uses -- not a new, separate consent flag. Two
additional rules follow directly from Feature 41's existing "never exported"
guarantee for embeddings/centroids:
- The query embedding is computed, compared, and discarded within the
  request/job -- it is never written to a `protected_*` field or any other
  persistent store, since it was never consented to as stored biometric data,
  only as a one-time comparison.
- Results are scoped to the searching user's own (and, once organization-
  scoped search exists, their active organization's) identity data only --
  this feature must reuse the existing owner/organization isolation rules
  already enforced everywhere else in `identities`/`moderation`, never a
  platform-wide face search across accounts.

## 14. Optional future extensions

The following items remain outside the required core until the preceding features are complete:

- Mobile application
- GAN-based enhancement
- Noise and occlusion correction
- On-device preprocessing
- Federated learning
- Blockchain-backed integrity verification
- Third-party processing APIs
- Enterprise-scale workloads
- Automatic model selection
- Advanced privacy-preserving recognition

## 15. Recommended implementation sequence

1. Face embeddings and identity grouping
2. Organization and tenant management
3. Identity management and organization-scoped moderator review
4. Identity-group ZIP downloads
5. Notifications and full activity timeline
6. ~~Admin analytics and system health~~ — done (Features 32, 33, 34 backends complete)
7. ~~Uploaded-image model comparison~~ — done (Feature 35 backend complete)
8. Live camera detection
9. Real-time enhancement and communication
10. Live comparison page
11. Private cloud storage
12. Production security, load testing, and deployment

## 16. Documentation rule

Every implemented feature must finish with:

1. Updated models and migrations
2. API and service implementation
3. Permission and ownership checks
4. Automated tests
5. Manual or operational verification
6. A separate document under `docs/features/`
7. An updated status in this catalogue and its dedicated file under `docs/features/`
