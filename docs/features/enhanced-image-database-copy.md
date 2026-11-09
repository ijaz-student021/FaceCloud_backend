# The enhanced image in the database, and its preview in the admin

Photographs are files, not database rows: the database holds the path, the size and the checksum (`docs/DEFENSE_DATA_AND_LOGS_GUIDE.md`). That is the right design for a product, and it means that
a person looking at the database alone sees `users/12/enhanced/423636e2….jpg`, not the picture. Two switches, both **off by default**, let a demonstration show the picture from the database side.

| Setting | What it does |
|---|---|
| `ENHANCED_IMAGE_DATABASE_COPY_ENABLED` | When an enhancement is published, also keep the image as a row in `enhancement_enhancedimageblob` (same bytes, size, SHA-256). A database viewer can open it. |
| `ADMIN_IMAGE_PREVIEW_ENABLED` | The Django admin page of an enhancement result shows the original and the enhanced image, and says whether the enhanced one came from the database row or the stored file. |

`ADMIN_IMAGE_PREVIEW_ENABLED` shows users' photographs to staff who have the view permission (each view is logged). Keep it off on a server that holds real users' photographs; switch it on for a demonstration with demo photographs.

## The table

`enhancement_enhancedimageblob`: `result_id` (one row per result, unique, deleted with the result), `content_type`, `byte_size`, `sha256`, `source_name` (the stored file it was copied from), `data` (the image, a BLOB), `created_at`.
Nothing else reads the table: no API returns it, and the result's own queries never load it.

## Never while images are encrypted

The copy is plain bytes. It is therefore **never made while `IMAGE_ENCRYPTION_ENABLED` is on, nor from a file that is stored encrypted**, so it cannot weaken encryption at rest (production requires that setting, so a
production server makes no copies). With the setting on, `manage.py copy_enhanced_images_to_database` reports `not copied: stored images are encrypted`. The admin preview does work with encrypted files: it
decrypts for a signed-in administrator, as the API does for the owner. The project's test runner turns encryption on for every test; the tests of this feature say when they need plain files.

## Making and checking the copies

```bash
python manage.py copy_enhanced_images_to_database --dry-run     # how many would be copied; writes nothing
python manage.py copy_enhanced_images_to_database              # copy every completed result that has a file (safe to repeat)
python manage.py copy_enhanced_images_to_database --verify     # writes nothing; exits with an error unless every copy is perfect
```

`--verify` accepts a copy only if it is byte-for-byte the stored file, matches its own SHA-256 and size, decodes as an image, and has the width and height the result recorded. With the switch on, new
results are copied as they finish; a failure of the copy is logged with the result id and never changes the enhancement (the copy runs after the result is published, inside its own savepoint).

## Measured on a copy of the development database (2026-11-09)

189 enhanced images, 166.7 MB (median 297 KB, largest 14.1 MB). Copying all of them took 2.5 s; `--verify` found 189 of 189 identical to the stored file and valid images of the recorded size.
The database file grew from 8.4 MB to 175.4 MB. That is the cost: every image is stored twice, and a database dump or backup carries the pictures. It is fine for SQLite in a demonstration and is not what
a production system should do (object storage, encrypted).

## Showing it

* **Django admin**: `/admin/` → *Enhancement* → *Image enhancement results* → open a result: *Enhanced image*, *Original image* and *Copy in the database* (size, type, SHA-256 prefix).
  A staff user needs the *view image enhancement result* permission; each view is logged (`Administrator <id> opened the enhanced image of enhancement result <id>`, no file name).
* **SQLite command line**: `select result_id, content_type, byte_size, substr(sha256,1,12), hex(substr(data,1,4)) from enhancement_enhancedimageblob;` (`FFD8FFE0` is the JPEG signature), and
  `select writefile('/tmp/out.jpg', data) from enhancement_enhancedimageblob where result_id=224;` writes the picture out (checked: its SHA-256 equals the stored file's).
* **DB Browser for SQLite**: open `db.sqlite3`, table `enhancement_enhancedimageblob`, *Browse Data*, select a `data` cell; the *Edit Database Cell* pane normally shows an image BLOB as a picture. Not tested here.

## Rolling back

Set both switches to `False` (nothing else changes). To remove the copies: `EnhancedImageBlob.objects.all().delete()` in a shell, or `manage.py migrate enhancement 0013` (drops the table). The stored files are never touched by any of it.

## Evidence and limits

`enhancement/test_enhanced_image_database_copy.py` (66 tests): the copy equals the file; a second run changes nothing; a changed file replaces the copy; no copy under encryption (setting on, or a file stored encrypted
with the setting later off); size mismatch and missing file are reported, not raised; the hook never raises and logs the result id; a real `process_enhancement_result` run leaves a copy only with the switch on, and
still completes when the copy fails; `--verify` catches a corrupted copy, a consistent but different copy, a missing file, a non-image and a wrong size; the admin page for staff, for staff without permission, for a
visitor, with the switch off, with and without a copy, and for a result with no file. 31 deliberate breakages of its guards (encryption refusal, checksum, size, permission, switch, headers, logging, cascade, ...) were each caught by a test. Run on SQLite; the savepoint test is meaningful on PostgreSQL (CI), where a failed write would otherwise poison the caller's transaction. Not done: a browser check of the admin page, a DB Browser
for SQLite check, a run on PostgreSQL by hand.
