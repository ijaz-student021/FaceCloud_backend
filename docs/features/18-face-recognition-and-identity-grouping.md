# Feature 18: Face Recognition and Identity Grouping

## Status

Planned. This document defines the implementation contract. No identity model or
grouping API should be presented as complete until the privacy, model-license,
migration, API, Celery, and test gates below pass.

## Outcome

FaceCloud will extract a private embedding for every valid detected face and group
similar faces into owner-scoped Person Folders. An image containing 1–5 people can
therefore belong to multiple identity groups. Uncertain comparisons go to review;
they are never silently forced into a group.

This feature creates and reads identity groups. Renaming, merging, moving, profile
selection, and deletion are implemented separately in Feature 19.

## Non-negotiable rules

- Compare embeddings only against identities owned by the same user.
- Never return raw embeddings through an API, log, report, or download.
- Use only a model and weights whose commercial terms have been verified and saved.
- Do not download model code or weights dynamically in production.
- Treat recognition as probabilistic similarity, not proof of legal identity.
- Use neutral generated names such as `Person 001`; never infer a real name.
- Route low-confidence and ambiguous matches to review.
- Preserve 3–5-person group images and process each valid face independently.
- Do not allow moderators to see personal groups unless the owner explicitly shares
  a specific item for review.
- Deleting an account or image must remove its identity artifacts according to the
  retention policy.

## Phase 1: Model and privacy gate

Before writing inference code:

1. Select an embedding provider that supports local commercial deployment.
2. Record the model source, version, license, weight checksum, embedding dimension,
   input dimensions, preprocessing contract, and redistribution restrictions.
3. Decide whether buyers receive API access only or whether weights may be shipped.
4. Establish explicit consent, retention, export, and deletion behavior.
5. Prepare a representative evaluation set across lighting, skin tones, pose,
   camera quality, age ranges, and single/group photographs.
6. Choose thresholds from evaluation results instead of copying internet defaults.

The provider must be replaceable so a private model can be introduced later without
changing database or API contracts.

## Phase 2: Identity data model

Create a new `identities` Django app with the following models.

### IdentityAnalysis

One execution record per uploaded image:

- Owner and image
- Preprocessing and face-validation dependencies
- Pending, queued, processing, completed, needs-review, failed, and cancelled status
- Face count and assignment counters
- Model/provider and policy versions
- Celery task ID, safe failure reason, timestamps, and duration

### IdentityFace

One private biometric-processing record for each validated face:

- Owner, image, analysis, and face index
- Validated bounding box and optional alignment landmarks
- Encrypted or protected normalized embedding payload
- Embedding dimension, provider, model checksum, and quality metadata
- Unique constraint on analysis and face index

Embeddings must not be stored as ordinary public JSON. The initial implementation
may use a protected binary float vector for the current project scale. A production
scale deployment can use a reviewed PostgreSQL vector extension without changing
the service interface.

### IdentityGroup

Represents one Person Folder:

- Owner
- Neutral generated display name
- Active, merged, or archived state
- Optional profile membership
- Member count and timestamps
- Provider/policy compatibility metadata

### IdentityMembership

Connects one `IdentityFace` to one group:

- Owner, identity face, and identity group
- Similarity score and matching distance
- Auto-matched, new-group, needs-review, confirmed, rejected, or removed state
- Decision method, reviewer, review timestamp, and safe evidence
- Constraints preventing cross-owner or duplicate active assignments

An image can have several `IdentityFace` records and can therefore appear in several
Person Folders.

## Phase 3: Provider and face preparation

Implement an `IdentityEmbeddingProvider` interface:

- `healthcheck()` verifies model availability and checksum.
- `prepare_face()` clips, aligns, resizes, and normalizes a validated face crop.
- `embed()` returns a finite, L2-normalized vector with the expected dimension.
- `similarity()` returns a documented score using one consistent metric.

Use the standardized image and the stored face-validation boxes. Add landmark-based
alignment if required by the chosen provider. Reject invalid crops safely and never
reuse the perceptual duplicate hash as an identity embedding.

## Phase 4: Grouping policy

For every valid face:

1. Generate and validate the normalized embedding.
2. Load compatible candidate groups belonging only to the same owner.
3. Compare against a stable group representation, such as a protected centroid and
   selected confirmed member exemplars.
4. If the best score passes the calibrated high-confidence threshold and is clearly
   separated from the second-best score, assign the existing group.
5. If no group passes the new-group boundary, create a neutral new group.
6. If the score is borderline or two groups are too close, create a needs-review
   assignment without changing a confirmed group.
7. Update group counts and centroids only from confirmed memberships.

Required configuration includes provider name, model path/checksum, embedding
dimension, match threshold, new-group threshold, ambiguity margin, maximum candidate
groups, and automatic-analysis switch. Invalid thresholds must fail system checks.

## Phase 5: Idempotent Celery processing

Add a Celery task such as `identities.analyze_image`:

- Start after preprocessing and face validation have completed.
- Skip rejected or missing-face inputs with a clear safe outcome.
- Process every valid face in a 1–5-person image.
- Lock the owner and current analysis while committing assignments.
- Make reruns replace stale unconfirmed results without duplicating groups.
- Reject stale task IDs and respect batch cancellation.
- Use bounded retries only for transient failures.
- Never hold a database transaction during model inference.

Add an `identity_grouping` progress stage after face/quality validation and before
finalization where appropriate. Identity failure must not delete or corrupt a valid
enhancement result.

## Phase 6: Owner-scoped API

Provide JWT-protected endpoints suitable for the Person Folders page:

- `POST /api/identities/images/{image_id}/` queues or force-reruns analysis.
- `GET /api/identities/images/{image_id}/` returns the safe analysis summary.
- `GET /api/identities/groups/` lists the owner's groups with counts and previews.
- `GET /api/identities/groups/{group_id}/` returns one group and paginated images.
- `GET /api/identities/review/` lists uncertain assignments for the owner.
- `PATCH /api/identities/assignments/{assignment_id}/review/` confirms a suggested
  group, selects another owner-visible group, or creates a new group.

All querysets must be owner-scoped before object lookup so another user's identifier
returns HTTP 404. Responses expose scores and explanations but never embeddings,
centroids, internal storage paths, or model secrets.

Feature 19 will add rename, merge, move, remove, profile-image, and delete endpoints.

## Phase 7: Dashboard integration

- Replace the hard-coded Person 1/2/3 cards with `/api/identities/groups/`.
- Display real group count and real image totals on Overview.
- Show generated names, safe previews, update time, and review badges.
- Show one image in each applicable group when it contains multiple people.
- Add loading, empty, processing, review, failed, and unavailable-model states.
- Add identity-group filtering to My Images and the global search contract.

## Phase 8: Tests and acceptance criteria

### Automated tests

- Deterministic provider contract using a test provider, without production weights.
- One face creates one neutral identity group.
- Two similar faces for the same owner join one group.
- Dissimilar faces create different groups.
- A 3–5-person image produces one face record per valid face and is not rejected.
- Ambiguous matches enter review and do not update confirmed centroids.
- Reruns are idempotent and stale tasks cannot overwrite current results.
- Missing, corrupt, wrong-dimension, NaN, or unavailable model output fails safely.
- Account, image, and group cleanup follows cascade/retention policy.
- Raw embeddings never appear in serializers, logs, admin lists, or error messages.
- Cross-user analysis, candidates, groups, previews, downloads, and reviews return 404.
- Concurrent same-owner jobs do not create duplicate groups or memberships.
- My Images, Overview, and batch progress remain compatible.

### Model evaluation

- Report genuine-match and impostor-match score distributions.
- Measure false-match and false-non-match rates at proposed thresholds.
- Evaluate single and group images across the documented representative categories.
- Record uncertain-rate and manual-review workload.
- Do not mark the provider production-ready until thresholds and known limitations are
  documented.

### Completion gate

Feature 18 is complete only when:

1. Commercial model use is documented.
2. Migrations and Django system checks pass.
3. Celery registers and executes the identity task.
4. Owner-scoped APIs pass privacy tests.
5. Multi-face and ambiguity behavior pass.
6. The complete backend regression suite passes.
7. The feature catalogue and API documentation are updated.

## Implementation order

1. Approve model, license, privacy, and threshold strategy.
2. Create the `identities` models, constraints, indexes, admin, and migration.
3. Build the pluggable provider and face-alignment boundary.
4. Implement owner-scoped grouping and ambiguity services.
5. Add idempotent Celery execution and pipeline/progress integration.
6. Add owner-only group, analysis, and review APIs.
7. Integrate Overview and My Images summaries.
8. Add automated privacy, concurrency, multi-face, and failure tests.
9. Run migrations, system checks, focused tests, and the full backend suite.
10. Finalize documentation and provide the frontend API contract.
