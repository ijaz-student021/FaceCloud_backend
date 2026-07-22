# Feature 47: Non-Destructive Photo Gallery Editor

## Status

Complete backend. Frontend slider and preview integration remains.

## Purpose

This feature lets a user refine an enhanced photo using Auto mode or five manual
controls: brightness, contrast, shadows, saturation, and highlights. It is
designed for natural corrections rather than aggressive filters.

## Processing rules

- The original upload is never changed.
- The primary enhanced output is never changed.
- Every saved edit is a separate `ImageEditVersion` file and database record.
- The final render uses the complete enhanced-image pixel grid.
- Auto derives conservative values from luminance percentiles and contrast.
- Manual values are accepted from -100 to 100; the recommended UI range is
  -50 to 50 for ordinary use.
- A safety loop reduces overall adjustment strength if the edit introduces more
  than the allowed highlight clipping or destroys too much contrast.
- Saturation modifies LAB chroma around its neutral point to reduce hue drift.
- At most one edit render may be queued or processing for a result at a time.

## Frontend workflow

1. Load `/api/enhancements/editor/configuration/` once.
2. Show Auto and Manual modes in the My Images photo editor.
3. In Manual mode, show the five sliders and a Reset action.
4. Preview slider changes using Canvas/WebGL without uploading every movement.
5. Send only the final chosen values to the create endpoint.
6. Poll the returned edit URL while its status is `queued` or `processing`.
7. On `completed`, replace the gallery preview with its protected download URL.
8. Let the user activate an older version or delete an unwanted version.

## API contract

### Create Auto edit

```http
POST /api/enhancements/results/{result_id}/edits/
Authorization: Bearer {access_token}
Content-Type: application/json

{"mode": "auto"}
```

### Create Manual edit

```json
{
  "mode": "manual",
  "adjustments": {
    "brightness": 8,
    "contrast": 12,
    "shadows": 10,
    "saturation": 4,
    "highlights": -8
  }
}
```

The API returns HTTP 202. `requested_adjustments` records the user's values,
`applied_adjustments` records the safe effective values, and `quality_metadata`
records the before/after measurements and any safety action.

## Privacy and security

- All editor endpoints require authentication.
- Every queryset is restricted to `owner=request.user`.
- Cross-user access returns HTTP 404 to avoid disclosing object existence.
- Storage filenames use UUID paths rather than user input.
- Deleting a version removes its physical file.
- Edited downloads are written to the owner-only download audit history.

## Main files

- `enhancement/models.py`
- `enhancement/services.py`
- `enhancement/tasks.py`
- `enhancement/serializers.py`
- `enhancement/views.py`
- `enhancement/urls.py`
- `enhancement/tests.py`
- `enhancement/migrations/0003_imageeditversion.py`
- `downloads/migrations/0002_alter_downloadevent_resource_type.py`
