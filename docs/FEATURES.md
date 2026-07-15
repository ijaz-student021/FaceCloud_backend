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
| AI enhancement | Planned |
| Quality assessment and face validation | Planned |
| Identity grouping and moderation | Planned |
| Reports, analytics, and notifications | Planned |
| Live camera detection and enhancement | Planned |
| Frontend dashboards | Planned for frontend integration |

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

### Feature 10: Enhancement-level selection — Planned

Lets users control enhancement strength while preserving an automatic option.

- Low enhancement
- Medium enhancement
- High enhancement
- Automatic enhancement
- Per-image or batch-level selection
- Preview and selected-level metadata
- Report integration

### Feature 11: OpenCV enhancement — Planned

Provides a fast traditional baseline for correction and model comparison.

- Gamma and brightness correction
- Contrast adjustment
- Histogram equalization and CLAHE
- Sharpness adjustment
- Optional noise reduction
- CPU-based execution
- Processing-time and quality measurements

### Feature 12: Zero-DCE AI enhancement — Planned

Uses deep learning to improve low-light images while preserving facial detail.

- Model loading and warm-up
- CPU and GPU execution paths
- Adaptive low-light correction
- Overexposure control
- Batch inference
- Model/version metadata
- Enhanced-image persistence
- Failure and timeout handling

### Feature 13: Before-and-after comparison — Planned

Allows users to judge enhancement results visually and quantitatively.

- Original and enhanced previews
- Side-by-side comparison
- Before-and-after slider
- Zoom and inspection
- Enhancement-level display
- Quality-score differences
- Selected-result download

## 6. Quality and facial validation

### Feature 14: Image quality assessment — Planned

Calculates objective signals that determine whether an image is usable.

- Brightness score
- Blur and sharpness scores
- Contrast and clarity measurements
- Resolution and noise validation
- Underexposure and overexposure detection
- Configurable thresholds
- Overall quality score

### Feature 15: Face detection and validation — Planned

Ensures the image contains exactly one suitable, visible face.

- Face detection and count
- No-face and multiple-face rejection
- Face-size and position validation
- Partial-face and visibility checks
- Detection confidence
- Clear validation feedback

### Feature 16: Processing status workflow — Planned

Tracks each image through the complete processing lifecycle.

- Uploaded
- Pending
- Processing
- Accepted
- Rejected
- Needs review
- Failed
- Completed
- Cancelled
- Status history, timestamps, and reasons

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

Moves expensive work outside request-response execution. Preprocessing jobs are
complete; enhancement and recognition jobs will reuse this foundation.

- Celery preprocessing worker and Redis configuration — Complete
- Per-image and per-batch jobs
- Controlled concurrency
- Retry and timeout policies
- Preprocessing failure isolation and batch counters — Complete
- Cancellation where safe
- CPU/GPU resource limits

### Feature 21: Live batch progress tracker — Planned

Reports real processing progress for multiple-image and ZIP jobs.

- Total, processed, and remaining counts
- Accepted, rejected, review, and failed counts
- Current processing stage
- Percentage completion
- Estimated completion time
- Live frontend updates
- Final completion state

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

The role and reusable permission foundation exists; the operational review workflow remains.

- Low-confidence review queue
- Original/enhanced comparison
- Approve, reject, or keep in review
- Rejection-reason management
- Identity correction
- Duplicate-group merging
- Moderator audit history

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

Provides a protected operational workspace for review decisions.

- Queue summary and filters
- Image, quality, identity, and duplicate review
- Approve/reject actions
- Group corrections
- Moderator activity history

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
- Database migration guide
- Reusable documentation template
- Planned Swagger/OpenAPI integration
- Planned live/WebSocket documentation

### Feature 45: Testing and quality assurance — Partial and continuous

Protects completed behavior from regression as the platform grows.

- 50 authentication, upload, preprocessing, and queue tests passing against PostgreSQL
- Django system and migration-drift checks
- Security, ownership, ZIP, and cleanup tests
- Manual registration, token, profile, and real upload verification
- Planned enhancement, face, queue, moderator, storage, and live-load tests

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

1. Image preprocessing and standardization
2. OpenCV enhancement baseline
3. Zero-DCE integration
4. Quality assessment
5. Face detection and validation
6. Processing state model
7. Celery and Redis background processing
8. Live batch progress
9. Reports and result downloads
10. Duplicate detection
11. Face embeddings and identity grouping
12. Identity management and moderator review
13. Notifications and full activity timeline
14. Admin analytics and system health
15. Uploaded-image model comparison
16. Live camera detection
17. Real-time enhancement and communication
18. Live comparison page
19. Private cloud storage
20. Production security, load testing, and deployment

## 16. Documentation rule

Every implemented feature must finish with:

1. Updated models and migrations
2. API and service implementation
3. Permission and ownership checks
4. Automated tests
5. Manual or operational verification
6. A separate document under `docs/features/`
7. An updated status in this catalogue and `docs/README.md`
