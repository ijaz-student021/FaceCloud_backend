# Feature 15: Face Detection and Validation

## Status

Complete OpenCV baseline. A future learned detector may replace the baseline without changing the API contract.

## Workflow and decisions

Every successfully preprocessed image is automatically checked with the bundled OpenCV frontal-face detector. The original is never changed and results are stored idempotently in PostgreSQL.

- `accepted`: one suitable portrait face or a valid group containing up to five visible faces.
- `rejected`: a clear violation such as a single primary face that is too small.
- `needs_review`: no confident frontal face or a face near the boundary. This avoids unsafe rejection when a classical detector is uncertain.
- `failed`: validation could not complete safely.

Results contain face count, confidence, bounding boxes, area ratios, decision, machine-readable reason, explanation, detector version, and duration. Reasons include `no_face_detected`, `face_too_small`, `face_off_center`, `group_face_too_small`, `large_group_detected`, and `detector_failed`.

Group photographs containing 3–5 people are supported and are not rejected because of their face count. Very small group faces or more than five detected people are routed to review.

## API integration

- `POST /api/validation/images/{image_id}/` runs validation.
- `GET /api/validation/images/{image_id}/` retrieves it.
- My Images includes `face_validation` and accepts `accepted` and `needs_review` filters.
- Dashboard totals and activity use the persisted validation decisions.

Every query is owner-scoped. Another user's result returns HTTP 404.

## Accuracy and fairness

Validation uses facial geometry, not skin tone, ethnicity, gender, or identity. Low confidence routes to review rather than rejection. Before production, benchmark it across lighting, skin tones, camera quality, poses, age groups, and accessibility cases.

Automated tests cover portrait acceptance, 3–5-person group acceptance, review decisions, persistence, reruns, and cross-user privacy.
