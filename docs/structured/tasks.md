# FaceCloud Engineering Task Plan

## Status definitions

- **Done:** implementation and required verification completed
- **In progress:** active work exists but acceptance criteria are incomplete
- **Next:** approved immediate work
- **Backlog:** planned after higher-priority dependencies
- **Blocked:** cannot proceed without a named external dependency or decision

## 1. Recently completed

| Task | Status | Evidence |
|---|---|---|
| Feature 18 recognition baseline | Done | YuNet/SFace adapters, models, services and tests |
| Feature 18 deterministic grouping unit tests | Done (2026-09-18) | `ChooseGroupDecisionTests`, `RefreshGroupCentroidTests`, `identities/tests.py` |
| Feature 19 group management backend | Done | Identity-group and membership APIs/tests |
| Feature 19 endpoint acceptance tests | Done (2026-09-18) | `IdentityGroupManagementApiTests`, 14 tests, `identities/tests.py` |
| Feature 20 bounded Celery auto-retry | Done (2026-09-18) | `autoretry_for` on transient DB errors, all 8 task files |
| Feature 20 per-face cancellation | Done (2026-09-18) | `identities/services.py:76,101` + test |
| Feature 44 OpenAPI schema mounted | Done (2026-09-18) | `/api/schema/`, `/api/docs/`, `/api/redoc/` |
| Feature 44 all raw APIViews typed | Done (2026-09-18) | 55-56 views across 10 apps |
| Feature 44 schema warnings | Done (2026-09-19) | 43 warnings fixed (untyped method fields, enum name clashes, path params); `spectacular --validate --fail-on-warn` is now a test |
| Feature 44 error catalogue and examples | Done (2026-09-19) | `docs/API_ERRORS.md`, shared 401/429 responses, examples for register, login and enhancement |
| Feature 28 unified activity timeline | Done (2026-09-18) | `activity` app, `GET /api/user/activity/`, `activity/tests.py`, 11 tests |
| PureScale/PureVision restoration adapters | Done (2026-09-18) | `enhancement/purescale.py`, `enhancement/purevision.py`, checksum-verified real checkpoints, disabled by default; `features/purescale-purevision.md` |
| Automatic B&W colorization (SIGGRAPH17) | Done (2026-09-18) | `enhancement/colorization.py`, final pipeline stage, luminance-fidelity guard, disabled by default; `features/colorization.md` |
| Feature 41 privacy backend (consent, erasure + verification, retention, export, audit) | Done (2026-09-19) | `privacy` app, 36 tests; `features/41-privacy-and-facial-data.md` |
| Identity-preserving enhancement (identity guard) | Done (2026-09-19) | `enhancement/identity_guard.py`, 16 tests, measured on real photos; `features/identity-preserving-enhancement.md` |
| Slider enhancement controls (style presets, warmth, intensity blend) | Done (2026-09-18) | `natural`/`dynamic`/`ai_max` style field, `warmth`/`intensity` editor controls; `features/slider-enhancement-controls.md` |
| Verify NAFNet and Real-ESRGAN files | Done | Restricted loads, tensor validation and hashes |
| Implement optional HD restoration adapter | Done | Checksums, tiling, fallbacks and enhancement tests |
| Add HD-restoration documentation | Done | `features/hd-deblur-super-resolution-pipeline.md` |

## 2. Immediate tasks

### T-001 — Complete Feature 18 production calibration

- **Status:** Blocked — needs a consented evaluation photo set (deterministic
  decision-logic unit tests are done; this task covers the real-photo
  calibration step specifically)
- Build a consented, tenant-safe evaluation dataset.
- Measure false matches, missed matches, ambiguity, and demographic performance.
- Select production SFace thresholds and document the evaluation.
- Enable automatic analysis only after acceptance criteria pass.

### T-002 — Complete Feature 19 frontend integration

- **Status:** Blocked — no frontend repository present in this workspace.
  Backend/endpoint work for this task is done (see T-034 register note).
- Connect the Person Folders page to identity-group APIs.
- Add rename, merge, split, confirm, reject, remove, and undo interactions.
- Display confidence and review states without exposing raw embeddings.

### T-003 — Replace or correctly run NAFNet

- **Status:** Blocked
- **Blocker:** Current checkpoint/runtime produces corrupted output.
- Evaluate the official `NAFNetLocal` runtime and local-pooling conversion.
- Reject any candidate with tiling, chroma corruption, or identity drift.
- Keep `NAFNET_DEBLUR_ENABLED=False` until visual QA passes.

### T-004 — Productionize Real-ESRGAN

- **Status:** In progress
- Provision a dedicated GPU Celery queue.
- Benchmark tile sizes, memory, throughput, and cancellation.
- Add end-to-end tests for 2× output and provenance.
- Enable the feature gradually after operational QA.

### T-005 — Finish background recognition jobs

- **Status:** Done — bounded retry policy (transient DB errors) and
  per-face cancellation done 2026-09-18; stuck-job recovery, per-user
  concurrency limits and queue monitoring done 2026-09-19; idempotency
  tests done 2026-09-22 (`identities/test_idempotency.py`, 6 tests):
  reprocessing a completed or needs-review analysis is a true no-op (no
  provider calls, not just an unchanged end state); the task-level
  "superseded" guard and the service-level mid-flight guard are both
  proven, not just present; redelivering the same task id before
  completion does not duplicate faces or memberships.
- Add retry policy, cancellation, resource controls, and idempotency tests. — Done
- Ensure progress accurately distinguishes enhancement, validation, and identity work.

## 3. Privacy and moderation tasks

### T-006 — Biometric consent and retention

- **Status:** Done (backend) — 2026-09-19: `privacy` app, 71 tests plus 17
  cipher tests: consent records, retention purge, erasure with verification,
  personal export, audit report, deploy checks, AES-256-GCM embedding
  encryption with key ids and a re-encryption command, and stale-temp-file /
  orphaned-media cleanup. 2026-09-23: organization-wide export
  (`privacy.OrganizationExport`, 16 tests) -- an async job (queued/
  generating/ready/failed, downloadable once ready, 24h TTL) covering
  organization-owned batches/images/reviews while deliberately excluding a
  member's personal consent/login/activity history, the same isolation rule
  every other org-scoped endpoint in this codebase follows. All periodic
  jobs are on the beat schedule. Open: frontend screens only.
- Record consent purpose and timestamp. — Done
- Add configurable embedding retention and deletion. — Done
- Add account export and deletion verification. — Done, including
  organization-wide export (2026-09-23)
- Document encryption-at-rest requirements. — Done; see `features/41-privacy-and-facial-data.md`

### T-007 — Moderator workflow completion

- **Status:** Done (backend) — Phase C (review-decision core) done 2026-09-23:
  new `moderation` app with `ModeratorReview`, created by additive hooks next
  to the three pipelines that already land on an uncertain result
  (`identities`, `quality_control`, `duplicates` services.py -- no existing
  decision logic was rewritten). Org-scoped list/detail/decision/escalate
  endpoints and a dashboard-counts endpoint, all gated to the organization's
  owner/moderator role via the same `OrganizationScopedMixin` the
  organizations app uses, so cross-org access is a 404 exactly like every
  other org-scoped endpoint. Decisions write back into the underlying
  quality decision / duplicate match for the `approve`/`reject` actions.
  23 tests (`moderation/test_hooks.py`, `moderation/tests.py`).
  Identity-membership write-back done 2026-09-23 (same day, a follow-up
  once the review-decision core landed): `identities.services.
  review_membership()` now takes the membership's real owner as `reviewer`
  (the authorization gate is unchanged -- still requires the row's actual
  owner) and a separate `actor` for who is actually deciding, so a
  moderator can decide on the owner's behalf without misattributing group
  ownership or the `IdentityChange` audit trail to the moderator. `approve`/
  `reject`/`correct` (with or without an explicit `group_id`) all write
  through to the real `IdentityMembership`. Caught and fixed along the way:
  a real (if previously unreachable) inconsistency between the model's
  `identity_group_required_except_review` CheckConstraint and
  `review_membership()`'s own logic -- the constraint only exempted
  `needs_review` from requiring a group, not `rejected`, even though the
  code has always treated `reject` as not needing one; migration
  `identities.0006` corrects the constraint. 15 more tests
  (`identities/test_review_membership.py` -- review_membership() itself had
  zero prior coverage; `moderation/test_identity_writeback.py`).
  Moderator notifications done 2026-09-23 (Phase D of the remainder):
  `notifications` gained `MODERATOR_REVIEW_ASSIGNED`/`_ESCALATED` event types
  and a `moderator_review_updates` preference. A new explicit
  `moderation.services.assign_review()` action (previously only an implicit
  side effect of deciding/escalating) notifies the assigned moderator;
  escalation notifies every active organization owner (a bounded fan-out --
  realistically 1-2 owners -- unlike the deferred all-moderators
  batch-failure case). 8 tests (`moderation/test_notifications.py`).
  Deliberately not attempted, flagged rather than dropped: deadline/SLA
  alerts (no deadline field exists on `ModeratorReview`, and none is being
  invented for this); batch-failure-to-moderators (needs a genuinely new
  multi-recipient fan-out pattern, bigger than the owner-only escalation
  case above).
  Batch-management moderator actions done 2026-09-23 (Phase E): org-scoped
  retry/cancel, both reusing `uploads.services.retry_batch`/`cancel_batch`
  exactly -- `cancel_batch` was newly extracted from the owner-facing view's
  inline logic (previously not a standalone function, unlike `retry_batch`
  which the admin endpoint already reused) so this and the owner endpoint
  share one implementation, not a duplicate. Reprocessing at a different
  enhancement level is still explicitly deferred: it does not exist
  anywhere in the codebase today, not even an owner-facing version, and is
  real new design work rather than a reuse. 8 tests
  (`moderation/test_batch_management.py`, including one guarding that the
  owner-facing cancel endpoint's behavior is unchanged by the extraction).
  Org-scoped reports done 2026-09-23 (Phase F), scoped to two report types
  (review-summary, moderator-activity) rather than the original six:
  `reports/renderers.py`'s CSV/PDF rendering is hard-coded to a per-image row
  shape, so covering more types is proportionally more new code, not more
  reuse. `ModeratorReport` follows `privacy.OrganizationExport`'s async job
  shape (`ProcessingReport` has no `organization` FK and wasn't reusable
  unmodified). `reports/renderers.py`'s `_table_style`/`_csv_safe` were
  promoted to public (`table_style`/`csv_safe`, no behavior change) so the
  new `moderation/renderers.py` could import and reuse them instead of
  duplicating ~10 lines. Real bug caught and fixed, not worked around: the
  payload builders stored raw `datetime` objects in a plain `JSONField`
  (no custom encoder), which fails on save -- fixed by `.isoformat()`-ing
  every datetime before it goes in the payload, matching the exact
  convention `reports/services.py`'s own `build_report_payload` already
  uses. 14 tests (`moderation/test_reports.py`).
  Duplicate-group merging done 2026-09-23 (Phase G, the last of the four):
  there is no `DuplicateMatch`-level merge concept anywhere -- the real
  primitive is `identities.management.merge_groups()` (self-service only,
  same `owner=reviewer`-gated shape `review_membership()` had before its
  own fix), and the link from a duplicate review to *which* identity groups
  are involved wasn't modeled anywhere, so both were built here.
  `merge_groups()` got the identical reviewer/actor split
  `review_membership()` already has; `moderation.services.
  groups_for_duplicate_match()` walks a match's two images through their
  faces/memberships to the (possibly several, since one image can have
  multiple faces) candidate groups on each side, auto-resolving when
  unambiguous and requiring an explicit `group_id` otherwise. Kept as its
  own endpoint rather than overloading `decide_review`'s `correct` action,
  since confirming a duplicate match and merging the identity groups it
  points at are genuinely different operations. 9 tests
  (`moderation/test_duplicate_group_merge.py`).
  T-007's backend is now complete across all four remaining pieces from the
  original Phase C note. Still open, flagged throughout rather than
  claimed done: deadline/SLA alerts, batch-failure-to-moderators
  notifications, reprocessing at a different enhancement level,
  rejection-reason/quality-score/batch/duplicate-decision report types, and
  the frontend (no frontend repo in this workspace).
- Organization-scoped queues and decisions — Done for all three review
  sources (quality, duplicate, identity)
- Corrections, reasons, and complete audit history — Done (every decision/
  escalation writes an `OrganizationAuditEvent`; `correct` has a defined
  outcome for all three sources -- reassign or start a new group for
  identity, and is accepted but a no-op write-back for quality/duplicate
  since both are binary AI recommendations with no third outcome)
- Frontend moderator dashboard integration — Open (no frontend repo in this workspace)

### T-008 — Organization and tenant management

- **Status:** In progress — Phase A done 2026-09-22: `organizations` app
  (Organization, OrganizationMembership, OrganizationInvitation,
  OrganizationAuditEvent), every existing user backfilled into a personal
  organization they own, new users get one automatically at registration,
  invitation lifecycle (invite/resend/cancel/accept, hashed tokens, role
  restricted to moderator/member), member role/status management with
  last-owner and moderator-cannot-manage-owner-or-moderator protection,
  immutable audit trail, 32 tests including cross-org isolation returning 404
  (not 403) for an invisible organization. Phase B done 2026-09-22, scoped down
  from the original plan: rather than an `organization` column on every
  owner-scoped model, only `UploadBatch` and `IdentityGroup` got one (the two
  models with no single parent to derive it from); every other content model
  will reach its organization transitively through its existing FK to one of
  those two when T-007's moderator queries need it, avoiding ~15 denormalized
  columns nothing queries directly. New batches/groups are tagged with the
  owner's personal organization at creation
  (`organizations.services.get_default_organization`); existing rows were
  backfilled. Owner-scoped access is unchanged everywhere -- org membership
  still does not grant access to a member's personal content, per the spec.
  T-007's moderator review workflow (Phase C, the first real consumer of
  these two fields) is done as of 2026-09-23 -- see T-007 below.
- Tenant membership and moderator assignment — Done (Phase A)
- Tenant-scoped queries, storage, reports, embeddings, and policies — Partial
  (Phase C's `ModeratorReview` is the first org-scoped query surface, covering
  quality/duplicate/identity review queues and a dashboard; reports, storage
  quotas, and embedding-level policies are still open)
- Automated cross-tenant isolation tests — Done for organization/membership APIs
  (Phase A) and moderator review APIs (Phase C, `moderation/tests.py`)

## 4. Platform and frontend tasks

| Task | Status |
|---|---|
| Connect completed dashboard APIs to frontend | Next |
| Connect reports, downloads and notifications | Next |
| Complete editor slider integration | Next |
| Admin analytics dashboard | Done (backend, 2026-09-22); frontend page remains |
| System health frontend page | Next |
| Model comparison page | Done (backend, 2026-09-19); frontend page remains |
| Secure expiring share links | Done (backend, 2026-09-19); frontend remains |
| Watermark options | Done (backend, 2026-09-19); frontend controls remain |
| Private cloud storage | Backlog |
| Live camera detection and enhancement | Backlog |

## 5. Complete remaining-feature register

This table includes every incomplete, planned, or integration-dependent catalogue
item. Detailed acceptance criteria remain in `FEATURES.md`.

| Feature | Remaining deliverable | Status | Task |
|---:|---|---|---|
| 10–13 | Enhancement controls, comparison and frontend wiring | Next | T-010 |
| 14–17 | Quality, face-validation and duplicate decision UI | Next | T-011 |
| 18 | SFace threshold calibration and production enablement | Blocked (dataset) | T-001 |
| 19 | Person Folders frontend and correction workflow | Blocked (no frontend repo) | T-002 |
| 20 | Recognition concurrency and dead-job/queue monitoring | Done (2026-09-22) | T-005 |
| 21 | Batch-progress frontend integration | Next | T-012 |
| 22 | JSON/CSV/PDF report frontend integration | Next | T-013 |
| 23 | Image and batch package frontend integration | Next | T-014 |
| 24 | Connect completed identity-group ZIP API to Person Folders | Next | T-015 |
| 25 | Export watermark options | Done backend (2026-09-19, `docs/features/25-watermark-options.md`); frontend controls remain | T-016 |
| 26 | Secure expiring share links | Done backend (2026-09-19, `docs/features/26-secure-share-links.md`); frontend remains | T-017 |
| 27 | Organization-scoped moderator review | Done backend (all phases A-G, 2026-09-23); frontend remains | T-007 |
| 28 | Complete owner activity timeline | Done backend (2026-09-18); frontend remains | T-018 |
| 29 | Notification panel and preferences UI | Next | T-019 |
| 30 | User dashboard and My Images integration | Next | T-020 |
| 31 | Moderator dashboard frontend | Backlog | T-021 |
| 32 | Admin analytics dashboard | Done backend (2026-09-22, `docs/features/32-admin-analytics.md`); frontend page remains | T-022 |
| 33 | Platform-wide administrative controls | Done backend (2026-09-22, `docs/features/33-administrative-management.md`); storage/threshold config and moderator monitoring deferred, frontend remains | T-023 |
| 34 | Connect completed system health API to an admin page | Next | T-024 |
| 35 | Uploaded-image model comparison page | Done backend (2026-09-19, `docs/features/35-model-comparison.md`); frontend page remains | T-025 |
| 36–40 | Complete live-camera release | Backlog | T-026–T-030 |
| 41 | Consent, retention, encryption, export and erasure | Backend done (2026-09-19, org-wide export added 2026-09-23), incl. AES-GCM key rotation, temp/orphan cleanup and beat scheduling; frontend remains | T-006 |
| 43 | Private cloud/object storage | Backlog | T-031 |
| 44 | OpenAPI schema for every APIView | Done (2026-09-19): zero schema warnings (enforced by a test), examples on the core flows, `docs/API_ERRORS.md` | T-032 |
| 45 | Load, performance, camera and integration QA | Continuous | T-033 |
| 46 | Organization and tenant management | In progress (Phases A+B done 2026-09-22, C done 2026-09-23) | T-008 |
| 47 | Gallery-editor frontend integration | Next | T-034 |
| 48 | Automatic event/timeline grouping | Backlog (no face data involved -- see `FEATURES.md`) | T-035 |
| 49 | AI-generated photo collages (face-aware smart crop) | Backlog (face-box crop needs no consent; person-specific collage reuses existing identity consent gate -- see `FEATURES.md`) | T-036 |
| 50 | Face-based visual search | Backlog (new biometric processing -- must reuse `biometric_processing_allowed`, query embedding never persisted -- see `FEATURES.md`) | T-037 |

### Register workstreams

- **T-010–T-011:** enhancement and decision user interfaces.
- **T-012–T-014:** progress, reports and package delivery.
- **T-015–T-017:** identity export, watermarks and secure sharing.
- **T-018–T-021:** activity, notifications, user and moderator portals.
- **T-022–T-025:** analytics, administration, health and model comparison.
- **T-026–T-030:** camera detection, enhancement, comparison, transport and privacy.
- **T-031:** private object storage, lifecycle, migration and deletion verification.
- **T-032:** OpenAPI schema, examples, error catalogue and deployment instructions.
- **T-033:** load, performance, security, restoration and end-to-end suites.
- **T-034:** editor sliders, previews, versions, undo and downloads.
- **T-035–T-037:** event/timeline grouping, AI collages, and face-based visual
  search -- three feature ideas surfaced by reviewing a separate, unrelated
  prototype project (`smart_gallery/`, not part of this repo's dependency or
  code base) for UX/feature concepts; none of its code or face-recognition
  stack (InsightFace/DBSCAN) was adopted, only the feature ideas, redesigned
  against this project's own identity data and consent rules.

## 6. Generative restoration research

### T-009 — Define generative restoration mode

- **Status:** Backlog
- Confirm business need, consent language, commercial licensing, cost, and GPU plan.
- Store outputs separately with `is_ai_generated` and `recognition_eligible=false`.
- Add scene-change, face-identity, artifact, and provenance checks.
- Never replace the conservative default mode.

## 7. Continuous verification

Run before each handoff:

```powershell
.\venv\Scripts\python.exe manage.py check
.\venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\venv\Scripts\python.exe manage.py test --keepdb
git diff --check
```

## 8. Recommended execution order

1. Feature 18 threshold calibration and privacy controls
2. Feature 19 Person Folders frontend integration
3. Recognition-job reliability and moderator review
4. GPU queue and Real-ESRGAN operational QA
5. Organization/tenant isolation
6. Remaining frontend dashboards and sharing features
7. Generative restoration research only after licensing and consent approval

Then execute the remaining register by dependency: export/download foundations,
moderation and tenancy, administration and health, private storage, and finally
the coordinated live-camera release.
