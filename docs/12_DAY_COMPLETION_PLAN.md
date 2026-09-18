# FaceCloud 12-Day Completion Plan

## 1. Release objective

Deliver a defensible FaceCloud MVP in 12 days with a stable demonstration path:

```text
Authenticate
  -> upload one/many/ZIP images
  -> preprocess and validate faces
  -> enhance conservatively
  -> detect duplicates
  -> recognize and group identities
  -> correct Person Folders
  -> track progress
  -> generate reports/downloads
  -> verify privacy, permissions and failure handling
```

The release goal is not to claim that all 47 catalogue features are complete. It
is to finish the highest-value end-to-end workflow and present accurate evidence.

## 2. Scope commitment

### Must finish

1. Feature 18 acceptance tests, threshold evidence and production gating
2. Feature 19 API endpoint tests and Person Folders integration
3. Feature 20 identity-job reliability, cancellation and stale-task protection
4. Features 21-23 progress, reports and downloads integration
5. Feature 29 notification integration
6. Feature 30 real user dashboard integration
7. Feature 41 minimum consent, retention and deletion controls
8. Feature 44 accurate API and deployment documentation
9. Feature 45 release regression, security and performance verification
10. Feature 47 gallery-editor frontend integration if the frontend repository is available

### Finish only if must-finish scope is green

- Feature 24 identity-group ZIP export
- Feature 28 activity timeline expansion
- Feature 34 internal system-health endpoint/page

### Explicitly defer

- Feature 25 watermark customization
- Feature 26 public share links
- Features 27 and 31 full organization moderator system
- Features 32-33 advanced analytics and platform configuration
- Feature 35 model comparison page
- Features 36-40 live-camera release
- Feature 43 production cloud-storage migration
- Feature 46 complete multi-tenant organizations
- Generative whole-scene restoration
- NAFNet enablement until output passes visual QA

Deferred features remain documented as planned and must not appear as complete in
the presentation or report.

## 3. Daily execution plan

### Day 1 - Freeze scope and establish the release baseline

Deliverables:

- Create a release branch and preserve the current working tree.
- Run Django checks, migration checks and the full automated suite.
- Record failed tests, test duration and current environment configuration.
- Reconcile `FEATURES.md`, `features_working.md` and this plan.
- Freeze API/schema changes except those required by must-finish scope.
- Create one release checklist with an owner and state for every task.

Exit criteria:

- Baseline results are saved.
- No unknown migration drift.
- Every remaining 12-day task is classified as must, optional or deferred.

### Day 2 - Feature 18 deterministic grouping tests

**Mostly done 2026-09-18.** `identities/tests.py`: `ChooseGroupDecisionTests`
(new/match/review/owner-isolation) and `RefreshGroupCentroidTests` (centroid
refresh, empty-group clearing). `ProcessIdentityAnalysisCancellationTests`
covers mid-loop cancellation between faces. Still open: a dedicated stale
identity-task-ID test (the guard itself exists at `identities/tasks.py:21`,
but no test exercises it directly).

Deliverables:

- Unit tests for `_choose_group`: new group, confident match and ambiguity.
- Owner-isolation tests for candidate selection.
- Centroid refresh and empty-group behavior tests.
- Stale identity-task ID and cancellation tests.

Exit criteria:

- Grouping decision boundaries are covered without relying on camera images.
- Tests fail if cross-owner candidates are considered.

### Day 3 - Feature 18 real-image acceptance and calibration

Deliverables:

- A small consented evaluation set with same-person and different-person pairs.
- End-to-end YuNet -> alignment -> SFace -> grouping tests.
- Threshold report containing false matches, false new groups and review outcomes.
- Production thresholds stored through configuration, not hard-coded in views.

Exit criteria:

- Same-person examples group together at the selected threshold.
- Different-person examples do not auto-group.
- Ambiguous examples enter review.
- Dataset consent and model-license records are available.

If a consented dataset is unavailable, use synthetic deterministic service tests,
mark real-world calibration blocked and do not claim production accuracy.

### Day 4 - Feature 19 endpoint acceptance

**Done 2026-09-18** except OpenAPI request/response examples (tracked under
Feature 44 instead). `identities/tests.py`: `IdentityGroupManagementApiTests`
covers rename, merge, move, remove, set-profile and delete via authenticated
HTTP requests, each with a success case and a cross-owner-404 case, plus
invalid-payload and non-empty-delete edge cases (14 tests). List/detail
endpoint tests already existed in `IdentityPrivacyApiTests`. Change-history
assertions are covered via the rename test's `IdentityChange` check.

Deliverables:

- Authenticated API tests for list, detail, rename, merge, move, remove, profile and
  delete operations.
- Invalid-payload, nonempty-delete and cross-owner tests for every write endpoint.
- Assertions for change-history records.
- OpenAPI request/response examples.

Exit criteria:

- Every documented management endpoint has success and permission tests.
- No private embedding or centroid is returned by serializers.

### Day 5 - Person Folders and dashboard integration

Deliverables:

- Replace Person Folders demonstration data with identity APIs.
- Implement list, detail, rename, merge, move, remove and profile actions.
- Connect dashboard totals and My Images to real APIs.
- Add loading, empty, failure, expired-token and retry states.

Exit criteria:

- A user can upload photos, see groups and correct them through the UI.
- Another user cannot view or modify those groups.

Dependency: if the frontend repository is not available, deliver API contracts,
Postman/cURL demonstrations and mark frontend work externally blocked.

### Day 6 - Background-processing hardening

Deliverables:

- Bounded retry policy for transient queue/database/storage failures.
- No retries for validation or permanent model errors.
- Cooperative identity cancellation between face operations.
- Task-version/idempotency tests preventing stale overwrites.
- Document CPU and optional GPU queue routing and concurrency.

Exit criteria:

- One failed image does not fail its entire batch.
- Cancelled work stops safely and preserves completed results.
- Duplicate/stale tasks cannot replace newer records.

### Day 7 - Progress, reports, downloads and notifications

Deliverables:

- Connect Feature 21 progress polling to real stage states.
- Connect Feature 22 JSON/CSV/PDF generation and queued-state handling.
- Connect Feature 23 package creation, expiry, history and regeneration.
- Connect Feature 29 unread count, list, mark-read and preferences.

Exit criteria:

- The complete upload-to-download demonstration works without manual database edits.
- Queue failure produces a safe user-facing error and retry option.

### Day 8 - Minimum biometric privacy controls

Deliverables:

- Explicit recognition-consent record with purpose, version and timestamp.
- Identity processing blocked without valid consent.
- Configurable embedding retention period.
- Deletion service and tests for faces, memberships, groups and protected vectors.
- User-facing explanation that recognition uses original/conservative images.

Exit criteria:

- Consent, withdrawal and deletion paths are tested.
- No raw embedding appears in APIs, logs or reports.
- Cross-owner and cross-tenant assumptions are documented accurately.

### Day 9 - License and security closure

Deliverables:

- Model register containing filename, SHA-256, source, license and approval state.
- Evidence for YuNet, SFace, FaceCloud Curve, EDSR, GFPGAN and Real-ESRGAN.
- Disable any checkpoint without adequate permission or provenance.
- Generate third-party notices for distributable dependencies.
- Run ownership, upload, archive and download security tests.

Exit criteria:

- Every enabled model has a recorded license decision.
- SFace stays gated if commercial checkpoint permission is unresolved.
- Secrets, weights, user media and private licenses are excluded from Git.

### Day 10 - Full regression and performance day

Deliverables:

- Run the complete test suite from a clean test database.
- Run migration and Django deployment checks.
- Test representative single, multiple and ZIP batches.
- Record processing time, peak memory and failure rate.
- Verify Redis restart, worker interruption and queue-unavailable behavior.
- Perform visual QA on enhancement samples.

Exit criteria:

- No release-blocking test failures.
- No corrupt enhancement output.
- Known performance limits are documented.

### Day 11 - Documentation and demonstration rehearsal

Deliverables:

- Synchronize PRD, architecture, decisions, tasks, feature catalogue and audit report.
- Generate accurate API schema and setup instructions.
- Prepare a deterministic demo dataset and step-by-step demonstration script.
- Prepare architecture, privacy, model and limitation explanations.
- Run the full demonstration twice from a clean state.

Exit criteria:

- Documentation status matches executable evidence.
- Demonstration completes without hidden manual repair.
- Planned features are clearly distinguished from implemented features.

### Day 12 - Release candidate and contingency

Deliverables:

- Fix release-blocking defects only; do not add scope.
- Run final smoke, permission and migration checks.
- Create a tagged release/commit and archive the evidence report.
- Back up configuration templates, schema, documentation and demo instructions.
- Use remaining time for one optional item only if all must-finish gates pass.

Exit criteria:

- Release checklist is signed off.
- Repository is reproducible from documented setup instructions.
- Final feature claims are evidence-based.

## 4. Daily operating rhythm

Use this routine every day:

1. 15 minutes: review blockers and select the day's acceptance criteria.
2. Implement the smallest end-to-end vertical slice.
3. Run focused tests after every material change.
4. Run affected module tests before handoff.
5. Update `tasks.md` and evidence documentation.
6. Stop feature work one hour before the day ends and stabilize.

Do not begin a new feature while the current day's release-blocking tests are red.

## 5. Quality gates

### Required on every change

```powershell
.\venv\Scripts\python.exe manage.py check
.\venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\venv\Scripts\python.exe manage.py test <affected modules> --keepdb
git diff --check
```

### Required before release

```powershell
.\venv\Scripts\python.exe manage.py test --noinput
.\venv\Scripts\python.exe manage.py check --deploy
.\venv\Scripts\python.exe manage.py makemigrations --check --dry-run
```

Also verify:

- Redis responds and all expected Celery workers are online.
- Models match configured SHA-256 values.
- Originals remain unchanged after every workflow.
- All protected endpoints reject another user's identifiers.
- Reports and logs contain no raw embeddings or secrets.

## 6. Release-blocking severity

### P0 - Stop release

- Cross-owner or cross-tenant data access
- False automatic grouping of clearly different people in acceptance data
- Raw embedding or secret disclosure
- Original-file overwrite or irreversible deletion error
- Corrupted model output accepted as successful
- Migration failure or unreproducible setup

### P1 - Fix before release

- Broken primary upload-to-download journey
- Stuck batch state or cancellation failure
- Missing Feature 18/19 endpoint tests
- Report/package generation failures
- Inaccurate feature or license claims

### P2 - Document and defer if necessary

- Styling defects
- Optional email delivery issues
- Advanced analytics
- HD restoration performance on CPU
- Optional planned features outside the MVP cut

## 7. Progress metrics

Track these each evening:

| Metric | Release target |
|---|---:|
| Full automated suite | 100% passing |
| Feature 19 management endpoints with API tests | 100% |
| Feature 18 decision branches covered | 100% |
| Cross-owner protected endpoints tested | 100% of release scope |
| Enabled models with checksum and license evidence | 100% |
| P0 defects | 0 |
| P1 defects | 0 at release |
| Demonstration runs completed from clean state | At least 2 |

## 8. Final deliverable definition

At the end of Day 12, FaceCloud should be presented as a secure backend-complete
MVP with a working user journey, not as a fully delivered 47-feature commercial
platform. Deferred features remain a documented roadmap. This narrower claim is
more credible, testable and professionally defensible.
