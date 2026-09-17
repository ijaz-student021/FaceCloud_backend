# Feature 19: Identity Group Management

## Status

Complete backend. The dashboard frontend can consume the owner-scoped endpoints
below. Biometric inference remains governed separately by the Feature 18 model and
license gate.

## API contract

- `GET /api/identities/groups/` lists active Person Folders.
- `GET /api/identities/groups/{id}/` returns a folder and its face memberships.
- `PATCH /api/identities/groups/{id}/rename/` accepts `display_name`.
- `POST /api/identities/groups/{target_id}/merge/` accepts `source_group_id` and
  optional `reason`.
- `PATCH /api/identities/memberships/{id}/move/` accepts `target_group_id` and
  optional `reason`.
- `PATCH /api/identities/memberships/{id}/remove/` accepts optional `reason`.
- `PATCH /api/identities/groups/{id}/profile/` accepts `membership_id`.
- `DELETE /api/identities/groups/{id}/delete/` deletes a folder only after all
  confirmed memberships have been moved or removed.
- `GET /api/identities/history/` returns immutable change records.

Every object lookup starts from an owner-filtered queryset. Cross-owner IDs return
404 and provider/model compatibility is required for moves and merges. Operations
lock affected rows, recompute protected centroids and counts transactionally, clear
invalid profile selections, and record before/after evidence without embeddings.
