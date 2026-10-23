# Features 14–16: Image Quality Decision Workflow

## Status

Backend complete: persisted scoring, final decisions, background execution, APIs, dashboard/My Images integration, enhancement eligibility, tests, and configurable thresholds.

## Pipeline

```text
Upload → Preprocess → Face validation → Quality scoring → Final decision
                                                        ├─ accepted
                                                        ├─ rejected
                                                        └─ needs_review
```

The original upload is never overwritten. Quality work runs through Celery only after the surrounding PostgreSQL transaction commits.

## Group-image rule

Images containing 3–5 people are allowed and are not rejected merely because they contain multiple faces. A group image can be accepted when its faces are sufficiently visible and the image passes quality checks. More than five faces, uncertain detection, or very small group faces route to `needs_review` rather than automatic rejection.

## Scores

The service records normalized 0–100 brightness, sharpness, contrast, noise, exposure, resolution, and weighted overall scores. Raw measurements, detected defects, all decision reasons, policy version, task ID, and processing duration are retained for auditability.

Severe blur, extremely low resolution, extreme overexposure, a photograph with no usable signal (practically black, mean brightness under `QUALITY_REJECT_MIN_LUMINANCE`, about 6 of 255, or almost no contrast), or a rejected face-validation result can reject an image. **A dark photograph is not refused:** between that line and `QUALITY_REVIEW_MIN_LUMINANCE` (55) it carries the warning `underexposed` and goes on to the low-light enhancement. Contrast is measured in grey levels, so below the review brightness the contrast floor (`QUALITY_REJECT_CONTRAST`) is scaled down in proportion to the brightness; at ordinary brightness it is unchanged. A very dark photograph in which no face is found can still be refused for `severe_blur`, because darkness lowers the sharpness measure; that rule is unchanged. Borderline measurements and uncertain face validation route to review. Thresholds are environment-configurable through the `QUALITY_*` settings documented in `.env.example`.

## APIs

- `POST /api/quality/images/{image_id}/` queues analysis or reanalysis.
- `GET /api/quality/images/{image_id}/` returns the owner's stored result.
- `GET /api/quality/results/?decision=accepted|rejected|needs_review` lists owner results.
- My Images includes `quality_decision` and supports final-decision and quality-processing filters.
- Dashboard cards, seven-day decisions, average score, common rejection reason, and activity use persisted results.

Enhancement is blocked when a completed quality result is rejected or needs review. Existing legacy images without a quality result remain compatible and can be analyzed through the API.

## Privacy and fairness

All queries are owner-scoped. The workflow does not classify ethnicity, skin tone, gender, or identity. Uncertainty routes to review rather than an unsafe permanent rejection. Production thresholds should be benchmarked across diverse people, lighting, cameras, poses, group sizes, and accessibility cases.

## Verification

Tests cover good images, valid groups, severe blur, low resolution, borderline quality, uncertain faces, normalized scoring, persistence, idempotency, Celery queuing, API privacy, dashboard integration, and existing-feature regression.
