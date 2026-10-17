"""Create (or re-check) the private S3 bucket FaceCloud stores its images in, with safe settings.

    python scripts/aws_provision_bucket.py --bucket my-facecloud-media --region eu-west-1 [--kms-key-id ARN] [--dry-run]

Run it with the credentials of an IAM **administrator user or role**, never the AWS account's root keys (it refuses
them). It is idempotent: running it again changes nothing that is already right. For the bucket it sets:

* Block Public Access (all four settings), and ownership "bucket owner enforced" (no object ACLs);
* default encryption (SSE-S3, or SSE-KMS with ``--kms-key-id``) and versioning, so an overwritten or deleted object
  can be recovered;
* a lifecycle rule that aborts incomplete multipart uploads after 7 days and expires old versions after 30 days;
* a bucket policy that denies any request not made over TLS.

It then prints the least-privilege IAM policy for the *application's* IAM user or role (read, write and delete on
the key prefix, plus read-only access to the bucket settings that ``manage.py check_storage`` inspects). Create that
user or role by hand; this script never creates credentials.
"""
import argparse
import json
import sys


def app_policy(bucket, prefix="media"):
    prefix = prefix.strip("/")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "ListOnlyTheMediaPrefix", "Effect": "Allow", "Action": ["s3:ListBucket"],
             "Resource": f"arn:aws:s3:::{bucket}", "Condition": {"StringLike": {"s3:prefix": [f"{prefix}/*", prefix]}}},
            {"Sid": "ReadWriteDeleteMedia", "Effect": "Allow",
             "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
             "Resource": f"arn:aws:s3:::{bucket}/{prefix}/*"},
            {"Sid": "ReadBucketSettingsForCheckStorage", "Effect": "Allow",
             "Action": ["s3:GetBucketPublicAccessBlock", "s3:GetEncryptionConfiguration", "s3:GetBucketVersioning"],
             "Resource": f"arn:aws:s3:::{bucket}"},
        ],
    }


def tls_only_policy(bucket):
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
            "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }],
    }


def provision(s3, sts, bucket, region, kms_key_id="", dry_run=False):
    """Apply the settings. Returns a list of ``(step, what happened)``; raises SystemExit on a refusal."""
    from botocore.exceptions import ClientError

    steps = []
    identity = sts.get_caller_identity()["Arn"]
    if identity.endswith(":root"):
        raise SystemExit("Refusing to run with the AWS account's root credentials. Use an IAM administrator user or role.")
    steps.append(("credentials", f"running as {identity}"))

    def do(step, action):
        steps.append((step, "would apply" if dry_run else "applied"))
        if not dry_run:
            action()

    try:
        s3.head_bucket(Bucket=bucket)
        steps.append(("bucket", "already exists and is reachable"))
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise SystemExit(f"The bucket exists but cannot be used with these credentials ({code}).") from error
        arguments = {"Bucket": bucket, "ObjectOwnership": "BucketOwnerEnforced"}
        if region and region != "us-east-1":
            arguments["CreateBucketConfiguration"] = {"LocationConstraint": region}
        do("bucket", lambda: s3.create_bucket(**arguments))
    do("block public access", lambda: s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True}))
    do("ownership: bucket owner enforced", lambda: s3.put_bucket_ownership_controls(
        Bucket=bucket, OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}))
    default = {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": kms_key_id} if kms_key_id else {"SSEAlgorithm": "AES256"}
    do("default encryption " + default["SSEAlgorithm"], lambda: s3.put_bucket_encryption(
        Bucket=bucket, ServerSideEncryptionConfiguration={"Rules": [
            {"ApplyServerSideEncryptionByDefault": default, "BucketKeyEnabled": bool(kms_key_id)}]}))
    do("versioning", lambda: s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}))
    do("lifecycle", lambda: s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [{
        "ID": "abort-incomplete-uploads-and-expire-old-versions", "Status": "Enabled", "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}]}))
    do("TLS-only bucket policy", lambda: s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(tls_only_policy(bucket))))
    return steps


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="")
    parser.add_argument("--kms-key-id", default="")
    parser.add_argument("--key-prefix", default="media")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change; change nothing.")
    arguments = parser.parse_args(argv)

    import boto3

    options = {"region_name": arguments.region} if arguments.region else {}
    steps = provision(boto3.client("s3", **options), boto3.client("sts", **options), arguments.bucket, arguments.region,
                      arguments.kms_key_id, arguments.dry_run)
    for step, outcome in steps:
        print(f"{outcome:14s} {step}")
    print("\nLeast-privilege policy for the application's IAM user or role:\n")
    print(json.dumps(app_policy(arguments.bucket, arguments.key_prefix), indent=2))
    print("\nThen set in .env: STORAGE_BACKEND=s3, AWS_STORAGE_BUCKET_NAME, AWS_S3_REGION_NAME, IMAGE_ENCRYPTION_ENABLED=True,\n"
          "and give the app the role (or the IAM user's keys), then run: python manage.py check_storage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
