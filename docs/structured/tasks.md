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

- **Status:** In progress — backend done 2026-09-19 (`privacy` app, 71 tests,
  plus 17 cipher tests): consent records, retention purge, erasure with
  verification, export, audit report, deploy checks, AES-256-GCM embedding
  encryption with key ids and a re-encryption command, and stale-temp-file /
  orphaned-media cleanup; both periodic jobs are on the beat schedule since
  2026-09-19. Open: tenant-level export, frontend screens.
- Record consent purpose and timestamp. — Done
- Add configurable embedding retention and deletion. — Done
- Add account export and deletion verification. — Done (tenant export waits on T-008)
- Document encryption-at-rest requirements. — Done; see `features/41-privacy-and-facial-data.md`

### T-007 — Moderator workflow completion

- **Status:** Backlog
- Organization-scoped queues and decisions
- Corrections, reasons, and complete audit history
- Frontend moderator dashboard integration

### T-008 — Organization and tenant management

- **Status:** In progress — Phase A done 2026-09-22: `organizations` app
  (Organization, OrganizationMembership, OrganizationInvitation,
  OrganizationAuditEvent), every existing user backfilled into a personal
  organization they own, new users get one automatically at registration,
  invitation lifecycle (invite/resend/cancel/accept, hashed tokens, role
  restricted to moderator/member), member role/status management with
  last-owner and moderator-cannot-manage-owner-or-moderator protection,
  immutable audit trail, 32 tests including cross-org isolation returning 404
  (not 403) for an invisible organization. Open: Phase B (organization FK and
  queryset re-scoping across uploads/identities/duplicates/quality_control and
  the rest of the owner-scoped apps) and T-007's moderator review workflow,
  which depends on Phase B.
- Tenant membership and moderator assignment — Done (Phase A)
- Tenant-scoped queries, storage, reports, embeddings, and policies — Open (Phase B)
- Automated cross-tenant isolation tests — Done for organization/membership APIs
  (Phase A); content-level isolation tests are Phase B

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
| 27 | Organization-scoped moderator review | Backlog | T-007 |
| 28 | Complete owner activity timeline | Done backend (2026-09-18); frontend remains | T-018 |
| 29 | Notification panel and preferences UI | Next | T-019 |
| 30 | User dashboard and My Images integration | Next | T-020 |
| 31 | Moderator dashboard frontend | Backlog | T-021 |
| 32 | Admin analytics dashboard | Done backend (2026-09-22, `docs/features/32-admin-analytics.md`); frontend page remains | T-022 |
| 33 | Platform-wide administrative controls | Done backend (2026-09-22, `docs/features/33-administrative-management.md`); storage/threshold config and moderator monitoring deferred, frontend remains | T-023 |
| 34 | Connect completed system health API to an admin page | Next | T-024 |
| 35 | Uploaded-image model comparison page | Done backend (2026-09-19, `docs/features/35-model-comparison.md`); frontend page remains | T-025 |
| 36–40 | Complete live-camera release | Backlog | T-026–T-030 |
| 41 | Consent, retention, encryption, export and erasure | Backend done (2026-09-19), incl. AES-GCM key rotation, temp/orphan cleanup and beat scheduling; frontend remains | T-006 |
| 43 | Private cloud/object storage | Backlog | T-031 |
| 44 | OpenAPI schema for every APIView | Done (2026-09-19): zero schema warnings (enforced by a test), examples on the core flows, `docs/API_ERRORS.md` | T-032 |
| 45 | Load, performance, camera and integration QA | Continuous | T-033 |
| 46 | Organization and tenant management | In progress (Phase A done 2026-09-22) | T-008 |
| 47 | Gallery-editor frontend integration | Next | T-034 |

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
