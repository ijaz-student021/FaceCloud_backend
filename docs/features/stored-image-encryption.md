# Stored-image encryption at rest

## Status

Complete for local media storage (`config/storage.py`, `config/image_crypto.py`).
It is the encryption part of Feature 43; moving media to S3 or another private
bucket is still open, and a bucket would use its own server-side encryption.

## What it protects

Everything the platform saves through Django's default storage is encrypted:
originals, standardized copies, enhanced and edited images, model-comparison
previews, reports and download or identity-export packages. Someone who copies
the `media/` folder, a disk image or a backup gets ciphertext, not photos.

It does **not** protect against someone who can run the application (they get
plaintext through it) or who has both the disk and the key. The key belongs in a
secret store, separate from the media and from `DJANGO_SECRET_KEY`.

## How it works

- **AES-256-GCM in 64 KiB chunks.** Each chunk is authenticated, and its
  authenticated data includes the file header, the chunk's index and whether it
  is the last chunk. Editing, reordering, swapping or cutting chunks off the end
  is detected, including truncating a file down to nothing (the final chunk is
  authenticated when the file is opened).
- **Random access.** A reader seeks to any byte and decrypts only the chunks it
  needs, so images open directly in PIL and OpenCV, downloads get a correct
  `Content-Length`, and nothing is held whole in memory or written as plaintext
  to a temporary file.
- **Keys and rotation.** A key id in each file's header picks the key, derived
  with HKDF-SHA256 from the configured secret, exactly as for face embeddings.
- **Cost.** 30 bytes plus 16 bytes per 64 KiB (0.05%); on the 51 real files the
  worst case was 0.5% because many are small. Encryption speed is not noticeable.
- **Transparent to the code.** `storage.size()` reports the plaintext size, and
  the opened file supports `read`, `seek`, `tell`, `chunks` and re-opening like
  any Django file. An audit found no code that reads media outside the storage
  API, and the whole test suite runs with encryption switched on to keep it so.

## Configuration

| Setting | Meaning |
|---|---|
| `IMAGE_ENCRYPTION_ENABLED` | Encrypt files as they are written |
| `IMAGE_ENCRYPTION_KEY` | Required when enabled; at least 32 characters. There is **no** fallback to `DJANGO_SECRET_KEY`: enabling without a key stops startup |
| `IMAGE_ENCRYPTION_KEY_ID` | Id (1 to 255) of the current key |
| `IMAGE_ENCRYPTION_PREVIOUS_KEYS` | Retired keys as `id:secret,id:secret` so older files stay readable |

Reading never depends on the flag: an encrypted file is decrypted whatever the
setting says (the keys must still be configured), and a plaintext file is served
as is. So switching the flag on or off never makes existing files unreadable.

Generate a key with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Converting existing files

Files written before encryption was enabled stay plaintext until converted:

```powershell
python manage.py encrypt_stored_media --dry-run     # count what would change
python manage.py encrypt_stored_media               # encrypt plaintext files
python manage.py encrypt_stored_media --rotate      # also re-key files under an old key id
```

Each file is written beside the original, read back and compared by SHA-256, and
only then swapped in atomically; the modification time is kept so the orphaned
media grace period still works. A file that cannot be converted is left as it
was, counted as failed, and the command exits non-zero. It is idempotent and
resumable. Files still open in another process may fail to swap on Windows;
run it again when they are idle.

Verified on a copy of the real media folder: 51 of 51 files encrypted and
identical byte for byte when decrypted, 49 images decoded by PIL directly from
encrypted storage, a second run changed nothing.

## Rotating the key

1. Set `IMAGE_ENCRYPTION_KEY_ID=2`, `IMAGE_ENCRYPTION_KEY=<new>` and
   `IMAGE_ENCRYPTION_PREVIOUS_KEYS=1:<old>`. New files use key 2; old files still
   read.
2. `python manage.py encrypt_stored_media --rotate`
3. Once it reports 0 files under key 1, remove the old key from the previous-keys
   list.

## Failure behaviour

A corrupt, tampered or unreadable-key file raises `ImageEncryptionError`, an
`OSError`, so existing "the file cannot be read" handling applies (a share link
answers 410, a watermark request 400, and so on) instead of returning garbage.

## Not covered

- Files in the `users/<id>/<folder>/` layout only; that is everything the app
  writes, and a test ties the folder list to every file field.
- Temporary ZIP build files in the system temp directory exist briefly in
  plaintext while a package is assembled; they are deleted after the build, and
  stale ones by the cleanup task.
- Database contents other than embeddings (which are encrypted separately).

## Verification

- `config/test_image_encryption.py` (30 tests): round trips at every chunk
  boundary, awkward input pieces, hidden plaintext, random access, PIL decoding,
  bit flips, header edits, truncation, appending, swapped chunks, wrong or
  missing keys, key rotation, plaintext passthrough, the flag switched off,
  reopening a closed file, correct `Content-Length`.
- `privacy/test_media_encryption.py` (10 tests): conversion, preserved
  modification time, dry run, idempotence, rotation only when asked, a bad copy
  never replacing the original, and the command.
- The full suite (568 tests) runs with encryption on for every test.
