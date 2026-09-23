# Feature 25: Watermark options

## Status

Complete for the backend. Watermarks apply to downloads of enhanced results and
gallery edits, with a preview endpoint. Shared links reuse the same engine
(Feature 26).

## Behaviour

A watermark is stamped onto a **copy** at export time. The stored enhanced or
edited file is never modified (a test compares its SHA-256 before and after), so
the clean version is always still there and the same result can be exported with
or without a mark. Nothing extra is stored: the marked copy is generated for the
response.

| `watermark` | Mark |
|---|---|
| `none` (default) | No mark; the download is byte-identical to the stored file |
| `small` | A small, unobtrusive "FaceCloud" (2.5% of the shorter edge) |
| `facecloud` | The standard "FaceCloud" brand mark (5%) |
| `username` | `@` plus the owner's username (5%) |
| `custom` | Your own text (5%) |

Other options:

| Parameter | Values | Default |
|---|---|---|
| `watermark_position` | `bottom_right`, `bottom_left`, `top_right`, `top_left`, `center`, `tile` | `bottom_right` |
| `watermark_opacity` | 0.1 to 1.0 | 0.5 |
| `watermark_text` | up to 40 characters, only for `custom` | none |

The text is white with a dark outline so it stays readable on light and dark
photos. It scales with the image, so the same setting looks alike on a phone
photo and a 20-megapixel one. Custom text is limited to Latin letters, digits and
common punctuation because the built-in font has no other glyphs; anything else
is refused with a message rather than drawn as empty boxes.

## Endpoints

All are owner-only (someone else's id is a 404).

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/enhancements/results/{id}/download/?watermark=...` | Export an enhanced result, marked if requested |
| GET | `/api/enhancements/edits/{id}/download/?watermark=...` | Same for a gallery edit |
| GET | `/api/enhancements/results/{id}/watermark-preview/?watermark=...` | Inline preview, downscaled to 1024 px, for checking placement before export |

Existing clients are unaffected: with no `watermark` parameter the download is
exactly as before. The preview requires a mode other than `none`, is served with
`Cache-Control: private, no-store`, and is **not** recorded as a download. A real
marked download is recorded in the download history under the name
`<name>_watermarked.<ext>` with the marked file's size.

Invalid options return 400 with the field that is wrong, for example
`{"watermark_opacity": ["Watermark opacity must be between 0.1 and 1.0."]}`.

## Files

- `enhancement/watermark.py`: validation and rendering (pure, no Django models).
- `enhancement/serializers.py`: `WatermarkOptionsSerializer` for the query string.
- `enhancement/views.py`: download and preview integration.

## Limits

- Images larger than `MAX_IMAGE_PIXELS` are refused for watermarking.
- Marking a very large image decodes and re-encodes it (a second or two for 20
  megapixels). JPEG output is re-encoded at `PREPROCESS_JPEG_QUALITY`.
- A visible watermark discourages reuse; it does not prevent it.

## Verification

`enhancement/test_watermark.py` (22 tests): option validation and error fields,
identical copy for `none`, source never mutated, each corner and centre placement,
tiling coverage, `small` smaller than `facecloud`, opacity, tiny images, and the
endpoints (unchanged default download, marked copy, original file untouched,
download-event record, 400s, ownership, preview behaviour, edits). The output was
also checked by eye on a real photograph.
