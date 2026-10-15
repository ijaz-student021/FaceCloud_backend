"""Verify the private-storage configuration: ``manage.py check_storage``.

Each check returns ``(name, status, detail)`` with status ``pass``, ``warn`` or ``fail``. A ``fail`` means the setup
would expose or lose data and exits non-zero. Nothing here creates or changes bucket settings: it only reads them and
does one write/read/delete round trip under a ``.check/`` key.
"""
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

PASS, WARN, FAIL = "pass", "warn", "fail"


def _client():
    import boto3

    options = {}
    if settings.AWS_S3_REGION_NAME:
        options["region_name"] = settings.AWS_S3_REGION_NAME
    if settings.AWS_S3_ENDPOINT_URL:
        options["endpoint_url"] = settings.AWS_S3_ENDPOINT_URL
    return boto3.client("s3", **options), boto3.client("sts", **{k: v for k, v in options.items() if k != "endpoint_url"})


def _filesystem_checks():
    results = [("backend", PASS, "filesystem storage (local media folder)")]
    results.append(("encryption at rest", PASS if settings.IMAGE_ENCRYPTION_ENABLED else WARN,
                    "images are encrypted before they are written" if settings.IMAGE_ENCRYPTION_ENABLED
                    else "IMAGE_ENCRYPTION_ENABLED is off: files are stored in plain form"))
    results.append(_round_trip())
    return results


def _round_trip():
    name = f"{settings.AWS_S3_KEY_PREFIX.strip('/')}/.check/{uuid.uuid4().hex}" if settings.STORAGE_BACKEND == "s3" else f".check/{uuid.uuid4().hex}"
    payload = b"facecloud storage check " + uuid.uuid4().bytes
    try:
        stored = default_storage.save(name, ContentFile(payload))
        with default_storage.open(stored, "rb") as handle:
            read_back = handle.read()
        size = default_storage.size(stored)
        default_storage.delete(stored)
        if default_storage.exists(stored):
            return ("write/read/delete round trip", FAIL, "the test object was not deleted")
        if read_back != payload or size != len(payload):
            return ("write/read/delete round trip", FAIL, "what was read back differs from what was written")
    except Exception as error:  # reported, never raised: this is a diagnostic
        return ("write/read/delete round trip", FAIL, f"{type(error).__name__}: {str(error)[:160]}")
    return ("write/read/delete round trip", PASS, "written, read back identical, size correct, deleted")


def _s3_checks():
    from botocore.exceptions import BotoCoreError, ClientError

    results = [("backend", PASS, f"s3 bucket {settings.AWS_STORAGE_BUCKET_NAME}, key prefix {settings.AWS_S3_KEY_PREFIX!r}")]
    try:
        s3, sts = _client()
    except Exception as error:
        return results + [("client", FAIL, f"{type(error).__name__}: {str(error)[:160]}")]
    bucket = settings.AWS_STORAGE_BUCKET_NAME
    try:
        identity = sts.get_caller_identity()["Arn"]
        results.append(("credentials", FAIL if identity.endswith(":root") else PASS,
                        "these are the AWS account's ROOT credentials: create an IAM user or role and use that instead"
                        if identity.endswith(":root") else f"running as {identity}"))
    except (ClientError, BotoCoreError) as error:
        results.append(("credentials", FAIL, f"no usable AWS credentials: {str(error)[:140]}"))
        return results
    try:
        s3.head_bucket(Bucket=bucket)
    except (ClientError, BotoCoreError) as error:
        results.append(("bucket", FAIL, f"the bucket cannot be reached with these credentials: {str(error)[:140]}"))
        return results
    results.append(("bucket", PASS, "reachable"))

    def read(call, **arguments):
        try:
            return call(**arguments), None
        except ClientError as error:
            return None, error.response.get("Error", {}).get("Code", str(error))

    block, problem = read(s3.get_public_access_block, Bucket=bucket)
    configuration = (block or {}).get("PublicAccessBlockConfiguration", {})
    if problem:
        results.append(("public access block", FAIL, f"not configured ({problem}): the bucket could be made public"))
    elif not all(configuration.get(key) for key in (
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
        results.append(("public access block", FAIL, f"not fully on: {configuration}"))
    else:
        results.append(("public access block", PASS, "all four settings are on"))

    encryption, problem = read(s3.get_bucket_encryption, Bucket=bucket)
    rules = (encryption or {}).get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
    results.append(("bucket default encryption", PASS if rules else WARN,
                    rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] if rules
                    else f"no default encryption ({problem or 'none set'}); objects still carry the per-object setting"))
    versioning, _ = read(s3.get_bucket_versioning, Bucket=bucket)
    status = (versioning or {}).get("Status")
    results.append(("versioning", PASS if status == "Enabled" else WARN,
                    "enabled (an overwritten or deleted object can be recovered)" if status == "Enabled"
                    else "off: a deleted or overwritten object cannot be recovered"))
    results.append(("client-side encryption", PASS if settings.IMAGE_ENCRYPTION_ENABLED else WARN,
                    "images are encrypted before upload, so the bucket holds ciphertext" if settings.IMAGE_ENCRYPTION_ENABLED
                    else "IMAGE_ENCRYPTION_ENABLED is off: the bucket holds readable images (server-side encryption only)"))
    results.append(_round_trip())
    return results


def run_checks():
    return _s3_checks() if settings.STORAGE_BACKEND == "s3" else _filesystem_checks()
