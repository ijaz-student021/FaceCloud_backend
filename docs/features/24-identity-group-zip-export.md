# Feature 24 - Identity-Group ZIP Export

## Status

Backend complete. Person Folders frontend integration remains.

## Purpose

Feature 24 lets an authenticated user export images from one of their own identity
groups. A user cannot discover, queue, inspect, regenerate, delete, or download
another user's group export.

## Security model

Ownership is enforced independently at every layer:

- Group API lookups filter by `owner=request.user` and return HTTP 404 for foreign IDs.
- Export API lookups filter by `owner=request.user`.
- Export creation requires `group.owner_id == request.user.id`.
- Archive queries require the same owner on the export, group, membership, face,
  and source image.
- Packages are stored under randomized `users/{owner_id}/identity-exports/` paths.
- Downloads are streamed only after ownership, status, expiry, and file-existence checks.
- ZIP names are sanitized and cannot contain absolute paths or `..` traversal.
- Manifests and reports never contain embeddings, centroids, credentials, model
  metadata, or physical storage paths.
- Expired and deleted export records remove their stored ZIP files.

## API

All endpoints require JWT authentication.

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/identities/groups/{group_id}/exports/` | Queue an export |
| `GET` | `/api/identities/groups/{group_id}/exports/` | List the user's exports for the group |
| `GET` | `/api/identities/exports/{export_id}/` | Poll export status |
| `GET` | `/api/identities/exports/{export_id}/download/` | Stream a ready ZIP |
| `POST` | `/api/identities/exports/{export_id}/regenerate/` | Regenerate a failed or expired export |
| `DELETE` | `/api/identities/exports/{export_id}/` | Delete an export and its ZIP |

Request example:

```json
{
  "include_originals": true,
  "include_preprocessed": false,
  "include_enhanced": true,
  "include_report": true,
  "report_format": "json"
}
```

At least one include option must be true. Report formats are `json` and `csv`.

## Processing workflow

1. Validate ownership, active group state, members, and request options.
2. Reuse an equivalent queued/generating export or create a new queued record.
3. Dispatch `identities.generate_group_export` to Celery after transaction commit.
4. Load only active memberships and same-owner source images.
5. Deduplicate images when several faces from one image belong to the group.
6. Add requested, available variants and record unavailable optional files as skipped.
7. Add a non-sensitive report and `manifest.json`.
8. Enforce configured file-count and uncompressed-size limits.
9. Store the ZIP, SHA-256 checksum, size, expiry, and safe display name.
10. Notify the owner and record successful downloads in download history.

Exports use the existing `DOWNLOAD_PACKAGE_TTL_HOURS` setting, which defaults to
24 hours. Cleanup is available through the `identities.cleanup_expired_exports`
Celery task.

## Response states

- `queued`: accepted for background generation.
- `generating`: Celery is building the ZIP.
- `ready`: available for secure download.
- `failed`: generation failed with a sanitized public message.
- `expired`: ZIP was removed after its retention window.

## Manual commands

```powershell
$GroupId = YOUR_OWN_GROUP_ID
$ExportBody = @{
    include_originals = $true
    include_preprocessed = $false
    include_enhanced = $true
    include_report = $true
    report_format = "json"
} | ConvertTo-Json

$Queued = Invoke-RestMethod -Method Post -Uri "$Api/api/identities/groups/$GroupId/exports/" -Headers $Headers -ContentType "application/json" -Body $ExportBody -ErrorAction Stop
$ExportId = $Queued.export.id

do {
    Start-Sleep -Seconds 2
    $Export = Invoke-RestMethod -Method Get -Uri "$Api/api/identities/exports/$ExportId/" -Headers $Headers -ErrorAction Stop
    Write-Host "Export status:" $Export.status
} while ($Export.status -in @("queued", "generating"))

if ($Export.status -eq "ready") {
    curl.exe -sS -L "$Api/api/identities/exports/$ExportId/download/" -H "Authorization: Bearer $AccessToken" -o "$Artifacts\identity-group-$GroupId.zip"
}
```

## Verification evidence

Automated tests cover archive contents, image deduplication, user-scoped storage,
unsafe names, biometric-data exclusion, size limits, expiry, download history,
active-export reuse, invalid options, and cross-user denial for every endpoint.

