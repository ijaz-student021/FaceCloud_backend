# Feature 15: Face Detection and Validation

## Status

Complete OpenCV baseline with a feature-flagged full-resolution per-face metrics
contract. A future commercially reviewed landmark detector can replace the
baseline through the detector adapter without changing the API contract.

## Workflow and decisions

Every successfully preprocessed image is automatically checked with the bundled
OpenCV frontal-face detector. The original is never changed and results are
stored idempotently in PostgreSQL.

- `accepted`: one suitable portrait face or a valid group containing up to five visible faces.
- `rejected`: a clear violation such as a single primary face that is too small.
- `needs_review`: no suitable frontal face, or a face cut off by or touching an edge of the picture (within 1% of it). Where the face sits otherwise does not matter:
  a head-and-shoulders portrait, with the face in the upper part, is accepted.
- `failed`: validation could not complete safely.

Results contain face count, detector score and its confidence type, bounding
boxes, area ratios, decision, machine-readable reason, explanation, detector
version, coordinate provenance, and duration. Reasons include
`no_face_detected`, `face_too_small`, `face_off_center`,
`group_face_too_small`, `large_group_detected`, and `detector_failed`.

Group photographs containing 3–5 people are supported and are not rejected
because of their face count. Very small group faces or more than five detected
people are routed to review.

## API integration

- `POST /api/validation/images/{image_id}/` runs validation.
- `GET /api/validation/images/{image_id}/` retrieves it.
- My Images includes `face_validation` and accepts `accepted` and `needs_review` filters.
- Dashboard totals and activity use persisted validation decisions.

Every query is owner-scoped. Another user's result returns HTTP 404.

## Full-resolution per-face metrics

`PER_FACE_METRICS_ENABLED` controls the new metrics contract and defaults to
`False` for a controlled rollout. When enabled, detection still runs on the
standardized image, but every box is mapped onto the EXIF-oriented,
full-resolution original. The result records the source and target dimensions,
independent X/Y scale factors, mapping method, and both standardized and
full-resolution boxes.

Each successfully mapped full-resolution face crop records:

- Laplacian-variance blur score
- Mean luminance
- Highlight-clipping percentage
- Crop width and height
- Whether the face touches an image boundary
- Full-resolution landmarks when supplied by the detector
- Landmark confidence when supplied by the detector
- An explicit pose/occlusion availability state

If the original cannot be decoded, validation continues safely and stores an
auditable `unavailable` metrics state. It does not fabricate measurements.

## Detector confidence limitation

The bundled Haar detector does not provide calibrated probability. Its
`detectMultiScale3` level weight is bounded into a legacy internal score and is
labelled `internal_heuristic_not_probability`. It must not be displayed as a
percentage or used as a probability. GFPGAN gating remains disabled until a
commercially reviewed landmark detector and empirically validated score
threshold are available.

## Future detector adapter

The validation service consumes detector-neutral boxes, scores, landmarks,
landmark scores, detector version, and confidence provenance. The current Haar
adapter supplies no landmarks, so pose and occlusion are recorded as unavailable
rather than estimated from an invented heuristic.

`IDENTITY_EMBEDDING_ENABLED`, `GFPGAN_ENABLED`, and `GFPGAN_QUEUE_ENABLED`
remain disabled. No embedding or GFPGAN dependency is introduced by this
checkpoint.

## Accuracy, fairness, and verification

Validation uses facial geometry, not skin tone, ethnicity, gender, or identity.
Before production, benchmark it across lighting, skin tones, camera quality,
poses, age groups, and accessibility cases using consented data.

Automated tests cover portrait acceptance, 3–5-person group acceptance, review
decisions, backward-compatible disabled flags, coordinate mapping,
full-resolution crop measurements, landmark mapping, boundary detection,
unavailable-source fallback, persistence, and cross-user privacy.
