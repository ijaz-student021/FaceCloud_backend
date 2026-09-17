# FaceCloud Citation-Backed Feature Verification Report

## Headline finding

**Features 18 and 19 are real backend implementations in the current repository,
but neither meets the strict “fully complete” evidence standard: Feature 18 lacks
same-person/different-person end-to-end grouping tests and calibrated production
threshold evidence, while Feature 19 lacks endpoint-level tests for each management
operation.**

## 1. Audit rules and verification result

This report uses executable source, migrations, URL routes and named automated tests
as evidence. Settings, comments and planning documents are not accepted alone as
proof. Missing evidence is written as **NOT FOUND**.

Verification executed on 17 September 2026:

```powershell
.\venv\Scripts\python.exe manage.py test identities validation enhancement.test_hd_restoration --keepdb --verbosity 1
```

Result: **25 tests passed** in 40.253 seconds. Django checks reported no issues.

## 2. Priority 1 — Feature 18

### Identity models

**FOUND.**

- `identities/models.py:10` — `IdentityAnalysis`
- `identities/models.py:44` — `IdentityFace`
- `identities/models.py:69` — `IdentityGroup`
- `identities/models.py:94` — `IdentityMembership`
- `identities/models.py:131` — `IdentityChange`

The implementation calls the person-folder entity `IdentityGroup`.

### Applied migrations

**FOUND.**

- `identities/migrations/0001_initial.py:19` — creates `IdentityAnalysis`
- `identities/migrations/0001_initial.py:46` — creates `IdentityFace`
- `identities/migrations/0001_initial.py:68` — creates `IdentityGroup`
- `identities/migrations/0001_initial.py:88` — creates `IdentityMembership`
- `identities/migrations/0002_identitychange.py` — identity-change history

`manage.py showmigrations identities` reported both migrations as applied (`[X]`).

### Real SFace embedding extraction and storage

**FOUND.**

- `identities/providers.py:54` — `SFaceOnnxProvider`
- `identities/providers.py:70` — creates OpenCV `FaceRecognizerSF`
- `identities/providers.py:96` — `SFaceOnnxProvider.embed`
- `identities/providers.py:100` — calls `self._recognizer.feature(...)`
- `identities/services.py:76` — `process_identity_analysis`
- `identities/services.py:100` — calls and validates `provider.embed(crop)`
- `identities/services.py:114` — stores a protected vector in `IdentityFace`
- `identities/storage.py:24` — `protect_embedding`

Passing test:

- `identities/tests.py:63` —
  `test_verified_model_extracts_normalized_feature_from_yunet_geometry`

It loads SFace, aligns using five landmarks, extracts a vector, and checks its
dimension, finite values and unit norm.

### Similarity comparison and grouping

**FOUND.**

- `identities/providers.py:39` — `similarity` computes the dot product of normalized
  embeddings, equivalent to cosine similarity.
- `identities/services.py:50` — `_refresh_group` calculates a normalized centroid.
- `identities/services.py:64` — `_choose_group` scores owner-scoped candidates and
  applies new/match/review thresholds.
- `identities/services.py:117` — creates a new group.
- `identities/services.py:123` — records an automatic centroid-cosine match.
- `identities/services.py:127` — records an ambiguous assignment for review.

Owner isolation evidence:

- `identities/services.py:65` — filters candidates by `owner=owner`.
- `identities/tests.py:95` —
  `test_group_list_is_owner_scoped_and_hides_private_fields`
- `identities/tests.py:104` — `test_other_owner_group_identifier_returns_404`

### Required same-person/different-person grouping test

**NOT FOUND.**

No test processes two different people and asserts separate groups, then processes
the same person twice and asserts one shared group.

Related but insufficient evidence:

- `identities/tests.py:49` — `test_provider_normalizes_and_compares_vectors` compares
  one synthetic vector with itself.
- `identities/tests.py:63` verifies one SFace extraction but not grouping.
- Management tests insert protected vectors directly and do not execute the complete
  YuNet → SFace → grouping path.

### Feature 18 verdict

**Backend implemented; not fully acceptance-verified.** The claim that no group
model, embedding extraction or similarity logic exists is contradicted by current
executable code. Production completion still requires real same/different-person
tests and calibrated thresholds.

## 3. Priority 1 — Feature 19

Routes are mounted under `/api/identities/` by `config/urls.py:29`.

| Operation | Route | View | Service | Automated evidence |
|---|---|---|---|---|
| Rename | `identities/urls.py:15` | `IdentityGroupRenameView`, `identities/views.py:77` | `rename_group`, `identities/management.py:14` | Service-level coverage in `test_rename_move_profile_remove_and_delete_are_audited`, `identities/tests.py:128`; endpoint test **NOT FOUND** |
| Merge | `identities/urls.py:16` | `IdentityGroupMergeView`, `identities/views.py:87` | `merge_groups`, `identities/management.py:29` | `test_merge_moves_members_and_marks_source`, `identities/tests.py:138`; endpoint test **NOT FOUND** |
| Move | `identities/urls.py:21` | `IdentityMembershipMoveView`, `identities/views.py:99` | `move_membership`, `identities/management.py:55` | Service coverage at `identities/tests.py:128`; endpoint test **NOT FOUND** |
| Remove | `identities/urls.py:22` | `IdentityMembershipRemoveView`, `identities/views.py:110` | `remove_membership`, `identities/management.py:80` | Service coverage at `identities/tests.py:128`; endpoint test **NOT FOUND** |
| Set profile | `identities/urls.py:17` | `IdentityGroupProfileView`, `identities/views.py:120` | `set_profile_membership`, `identities/management.py:100` | Service coverage at `identities/tests.py:128`; endpoint test **NOT FOUND** |
| Delete | `identities/urls.py:18` | `IdentityGroupDeleteView`, `identities/views.py:131` | `delete_empty_group`, `identities/management.py:111` | `identities/tests.py:128` and `test_nonempty_group_cannot_be_deleted`, line 147; endpoint test **NOT FOUND** |

Cross-owner service protection is tested by
`test_cross_owner_management_is_rejected` at `identities/tests.py:151`.
Write-endpoint owner-scope tests for every operation are **NOT FOUND**.

### Feature 19 verdict

**Routes and domain services implemented; endpoint acceptance suite incomplete.**
It should not be marked fully complete until authenticated API tests cover every
operation, invalid input, cross-owner access and audit output.

## 4. Priority 2 — Feature 15 face detection

**YuNet is implemented and active locally; Haar remains the default configuration.**

- `validation/services.py:63` — `OpenCVYuNetFaceDetector`
- `validation/services.py:74` — calls `cv2.FaceDetectorYN_create`
- `validation/services.py:115` — `get_face_detector`
- `validation/services.py:117` — selects YuNet for `yunet_v1`
- `config/settings.py:269` — default provider is `haar_v1`
- Runtime inspection on 17 September 2026 resolved `yunet_v1`.
- `validation/tests.py:77` —
  `test_installed_yunet_model_supports_dynamic_input_and_contract`
- `validation/tests.py:256` —
  `test_no_face_requires_review_and_three_to_five_faces_are_allowed`

Verdict: **YuNet functionality is real, but deployments without an environment
override use Haar.**

## 5. Priority 2 — Feature 12 restoration claims

### Real-ESRGAN

**FOUND behind a disabled rollout flag.**

- `config/settings.py:190` — `HD_RESTORATION_ENABLED`, default false
- `config/settings.py:200` — Real-ESRGAN model path
- `enhancement/hd_restoration.py:275` — `upscale_with_realesrgan`
- `enhancement/services.py:678` — `hd_restoration_stage`
- `enhancement/services.py:693` — disabled-stage fallback
- `enhancement/test_hd_restoration.py:30` —
  `test_enabled_hd_restoration_returns_auditable_2x_output`
- `enhancement/test_hd_restoration.py:47` —
  `test_deblur_failure_still_allows_safe_super_resolution`

The pipeline tests mock model calls. A committed automated test that executes the
full Real-ESRGAN checkpoint on a representative image and asserts fidelity is
**NOT FOUND**.

### NAFNet

**FOUND in code but disabled.**

- `config/settings.py:191` — `NAFNET_DEBLUR_ENABLED`, default false
- `enhancement/hd_restoration.py:255` — `deblur_with_nafnet`
- `enhancement/services.py:699` — conditional NAFNet execution
- `enhancement/hd_restoration.py:263` — chroma/luminance fidelity guard

A committed regression test reproducing the visually corrupt checkpoint result is
**NOT FOUND**. The precise evidence-based claim is that NAFNet is implemented,
guarded and disabled; the visual-failure diagnosis is operational evidence.

## 6. Priority 2 — Feature 20 background reliability

### Found

- Late acknowledgement: `uploads/tasks.py:9`, `enhancement/tasks.py:14`, `:58`,
  `:123`, and `identities/tasks.py:7`.
- Stale task protection: `uploads/tasks.py:16`, `enhancement/tasks.py:27`, `:65`,
  `:133`, and `identities/tasks.py:10`.
- Cooperative cancellation: `uploads/tasks.py:38`, `enhancement/tasks.py:21`, `:75`,
  and `identities/services.py:81`.
- Per-image failure isolation: `uploads/tasks.py:47` and
  `enhancement/tasks.py:87`.
- Hard/soft global timeouts: `config/settings.py:345–346`.
- Tests:
  - `uploads/tests.py:119` —
    `test_batch_cancel_sets_cooperative_cancellation_state`
  - `uploads/tests.py:129` — `test_batch_retry_only_requeues_failed_images`
  - `uploads/tests.py:512` — `test_safe_output_naming_and_idempotent_reuse`
  - `uploads/tests.py:583` —
    `test_batch_reports_queue_unavailable_without_losing_originals`

### Not found

- Automatic bounded Celery retries on preprocessing, enhancement and identity
  tasks: **NOT FOUND** (`autoretry_for`/`max_retries` absent).
- Cancellation checks between every face in identity inference: **NOT FOUND**.
- Dedicated CPU/GPU concurrency enforcement: **NOT FOUND**.
- Queue monitoring and dead-job recovery: **NOT FOUND**.
- End-to-end stale identity-task overwrite test: **NOT FOUND**.

Verdict: **Partial.** Several reliability controls exist, but the complete claim is
unsupported.

## 7. Priority 2 — Feature 41 privacy and biometric protection

### Found

- Owner scope: `identities/services.py:65`; `identities/views.py:40`, `:48`, `:63`.
- Protected embeddings: `protect_embedding`, `identities/storage.py:24`, and
  `reveal_embedding`, line 33.
- Integrity tag: `identities/storage.py:29`.
- Tests:
  - `identities/tests.py:25` —
    `test_embedding_round_trip_is_not_plain_float_storage`
  - `identities/tests.py:31` —
    `test_tampering_fails_without_disclosing_payload`

### Not found

- Explicit facial-recognition consent workflow: **NOT FOUND**.
- User-configurable facial/embedding retention: **NOT FOUND**.
- Biometric-data export workflow: **NOT FOUND**.
- Verified biometric erasure workflow: **NOT FOUND**.
- Deployment database/object-storage encryption-at-rest configuration in
  application code: **NOT FOUND**.

The custom protected format supplies application-level confidentiality and tamper
detection, but it is not a complete privacy or infrastructure-encryption solution.

Verdict: **Partial.** Ownership and embedding protection exist; consent, retention,
export, erasure and deployment encryption remain incomplete.

## 8. Ten-day Feature 18 estimate

Feature 18 is already a real backend implementation. Closing its missing evidence
and production gates realistically requires **4–6 focused engineering days**, if a
consented dataset and license record are immediately available.

| Day | Work |
|---:|---|
| 1 | Deterministic grouping tests for match, new group and ambiguity |
| 2 | Real same-person/different-person end-to-end fixtures and assertions |
| 3 | Threshold evaluation and false-match/false-new-group documentation |
| 4 | Complete API ownership tests and background stale/cancel tests |
| 5 | Consent gate, retention behavior and deletion verification |
| 6 | Operational run, regression suite, documentation and rollout decision |

Without a consented dataset or license evidence, production completion is blocked
regardless of engineering time. Reserve the remaining four days for Person Folders
frontend integration and defect correction rather than expanding scope.

## 9. Corrected status summary

| Feature | Evidence-based status |
|---:|---|
| 18 | Backend implemented; end-to-end grouping acceptance and calibration incomplete |
| 19 | Routes/services implemented; per-endpoint API tests and frontend incomplete |
| 15 | YuNet implemented and locally active; Haar is the configuration default |
| 12 | Restoration adapters exist behind disabled flags; full-model regression QA incomplete |
| 20 | Partial controls; automatic retries and full resource/monitoring controls missing |
| 41 | Owner isolation and protected embeddings exist; consent/retention/export/erasure incomplete |

## 10. Complete 47-feature list

This section provides the complete catalogue in one place. Detailed evidence for
the disputed features remains in Sections 2–7. Features marked backend complete
still require the stated frontend or production acceptance work.

| No. | Feature | Status | Purpose |
|---:|---|---|---|
| 01 | User authentication and account management | Complete | Register, verify, authenticate and manage user accounts safely |
| 02 | Role-based authorization | Complete | Separate user, moderator and administrator permissions |
| 03 | Authentication audit trail | Complete | Record security-sensitive account and role events |
| 04 | Single-image upload | Complete | Safely upload and validate one image |
| 05 | Multiple-image upload | Complete | Validate and process multiple files independently |
| 06 | ZIP image upload | Complete | Safely ingest image archives with ZIP attack protections |
| 07 | Secure image storage and cleanup | Complete | Protect stored files and remove bytes when records are deleted |
| 08 | Upload batch management | Complete | Track ingestion type, counts, failures and ownership |
| 09 | Image preprocessing and standardization | Complete | Create canonical processing copies while preserving originals |
| 10 | Enhancement-level selection | Backend complete | Select low, medium, high or automatic enhancement strength |
| 11 | Adaptive OpenCV enhancement | Complete baseline | Improve exposure, contrast, noise and sharpness conservatively |
| 12 | Private AI enhancement | Backend implemented; rollout QA incomplete | Apply learned low-light correction and optional restoration |
| 13 | Before-and-after comparison | Backend complete | Compare original and enhanced derivatives with metrics |
| 14 | Image-quality assessment | Backend complete | Measure lighting, contrast, noise, blur, clipping and resolution |
| 15 | Face detection and validation | Baseline complete; deployment-dependent | Detect multiple faces and determine processing suitability |
| 16 | Processing status and quality decisions | Foundation complete | Route images to accepted, rejected or review outcomes |
| 17 | Duplicate image and face-region detection | Backend baseline complete | Detect repeated files and visually similar owner-scoped content |
| 18 | Face recognition and identity grouping | Backend implemented; acceptance incomplete | Extract SFace embeddings and group recurring people |
| 19 | Identity group management | Backend implemented; endpoint/frontend incomplete | Rename, merge, move, remove and manage person folders |
| 20 | Background batch processing | Partial | Run processing reliably through Celery outside HTTP requests |
| 21 | Live batch progress tracker | Backend complete | Report real processing stages and batch outcomes |
| 22 | Processing reports | Backend complete | Generate owner-scoped JSON, CSV and PDF reports |
| 23 | Secure image and batch downloads | Backend complete | Deliver protected files and generated ZIP packages |
| 24 | Identity-group ZIP export | Backend complete; frontend planned | Securely export one owned person group with selected image variants and report |
| 25 | Watermark options | Planned | Apply optional branding to exported copies |
| 26 | Secure expiring share links | Planned | Provide revocable, time-limited, view-only external access |
| 27 | Moderator review system | Partial | Resolve uncertain quality and identity decisions |
| 28 | Complete activity timeline | Partial | Show account, processing, identity, download and sharing history |
| 29 | In-app and email notifications | Backend baseline complete | Notify users about account and processing events |
| 30 | User dashboard | Backend available; frontend planned | Present real statistics, images, progress and user activity |
| 31 | Moderator dashboard | Frontend planned | Present organization-scoped review queues and decisions |
| 32 | Admin analytics dashboard | Planned | Show platform usage, reliability and processing trends |
| 33 | Administrative management | Partial | Manage users plus platform processing and configuration |
| 34 | System health page | Backend complete; frontend planned | Monitor API, database, Redis, workers, storage, email, models and job counts |
| 35 | Uploaded-image model comparison page | Planned | Compare original, OpenCV and approved AI outputs |
| 36 | Live camera face detection | Planned | Detect faces and give capture guidance from camera frames |
| 37 | Real-time camera enhancement | Planned | Preview bounded enhancement before confirming a snapshot |
| 38 | Live enhancement comparison | Planned | Compare original and processed camera frames and metrics |
| 39 | Live processing communication | Planned | Transport frames and results with backpressure and session control |
| 40 | Live camera privacy and security | Planned | Enforce consent, visible camera state and temporary-frame cleanup |
| 41 | Privacy and facial-data security | Partial | Protect biometric data through consent, retention and erasure controls |
| 42 | PostgreSQL data platform | Complete | Provide transactional authoritative application storage |
| 43 | Private cloud storage | Planned | Store private media in protected object storage |
| 44 | API documentation and developer support | Partial | Supply complete OpenAPI contracts, examples and deployment guidance |
| 45 | Testing and quality assurance | Partial and continuous | Validate functionality, security, performance and model quality |
| 46 | Organization and tenant management | Planned | Isolate memberships, moderators, data and policies by tenant |
| 47 | Non-destructive photo gallery editor | Backend complete | Edit through versioned derivatives without overwriting originals |

### Counts by current status class

- **Complete foundation:** Features 01–09 and 42 — 10 features
- **Backend complete, implemented or usable baseline:** Features 10–19, 21–23,
  29 and 47 — 15 features
- **Partial:** Features 20, 27, 28, 33, 41, 44 and 45 — 7 features
- **Planned:** Features 24–26, 30–32, 34–40, 43 and 46 — 15 features
- **Total:** 47 features
