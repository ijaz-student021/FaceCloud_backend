# FaceCloud Architecture Decision Log

This is a concise decision log. New decisions should include date, status,
context, decision, and consequences. Superseded decisions remain for history.

## ADR-001 — Preserve immutable originals

- **Status:** Accepted
- **Decision:** Every preprocessing, enhancement, edit, and restoration result is
  stored as a derivative. Original uploads are never overwritten.
- **Reason:** Recovery, auditability, recognition integrity, and user trust.

## ADR-002 — Use PostgreSQL as the authoritative data platform

- **Status:** Accepted
- **Decision:** PostgreSQL stores accounts, processing state, decisions, groups,
  memberships, reports, notifications, and audit metadata.
- **Consequence:** SQLite is not the production target; migrations and constraints
  must be PostgreSQL-compatible.

## ADR-003 — Use Redis and Celery for background processing

- **Status:** Accepted
- **Decision:** Preprocessing and inference execute outside web requests.
- **Consequence:** Tasks require idempotency, retry limits, progress reporting, and
  queue-unavailable failure handling.

## ADR-004 — Separate storage bytes from database metadata

- **Status:** Accepted
- **Decision:** Django storage holds originals and derivatives; PostgreSQL holds
  ownership, paths, hashes, and state.
- **Consequence:** Downloads must pass application authorization and deletion must
  clean both records and stored objects.

## ADR-005 — Conservative enhancement is the default

- **Status:** Accepted
- **Decision:** Default enhancement is bounded and identity-preserving. Models are
  optional stages protected by measurable output guards.
- **Consequence:** Dramatic generative reconstruction requires a separately labelled
  mode and must not silently replace natural enhancement.

## ADR-006 — YuNet and SFace provide the recognition baseline

- **Status:** Accepted
- **Decision:** YuNet detects faces and landmarks; SFace produces embeddings.
- **Consequence:** Matching is owner/tenant-scoped and thresholds require evaluation
  on a consented dataset before production automation.

## ADR-007 — Recognition never consumes generative restoration

- **Status:** Accepted
- **Decision:** Embeddings are generated only from originals or conservative
  standardized derivatives.
- **Consequence:** AI-restored outputs are display/download artifacts with explicit
  provenance and `recognition_eligible=false` semantics.

## ADR-008 — Model artifacts are local, private, and checksum-pinned

- **Status:** Accepted
- **Decision:** Workers never download models during inference. Exact model files
  are configured by path and SHA-256 and remain outside Git.
- **Consequence:** Deployment must provision artifacts and license evidence before
  enabling a model.

## ADR-009 — Real-ESRGAN HD restoration is rollout-gated

- **Status:** Accepted
- **Decision:** Real-ESRGAN is implemented behind `HD_RESTORATION_ENABLED` with
  tiling, output limits, and conservative source-grid blending.
- **Consequence:** It remains disabled by default and should run on a GPU queue.

## ADR-010 — NAFNet remains disabled after failed visual QA

- **Status:** Accepted
- **Decision:** `NAFNET_DEBLUR_ENABLED` defaults to false because the supplied
  checkpoint/runtime produced checkerboard and chroma corruption.
- **Consequence:** Structural checkpoint loading is insufficient; a replacement or
  official compatible runtime must pass sample and identity-preservation QA.

## ADR-011 — Generative restoration is a separate future product mode

- **Status:** Proposed
- **Decision:** Whole-scene diffusion restoration, if approved, will use an isolated
  GPU workflow, explicit consent and labelling, separate derivatives, and no
  recognition eligibility.
- **Consequence:** Model and checkpoint commercial licenses must be reviewed before
  implementation or deployment.

## ADR-012 — Track remaining work by catalogue feature number

- **Status:** Accepted
- **Decision:** The 47-feature catalogue is authoritative. Requirements and tasks
  reference its numbers and distinguish backend, frontend, model-QA and operational
  completion.
- **Consequence:** A feature is not complete merely because an endpoint or adapter
  exists; every stated acceptance layer must be reconciled first.
