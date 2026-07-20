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
| Authentication and account management | Complete |
| Role-based authorization | Complete |
| Authentication audit events | Complete |
| Single, multiple, and ZIP image upload | Complete |
| Secure local image storage and ownership | Complete |
| PostgreSQL 18 migration | Complete |
| Image preprocessing and format standardization | Complete |
| Celery preprocessing queue | Complete |
| Image enhancement | Complete backend: OpenCV, EDSR, and official Zero-DCE inference |
| Quality assessment, face validation, and final decisions | Complete backend baseline |
| Identity grouping and moderation | Planned |
| User dashboard statistics and My Images API | Complete backend; frontend integration remains |
| Reports, analytics, and notifications | Planned |
| Live camera detection and enhancement | Planned |
| Frontend dashboards | Planned for frontend integration |

### Detailed documentation available

- [Features 01–03: Authentication, accounts, authorization, and audit](features/01-authentication-and-authorization.md)
- [Features 04–08: Image uploads, batches, secure storage, and cleanup](features/02-image-upload-and-secure-storage.md)
- [Feature 09: Image preprocessing and standardization](features/09-image-preprocessing-and-standardization.md)
- [Features 10–12: Image-enhancement foundation and remaining processing work](features/10-12-image-enhancement.md)
- [Features 27, 31, and 46: Moderator organization panel and tenant privacy](features/27-31-46-moderator-organization-panel.md)
- [Feature 30: User dashboard statistics and My Images API](features/30-user-dashboard-and-my-images.md)
- [Feature 15: Face detection and validation](features/15-face-detection-and-validation.md)
- [Features 14–16: Quality scoring and final decision workflow](features/14-16-quality-decision-workflow.md)
- [Feature 42: PostgreSQL data platform](features/42-postgresql-data-platform.md)
- [Features 20–21: Background processing and live batch progress](features/20-21-live-batch-progress.md)

## 3. Identity, access, and security foundation

### Feature 01: User authentication and account management — Complete

Provides the complete account lifecycle and proves the identity of API users.

- Registration with username, email, and password
- Required, unique, normalized email addresses
- Strong password validation and secure hashing
- Email verification and verification resend
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
- Physical cleanup after image, batch, admin, cascade, or account deletion
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

- Official Epoch99 checkpoint exported reproducibly to ONNX
- Lightweight OpenCV-DNN CPU inference
- Low-light routing with adaptive OpenCV fallback
- Quality guard against destructive luminance or clipping growth
- Batch inference
- Provider, model, curve, dimensions, license, and fallback metadata
- Enhanced-image persistence
- Safe missing/corrupt-model failure handling

### Feature 13: Before-and-after comparison — Partial backend

Allows users to judge enhancement results visually and quantitatively.
Owner-scoped original, standardized, and enhanced URLs plus normalized quality
metadata are implemented. The interactive frontend comparison remains.

- Original and enhanced previews
- Side-by-side comparison
- Before-and-after slider
- Zoom and inspection
- Enhancement-level display
- Quality-score differences
- Selected-result download

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
My Images and dashboard integration, review-safe decisions, and tests are complete.

- Face detection and count
- No-face review and support for group images containing 3–5 people
- Face-size and position validation
- Boundary and minimum-size checks
- Detection confidence
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

### Feature 17: Duplicate image and face detection — Planned

Prevents redundant storage and detects visually duplicated facial content.

- Exact checksum matching
- Visual similarity checks
- Facial-embedding comparison
- Duplicate warnings
- Suspected-duplicate review state
- Duplicate identity-group detection
- Moderator confirmation and audit history

## 7. Recognition and identity organization

### Feature 18: Face recognition and identity grouping — Planned

Automatically organizes accepted images by person.

- FaceNet, ArcFace, or compatible embeddings
- Similarity-score calculation
- Existing-identity matching
- New identity-group creation
- Confidence thresholds
- Uncertain-match moderation
- Original and enhanced image association

### Feature 19: Identity group management — Planned

Allows users and moderators to correct automatic grouping decisions.

- Rename a group
- Merge groups
- Move an image to another group
- Remove an incorrect image
- Set a profile image
- Delete an invalid group
- View confidence and change history

## 8. Background processing and progress

### Feature 20: Background batch processing — Partial

Moves expensive work outside request-response execution. Preprocessing and OpenCV
enhancement jobs are implemented; recognition, advanced retry, and cancellation
workflows remain.

- Celery preprocessing worker and Redis configuration — Complete
- Per-image and per-batch enhancement jobs — Complete
- Controlled concurrency
- Retry and timeout policies
- Preprocessing and enhancement failure isolation and batch counters — Complete
- Cancellation where safe
- CPU/GPU resource limits

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

### Feature 22: Processing reports — Planned

Produces durable evidence for each processed image and batch.

- Filename and processing status
- Rejection or failure reason
- Brightness, blur, sharpness, and quality scores
- Face count and confidence
- Identity group and confidence
- Duplicate result and enhancement level
- Processing time and protected result links
- CSV, PDF, and JSON formats

### Feature 23: Image and batch downloads — Planned

Provides secure delivery of original, enhanced, and batch results.

- Individual original or enhanced downloads
- Accepted-image ZIP packages
- Full batch result ZIP
- Report downloads
- Authenticated or expiring links
- Download activity records

### Feature 24: Identity-group ZIP export — Planned

Packages all images of one identity into a structured archive.

- `original/` directory
- `enhanced/` directory
- `report.csv`
- Group metadata
- Secure generated archive
- Download audit event

### Feature 25: Watermark options — Planned

Adds optional branding to enhanced output copies.

- No watermark
- Small watermark
- Username watermark
- FaceCloud watermark
- Custom text, position, and opacity
- Preview before export
- Original enhanced asset remains unmodified

### Feature 26: Secure share links — Planned

Allows controlled external viewing without exposing a user account.

- Cryptographically secure share token
- Configurable expiration
- Default 24-hour option
- View-only access
- Manual revocation
- Automatic expiry
- Access tracking

## 10. Moderation, activity, and notifications

### Feature 27: Moderator review system — Partial

The global role and reusable permission foundation exists; organization-scoped
member management and the operational review workflow remain.

- Organization-scoped moderator access
- Active-organization member directory with no visibility into other organizations
- Personal user content remains private and is never exposed through organization membership alone
- Moderator access is limited to organization-owned content or content explicitly shared for review
- Out-of-scope users and resources return a non-disclosing not-found response
- Invite, resend, cancel, deactivate, reactivate, and remove-member workflows
- Membership removal instead of unsafe global-account deletion
- Protected owner, administrator, and last-owner rules
- Organization dashboard summary and operational metrics
- Organization batch monitoring, retry, reprocessing, and safe cancellation
- Duplicate image, face, and identity-group review
- Moderator assignment, deadline, and escalation notifications
- Organization-scoped CSV, PDF, and JSON reports and exports
- Low-confidence review queue
- Original/enhanced comparison
- Approve, reject, correct, reprocess, escalate, or keep in review
- Rejection-reason management
- Identity correction
- Duplicate-group merging
- Immutable membership and moderator audit history

### Feature 28: Complete user activity timeline — Partial

Authentication events exist; processing and content events will extend the timeline.

- Upload and batch events
- Processing results
- Moderator decisions
- Downloads and exports
- Group changes
- Share-link activity
- Administrative actions

### Feature 29: In-app and email notifications — Partial

Verification and password-reset email exist; processing notifications remain.

- Upload received
- Processing started and completed
- Accepted/rejected/review summaries
- Moderator decisions
- Report and download availability
- Share-link expiration

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

### Feature 32: Admin analytics dashboard — Planned

Provides system-wide operational and business visibility.

- User, upload, enhancement, acceptance, and rejection totals
- Live sessions and storage usage
- Common rejection reason
- Average processing time and system success rate
- Upload, quality, storage, and latency charts
- Date, user, role, status, model, and moderator filters

### Feature 33: Administrative management — Partial

Account management is complete; operational image and processing controls remain.

- User listing, roles, suspension, and reactivation
- Upload and processing monitoring
- Failed-job management
- Storage and threshold configuration
- Moderator monitoring
- System-wide audit views

### Feature 34: System health page — Planned

Shows whether every production dependency is operating correctly.

- API and database status
- Redis and Celery status
- AI model status
- Storage and email status
- Response and processing latency
- Pending, active, and failed jobs
- Available storage and application version

## 12. Model evaluation and live camera scope

### Feature 35: Uploaded-image model comparison page — Planned

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
- Facial-data retention policy
- Consent records
- Encryption-at-rest configuration
- Privacy export
- Temporary-processing cleanup
- Security audit reporting

### Feature 42: PostgreSQL data platform — Complete

Provides the final relational database foundation for concurrent processing and analytics.

- PostgreSQL 18 database
- Dedicated application role
- Environment-based connection configuration
- SQLite-to-PostgreSQL data migration
- Sequence reset and record-count validation
- PostgreSQL test-database support
- Preserved rollback backup

### Feature 43: Private cloud storage — Planned

Moves production assets from local media storage to private object storage.

- AWS S3 or compatible private bucket
- Secure object keys
- Signed download URLs
- Original, enhanced, report, and ZIP storage
- Storage quotas and analytics
- Lifecycle and deletion policies

### Feature 44: API documentation and developer support — Partial

Maintains stable integration contracts and implementation documentation.

- Root project README
- Per-feature implementation documents
- Upload endpoint reference
- PostgreSQL platform documentation
- Planned Swagger/OpenAPI integration
- Planned live/WebSocket documentation

### Feature 45: Testing and quality assurance — Partial and continuous

Protects completed behavior from regression as the platform grows.

- 64 authentication, upload, preprocessing, adaptive OpenCV enhancement, normalized-quality, restoration-routing, ownership, and queue tests passing against PostgreSQL
- Django system and migration-drift checks
- Security, ownership, ZIP, and cleanup tests
- Manual registration, token, profile, and real upload verification
- Planned enhancement, face, queue, moderator, storage, and live-load tests

### Feature 46: Organization and tenant management — Planned

Separates customer organizations and provides safe organization-level ownership,
membership, moderation, and data access.

- Organization creation, status, subscription, quota, and owner management
- Owner, moderator, and member organization roles
- Secure, expiring member invitations
- Organization-scoped uploads, images, jobs, groups, reports, and reviews
- Explicit content ownership and visibility fields for personal, organization-owned, and review-shared data
- Organization membership does not automatically grant access to a member's personal content
- Time-bounded, revocable review access where temporary moderator visibility is required
- Member activation, deactivation, removal, and resource-retention policies
- Prevention of cross-organization access and face matching by default
- Queryset, object-permission, download, report, and background-task tenant isolation
- Organization audit history
- Safe backfill of existing users and content into personal organizations
- Global deletion controlled by the account owner or FaceCloud administrator

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

1. Processing reports and secure result downloads
2. Duplicate detection
3. Face embeddings and identity grouping
4. Organization and tenant management
5. Identity management and organization-scoped moderator review
6. Notifications and full activity timeline
7. Admin analytics and system health
8. Uploaded-image model comparison
9. Live camera detection
10. Real-time enhancement and communication
11. Live comparison page
12. Private cloud storage
13. Production security, load testing, and deployment

## 16. Documentation rule

Every implemented feature must finish with:

1. Updated models and migrations
2. API and service implementation
3. Permission and ownership checks
4. Automated tests
5. Manual or operational verification
6. A separate document under `docs/features/`
7. An updated status in this catalogue and its dedicated file under `docs/features/`
