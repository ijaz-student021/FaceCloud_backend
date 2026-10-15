# Private storage on AWS S3 (Feature 43)

FaceCloud can keep every stored image in a **private S3 bucket**. Set `STORAGE_BACKEND=s3`; nothing else in the code changes,
because every file field already uses Django's default storage (`config/s3_storage.py` is the backend). Local disk
(`STORAGE_BACKEND=filesystem`) remains the default.

## What it guarantees

* **The bucket holds ciphertext.** With `IMAGE_ENCRYPTION_ENABLED=True` each file is encrypted (AES-256-GCM, the key ring
  in `config/image_crypto.py`) *before it leaves the server*. S3's server-side encryption (SSE-S3, or SSE-KMS) is added
  on top. Someone with read access to the bucket, or to a leaked backup, sees nothing usable.
* **Private by construction.** Block Public Access is on, no object ACLs (bucket-owner-enforced), a bucket policy refuses
  any request not made over TLS, and the application's role can only read, write and delete under its own key prefix.
* **No signed URL for ciphertext.** Downloads stream through the authenticated API, which checks ownership and decrypts.
  `url()` refuses while encryption is on.
* **Secure object keys.** `media/users/<id>/<kind>/<uuid>.<ext>`: the uploader's file name is metadata in the database, never part of a key.
* **Recoverable.** Versioning keeps an overwritten or deleted object for 30 days; incomplete multipart uploads are
  aborted after 7.

## Step 0: lock the AWS account (do this first)

You created a root account. The root user is for account recovery and billing only; it must never run the application.

1. Sign in as root. **Turn on MFA** for the root user (Security credentials, authenticator app or passkey).
2. **Delete any root access keys** (Security credentials, Access keys). The root user should have none.
3. Create an **administrator IAM user** (or IAM Identity Center user) for yourself with MFA, and use it from now on.
4. Set a **budget alert** (Billing, Budgets, for example 10 USD a month) so a mistake is noticed in hours, not at month end.
5. Pick one **region** (for example `eu-west-1`) and keep to it.

## Step 1: create the bucket (one command, safe defaults)

With the administrator user's credentials (`aws configure`, or `AWS_PROFILE`), never the root keys. The script refuses them:

```
python scripts/aws_provision_bucket.py --bucket my-facecloud-media --region eu-west-1 --dry-run   # show the plan
python scripts/aws_provision_bucket.py --bucket my-facecloud-media --region eu-west-1             # apply it
```

It is idempotent. It sets Block Public Access (all four), bucket-owner-enforced ownership, default encryption (SSE-S3, or
`--kms-key-id` for SSE-KMS), versioning, the lifecycle rule and the TLS-only policy, then **prints the least-privilege IAM
policy** for the application. Bucket names are global, so choose a unique one.

## Step 2: give the application its own identity

Create an IAM **role** (if it runs on AWS) or an IAM **user** (if it runs elsewhere, for example your laptop or a VPS) and
attach the policy the script printed. It allows `GetObject`, `PutObject`, `DeleteObject` and `AbortMultipartUpload` on
`media/*`, `ListBucket` under that prefix, and read-only access to three bucket settings (so `check_storage` can
inspect them). It does not allow changing the bucket policy, deleting the bucket or anything else. For an IAM user,
create an access key and keep it only in `.env` or the host's secret store; rotate it periodically.

## Step 3: configure and verify

```
STORAGE_BACKEND=s3
AWS_STORAGE_BUCKET_NAME=my-facecloud-media
AWS_S3_REGION_NAME=eu-west-1
IMAGE_ENCRYPTION_ENABLED=True          # and IMAGE_ENCRYPTION_KEY (32+ characters), kept outside the bucket
AWS_ACCESS_KEY_ID=...                  # an IAM user's keys, or omit when the host has an IAM role
AWS_SECRET_ACCESS_KEY=...
```

```
python manage.py check_storage
```

It fails (exit 1) if the credentials are the account's **root** keys, the bucket is reachable but Block Public Access is
not fully on, or a test file cannot be written, read back identical, and deleted. It warns if versioning or default
encryption is off, or if `IMAGE_ENCRYPTION_ENABLED` is off (then the bucket holds readable images).

## Step 4: move the existing images

```
python manage.py copy_media_to_s3 --dry-run     # count what would be copied
python manage.py copy_media_to_s3               # copy, verify each file by SHA-256
```

Local files are never changed or deleted; encrypted files are copied as they are, plaintext ones are encrypted on the way in,
and an interrupted run resumes (files already in the bucket with the same digest are skipped). Remove the local copies by
hand only after the app has run against the bucket. `manage.py encrypt_stored_media` is for the filesystem backend; on S3 every
new object is encrypted at write.

## Costs and limits, stated plainly

* S3 storage is billed per GB-month plus requests and data transfer out; the images of a small deployment cost cents
  to a few dollars a month. The budget alert in Step 0 is the safeguard.
* A download reads the whole object into a temporary file, then decrypts it as it streams: memory is bounded
  (16 MB, then disk), but a very large file costs the transfer time.
* **Not built:** per-user storage quotas and a usage dashboard (the item still open in `docs/FEATURES.md`), and
  signed download URLs for plaintext objects (possible only with encryption off, so not recommended).
* Verified here against an in-process S3 (moto): not against a real AWS account, which needs your credentials.
  Run `check_storage` against the real bucket as the acceptance test.
