# Feature 32: Admin analytics

## Status

Complete for the backend (`operations/analytics.py`). No frontend page exists
yet; this is the data the dashboard page (Feature 32's remaining scope in the
task register) would call.

## What it does

`GET /api/system/analytics/` (administrators only) returns platform-wide
totals, a success rate, the most common rejection reasons, average processing
time, per-model usage, storage in bytes, and per-day upload/enhancement
charts — all aggregated live from data the platform already stores. Nothing
new is measured or recorded; this is read-only reporting.

## Filters

| Query param | Meaning |
|---|---|
| `date_from`, `date_to` | ISO-8601. Default: the 30 days up to now. Maximum span: 366 days. |
| `status` | Restrict enhancement totals to one `EnhancementStatus`. |
| `model` | Restrict enhancement totals to results whose algorithm name contains this substring. |
| `owner` | Restrict every total to one user's id. |

A bare date for `date_to` (e.g. `2026-09-22`, no time component) means
**through the end of that day**, not its midnight — otherwise activity
recorded later that same day would silently fall outside the window. A date
already carrying a time is used exactly as given.

## Response shape

```json
{
  "range": {"from": "...", "to": "..."},
  "filters_applied": {"status": null, "model": null, "owner": null},
  "totals": {
    "users": {"total": 6, "verified": 4, "suspended": 0},
    "batches": 17,
    "images": {"total": 23, "accepted": 23, "rejected": 0},
    "enhancements": {"total": 8, "by_status": {"completed": 8}},
    "quality_decisions": {"total": 7, "by_decision": {"accepted": 1, "rejected": 5}}
  },
  "success_rate_percent": 16.67,
  "average_processing_time_ms": {"enhancement": 27877.0, "quality_analysis": 100.5},
  "top_rejection_reasons": [{"value": "severe_underexposure", "count": 2}, ...],
  "model_usage": [{"value": "zero_dce_hybrid_v1", "count": 3}, ...],
  "storage_bytes": {"originals": 11016397, "enhanced_outputs": 1111560, "total": 12127957},
  "charts": {"uploads_per_day": [{"date": "2026-01-01", "count": 0}, ...], "enhancements_per_day": [...]},
  "generated_at": "..."
}
```

Notes on specific fields:

- **`success_rate_percent`** is accepted / (accepted + rejected + needs_review)
  quality decisions in the window, as a percentage; `null` when nothing was
  decided yet, never a divide-by-zero.
- **`top_rejection_reasons`** counts entries from
  `ImageQualityDecision.rejection_reasons` on every non-accepted decision (top
  10). `ImageUpload.rejection_reason` (free text from upload-time validation)
  is not included; in practice it is empty on accepted platform data and
  structured `rejection_reasons` is the meaningful source.
- **`model_usage`** splits a compound algorithm string such as
  `"zero_dce_hybrid_v1+private_gfpgan_v1_4"` on `+` and counts each named
  model separately (top 10), so a photo that used three models contributes to
  all three counts rather than one combined label nobody else shares.
- **`charts.*`** are zero-filled for every day in the window, so a frontend
  chart does not have to backfill missing days itself.

## Verification

`operations/test_analytics.py` (21 tests): window defaulting/validation
(explicit range, from-after-to, over-length, malformed dates, the
date-only-`date_to` inclusivity fix caught by a failing test before it was
fixed); window/owner/status/model filtering; success rate with and without
decided images; rejection-reason counting excludes accepted decisions;
compound-algorithm splitting; exact storage and average-duration arithmetic;
zero-filled charts; user totals are platform-wide, never windowed or
owner-filtered; the endpoint's authentication, admin-only permission and
400-on-bad-filter behaviour. Also manually verified against real dev data.

## Deliberately not built

- **Live sessions.** Nothing in the platform tracks active sessions today (no
  server-side session store beyond JWTs); adding one is out of scope for this
  feature.
- **Latency charts** beyond average processing time: per-day/percentile
  latency series would need a dedicated metrics pipeline.
- **Role and moderator filters**: `owner` covers "one user"; a role-wide or
  moderator-wide filter would need to join through the roles of many users
  per row, which is a heavier query than this endpoint is meant for. `owner`
  plus a client-side loop over an admin's user list covers the same need.
