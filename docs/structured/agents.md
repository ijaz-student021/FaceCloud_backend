# FaceCloud Engineering Agents and Responsibilities

## 1. Purpose

This document defines responsibility boundaries for people or automated coding
agents working on FaceCloud. Every contributor must preserve existing user work,
follow the product and architecture documents, and update tests and documentation
with implementation changes.

## 2. Shared rules

- Read [prd.md](prd.md), [architecture.md](architecture.md),
  [decisions.md](decisions.md), and [tasks.md](tasks.md) before broad changes.
- Treat [FEATURES.md](FEATURES.md) as the authoritative feature catalogue.
- Never commit `.env`, private keys, biometric exports, media, or model weights.
- Never overwrite original uploads during preprocessing or enhancement.
- Never use generatively restored images for recognition.
- Enforce ownership or tenant scope at queryset and service boundaries.
- Add migrations for schema changes; do not edit applied migrations casually.
- Add tests for success, permissions, invalid input, model failure, and fallback.
- Record material architectural choices in `decisions.md`.
- Update `tasks.md` when work begins, changes scope, or completes.
- Reference the catalogue feature number in branches, commits, tests and handoffs.
- Do not call a backend-only feature fully complete while required frontend or
  operational acceptance work remains.

## 3. Agent roles

### Product and requirements agent

- Maintains the PRD, feature catalogue, acceptance criteria, and release scope.
- Distinguishes complete backend work from frontend integration and operational QA.
- Does not mark model functionality complete without representative evaluation.

### API and domain agent

- Owns Django models, serializers, views, permissions, URLs, and migrations.
- Keeps domain logic in services rather than duplicating it across views.
- Preserves API compatibility or documents intentional versioned changes.

### Image and ML pipeline agent

- Owns preprocessing, enhancement, validation, recognition, and model adapters.
- Verifies exact checkpoint licenses, hashes, shapes, outputs, and performance.
- Adds safe fallbacks, pixel ceilings, tiling, provenance, and visual QA artifacts.
- Keeps recognition and generative restoration paths separate.

### Background-processing agent

- Owns Celery tasks, queues, retry policies, cancellation, idempotency, and progress.
- Prevents heavy inference from blocking web requests.
- Routes GPU workloads to isolated workers with explicit resource limits.

### Security and privacy agent

- Reviews authorization, tenant isolation, downloads, retention, consent, exports,
  audit records, and biometric-data handling.
- Blocks releases with cross-owner access or untracked generative provenance.

### Quality agent

- Maintains automated tests, sample datasets, performance baselines, and release QA.
- Compares visual outputs as well as numeric metrics.
- Treats corrupted, tiled, colour-shifted, or identity-changing output as failure.

### Documentation agent

- Keeps setup, configuration, model provenance, APIs, operations, and feature status
  synchronized with the repository.
- Uses precise status terms: planned, partial, backend complete, or complete.

## 4. Definition of done

A task is done when its acceptance criteria pass, relevant automated tests pass,
Django checks and migration checks pass, failure behavior is safe, documentation
is updated, and no unrelated user changes have been overwritten.

For partially complete catalogue features, the responsible agent must close the
corresponding entry in [tasks.md](tasks.md) and update [FEATURES.md](FEATURES.md).
