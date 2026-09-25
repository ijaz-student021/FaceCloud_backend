# Photo thumbnails

A gallery must not load a multi-megabyte original to draw a small tile, and uploads
can be up to 400 MB. The `thumbnails` app serves small JPEG previews of a user's
uploaded photos.

## API

`GET /api/images/{id}/thumbnail/?size=256` returns `image/jpeg` (inline, not an
attachment). `size` is optional and must be one of `THUMBNAIL_SIZES` (default
`256,1024`; the first is the default). Anything else is a 400. It needs a login;
someone else's image, a rejected image, a missing file or an undecodable photo is a
404. The response carries `Cache-Control: private, max-age=3600`.

`thumbnail_url` is returned on every image in `/api/images/` and `/api/images/my/`
(null for a rejected image), on each event photo, and as `cover_thumbnail_url` on
each event.

## How it works

- **Pre-generated after upload, otherwise on first request.** As soon as a stored
  photo is saved, a background task (`thumbnails.generate`) makes the default-size
  thumbnail after the transaction commits, so the first gallery view finds it
  cached. It is best effort: a queue outage is logged and never affects the upload,
  and the endpoint still generates on demand. `THUMBNAIL_PREGENERATE=False`
  switches it off; the larger size is always made on demand. For photos uploaded
  before this existed, `python manage.py generate_thumbnails` (options `--size`,
  `--limit`, `--dry-run`) fills in the missing ones and is safe to repeat.
- **Cached.** One `ImageThumbnail` row per image
  and size (unique constraint), one JPEG file at
  `users/<id>/thumbnails/<uuid>.jpg`. Later requests just stream the cached file.
  A cached row whose file has vanished is regenerated. Two requests racing on the
  first generation keep one row and delete the loser's file.
- **Source is the original upload**, with the EXIF orientation applied. It never
  enlarges a small photo, keeps the aspect ratio, uses only the first frame of an
  animated GIF, and flattens transparency onto white (JPEG has no alpha). A big
  JPEG is decoded at reduced size (`draft`), which is why phone photos are fast.
- **Encrypted at rest** like every other stored file, because it goes through the
  default storage (`config/storage.py`); a test reads the raw file and checks it is
  not a plain JPEG.
- **Removed with the photo.** Deleting the image (or the account) cascades to the
  rows, and a `post_delete` handler removes the files. The orphan-media scanner
  knows the `thumbnails` folder (`privacy/cleanup.py`).
- **Not facial data.** A thumbnail is a resized copy of the user's photo. No face
  is detected, embedded or read.

## Measured cost (first request, this machine, CPU)

| Source | 256 px | 1024 px | Result size |
|---|---|---|---|
| 12 MP JPEG, 3.7 MB | 95 ms | 438 ms | 18 KB / 367 KB |
| 24 MP JPEG, 6.0 MB | 195 ms | 598 ms | 16 KB / 335 KB |
| 12 MP PNG, 21.3 MB | 766 ms | 1372 ms | 18 KB / 371 KB |

A 256 px tile is roughly 200 to 1000 times smaller than the original it stands for.

## Settings

`THUMBNAIL_SIZES` (30 to 4096 pixel edges, default `256,1024`), `THUMBNAIL_JPEG_QUALITY`
(30 to 95, default 80) and the `thumbnail` throttle scope (6000/hour, since a
gallery requests many). Invalid values stop startup.

## Not built

- **Pre-generating the 1024 px size.** Only the default size is made after upload.
- **Thumbnails of enhanced or edited results** (only the original photo).
- **Loading through a bare `<img src>`.** The endpoint is protected like every
  download; a frontend fetches it with the Authorization header. Signed short-lived
  URLs would be a separate decision.
