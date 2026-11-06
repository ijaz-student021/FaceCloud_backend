# Feature 17: Duplicate Image and Face-Region Detection

## Status

Complete backend baseline. Exact files, visually modified copies, and repeated
validated face regions can be detected for the same authenticated owner. Learned
identity embeddings and identity-group merging remain Features 18 and 19.

## Purpose and safety policy

The feature warns a user when an uploaded image appears to repeat content already
stored in that user's account. It does not automatically reject, delete, merge,
or expose an image.

- Comparisons are restricted to images owned by the same authenticated user.
- Cross-user hashes, matches, filenames, and images are never queried or returned.
- Exact SHA-256 matches are confirmed automatically because the file bytes match.
- Visual and face-region matches are marked `suspected` and require review.
- Face-region similarity is not treated as proof of a person's identity.
- Internal full-image and face-region fingerprints are not serialized by the API.
- Processing failures use a generic public explanation and retain no exception data.

## Processing workflow

1. An upload completes preprocessing and face validation.
2. FaceCloud creates or resets one idempotent duplicate-analysis record.
3. Celery runs `duplicates.analyze_image` outside the web request.
4. SHA-256 metadata identifies byte-for-byte duplicates.
5. A 64-bit DCT perceptual hash identifies visually similar full images.
6. The configured face-fingerprint provider hashes validated face crops.
7. Candidate queries are owner-scoped and bounded by a configurable maximum.
8. Matches and the final decision are committed transactionally.
9. My Images exposes only a compact status, decision, and match-count summary.

The decision values are:

- `no_duplicate`: no active same-owner match was found.
- `duplicate`: at least one confirmed match exists.
- `needs_review`: at least one suspected visual or face-region match exists.

Analysis can also be `pending`, `queued`, `processing`, `completed`, `failed`, or
`cancelled`. A cancelled upload batch stops duplicate processing safely.

## Match evidence

Three match types are stored:

- `exact`: the stored SHA-256 checksums are identical; similarity is 100%.
- `perceptual`: full-image perceptual hashes are within the configured distance.
- `face_region`: at least one validated face crop is within the configured distance.

Exact matches cannot be dismissed through the review API. Suspected matches can be
confirmed or marked `not_duplicate`. Re-reviewing refreshes the summary for both
images. Duplicate analysis is independent of the enhancement-quality decision, so
a warning does not silently block a valid enhancement.

## API contract

All endpoints require JWT authentication and return only resources owned by the
authenticated user. Access to another user's resource returns HTTP 404.

- `POST /api/duplicates/images/{image_id}/` queues analysis. Send optional
  `{ "force": true }` to check the image again; a pair that is found again keeps
  its match id and its review decision (see "Data model and consistency").
- `GET /api/duplicates/images/{image_id}/` returns one analysis summary.
- `GET /api/duplicates/images/{image_id}/matches/` lists matches involving an image.
- `GET /api/duplicates/results/` lists analyses; optional `status` and `decision`
  filters are validated.
- `GET /api/duplicates/matches/` lists every match of the owner, newest first and
  paged, dismissed ones included; optional `review_status`, `match_type` and `image`
  (either side of the pair) filters are validated. Matches with an image in the
  trash are left out, as in the detail and review calls.
- `GET /api/duplicates/matches/{match_id}/` returns one owner-visible match.
- `PATCH /api/duplicates/matches/{match_id}/review/` accepts `confirmed` or
  `not_duplicate` for reviewable matches.

The existing My Images response includes `duplicate_analysis` with the public
status, decision, match count, timing, and matches URL. Raw fingerprints and task
identifiers remain server-side.

## Data model and consistency

`DuplicateAnalysis` is a one-to-one result for an uploaded image. It records the
owner, preprocessing dependency, execution state, final decision, bounded evidence,
policy version, timing, and internal fingerprints.

`DuplicateMatch` stores a directed image pair and match type, similarity values,
review state, reviewer, and safe structured evidence. Database constraints prevent
self-matches and duplicate records for the same directed pair and type. Processing
locks the owner and analysis rows so concurrent same-owner jobs cannot make
inconsistent match decisions.

A new check reconciles the stored pairs of an image with what it finds; it does not
delete and rebuild them. A pair is the same pair whichever image is stored as its
source, and each match type keeps its own row. A pair found again keeps its row (id,
`review_status`, `reviewed_by`, `reviewed_at`, `created_at`) and only its measured
values are refreshed. A pair is deleted only when the check looked for it and no
longer finds it: the other image must have been one of the candidates compared in
this run, so a pair is kept while the other image's own check is not finished or
when the other image is beyond `DUPLICATE_MAX_CANDIDATES`, and a pair with an image
in the trash is never deleted. The summaries of every image whose pair was deleted
are recounted. An exact match stays confirmed.

## Configuration

The following environment-backed settings are available:

- `DUPLICATE_AUTO_ANALYZE` enables automatic pipeline execution.
- `DUPLICATE_PERCEPTUAL_HASH_DISTANCE` controls full-image sensitivity.
- `DUPLICATE_FACE_HASH_DISTANCE` controls face-region sensitivity.
- `DUPLICATE_MAX_CANDIDATES` bounds the number of older analyses inspected.

Thresholds must be calibrated with representative, licensed evaluation images
before production. Larger hash-distance thresholds increase recall and false
positive risk.

## Verification

Automated tests cover deterministic hashing, exact duplicates, visually modified
copies, repeated face regions, owner isolation, safe corrupt-image failures,
asynchronous queuing, API fingerprint privacy, review decisions, idempotent reruns,
and prevention of dismissing exact matches.

Operational setup requires the database migration, Redis, and a restarted Celery
worker so the new `duplicates.analyze_image` task is registered.
