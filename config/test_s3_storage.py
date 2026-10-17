"""Private S3 storage (config/s3_storage.py), ``manage.py check_storage`` and the bucket provisioning script.

Everything runs against moto's in-process S3 and STS: no network and no AWS account. Skipped when moto is not installed.
"""
import importlib.util
import io
import json
import os
import secrets
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from . import image_crypto

MOTO = importlib.util.find_spec("moto") is not None and importlib.util.find_spec("storages") is not None
BUCKET = "facecloud-test-media"
KEY = "k" * 40


def load_script():
    spec = importlib.util.spec_from_file_location("aws_provision_bucket", Path(__file__).resolve().parents[1] / "scripts" / "aws_provision_bucket.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@override_settings(IMAGE_ENCRYPTION_ENABLED=True, IMAGE_ENCRYPTION_KEY=KEY, IMAGE_ENCRYPTION_KEY_ID=1, IMAGE_ENCRYPTION_PREVIOUS_KEYS={})
class EncryptedS3StorageTests(SimpleTestCase):
    def setUp(self):
        if not MOTO:
            self.skipTest("moto or django-storages is not installed.")
        from moto import mock_aws

        self.env = patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "eu-west-1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        import boto3

        self.s3 = boto3.client("s3", region_name="eu-west-1")
        self.s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        from .s3_storage import EncryptedS3Storage

        self.storage = EncryptedS3Storage(
            bucket_name=BUCKET, region_name="eu-west-1", location="media", default_acl=None, querystring_auth=True,
            file_overwrite=False, object_parameters={"ServerSideEncryption": "AES256"}, signature_version="s3v4",
            querystring_expire=300,
        )

    def stored(self, name):
        return self.s3.get_object(Bucket=BUCKET, Key=f"media/{name}")["Body"].read()

    def test_a_saved_file_is_ciphertext_in_the_bucket_and_plaintext_through_the_storage(self):
        secret = b"JPEG-pretend " + secrets.token_bytes(5000)
        name = self.storage.save("users/1/originals/abc.jpg", ContentFile(secret))
        in_bucket = self.stored(name)
        self.assertTrue(image_crypto.is_encrypted(in_bucket[:image_crypto.HEADER_LENGTH]))
        self.assertNotIn(secret[:40], in_bucket)
        with self.storage.open(name, "rb") as handle:
            self.assertEqual(handle.read(), secret)
        self.assertEqual(self.storage.size(name), len(secret))
        self.assertGreater(self.storage.raw_size(name), len(secret))
        self.assertTrue(self.storage.is_encrypted(name))
        self.assertEqual(self.storage.key_id(name), 1)

    def test_server_side_encryption_is_requested_on_every_object(self):
        name = self.storage.save("users/1/originals/sse.jpg", ContentFile(b"x" * 100))
        head = self.s3.head_object(Bucket=BUCKET, Key=f"media/{name}")
        self.assertEqual(head["ServerSideEncryption"], "AES256")

    @override_settings(IMAGE_ENCRYPTION_ENABLED=False)
    def test_with_encryption_off_the_file_is_stored_as_given_and_still_read_back(self):
        name = self.storage.save("users/1/originals/plain.bin", ContentFile(b"plain bytes"))
        self.assertEqual(self.stored(name), b"plain bytes")
        self.assertFalse(self.storage.is_encrypted(name))
        with self.storage.open(name, "rb") as handle:
            self.assertEqual(handle.read(), b"plain bytes")
        self.assertEqual(self.storage.size(name), len(b"plain bytes"))

    def test_an_object_written_before_encryption_was_switched_on_is_read_as_is(self):
        self.s3.put_object(Bucket=BUCKET, Key="media/users/1/originals/old.jpg", Body=b"old plaintext")
        with self.storage.open("users/1/originals/old.jpg", "rb") as handle:
            self.assertEqual(handle.read(), b"old plaintext")

    def test_a_file_larger_than_the_in_memory_spool_round_trips(self):
        with patch("config.s3_storage._SPOOL_BYTES", 64 * 1024):
            payload = secrets.token_bytes(3 * 1024 * 1024 + 17)
            name = self.storage.save("users/1/originals/big.bin", ContentFile(payload))
        with self.storage.open(name, "rb") as handle:
            self.assertEqual(handle.read(), payload)
        self.assertEqual(self.storage.size(name), len(payload))

    def test_the_head_is_read_by_a_ranged_request_not_a_full_download(self):
        name = self.storage.save("users/1/originals/head.bin", ContentFile(secrets.token_bytes(100_000)))
        head = self.storage._read_head(name)
        self.assertEqual(len(head), image_crypto.HEADER_LENGTH)

    def test_delete_exists_and_listing_work(self):
        first = self.storage.save("users/1/originals/a.jpg", ContentFile(b"a"))
        self.storage.save("users/1/enhanced/b.jpg", ContentFile(b"b"))
        self.assertTrue(self.storage.exists(first))
        folders, files = self.storage.listdir("users/1/originals")
        self.assertEqual(files, ["a.jpg"])
        self.storage.delete(first)
        self.assertFalse(self.storage.exists(first))

    def test_no_signed_url_is_made_for_ciphertext(self):
        name = self.storage.save("users/1/originals/u.jpg", ContentFile(b"u"))
        with self.assertRaises(NotImplementedError):
            self.storage.url(name)

    @override_settings(IMAGE_ENCRYPTION_ENABLED=False)
    def test_with_encryption_off_a_signed_expiring_url_is_possible(self):
        name = self.storage.save("users/1/originals/u2.jpg", ContentFile(b"u"))
        url = self.storage.url(name)
        self.assertIn("X-Amz-Signature", url)
        self.assertIn("X-Amz-Expires=300", url)

    def test_a_replaced_key_ring_still_reads_older_files(self):
        name = self.storage.save("users/1/originals/rot.jpg", ContentFile(b"before rotation"))
        with override_settings(IMAGE_ENCRYPTION_KEY="n" * 40, IMAGE_ENCRYPTION_KEY_ID=2, IMAGE_ENCRYPTION_PREVIOUS_KEYS={1: KEY}):
            with self.storage.open(name, "rb") as handle:
                self.assertEqual(handle.read(), b"before rotation")
            self.assertEqual(self.storage.key_id(name), 1)


@override_settings(STORAGE_BACKEND="s3", AWS_STORAGE_BUCKET_NAME=BUCKET, AWS_S3_REGION_NAME="eu-west-1", AWS_S3_ENDPOINT_URL=None,
                   AWS_S3_KEY_PREFIX="media", IMAGE_ENCRYPTION_ENABLED=True, IMAGE_ENCRYPTION_KEY=KEY, IMAGE_ENCRYPTION_KEY_ID=1,
                   IMAGE_ENCRYPTION_PREVIOUS_KEYS={})
class CheckStorageCommandTests(SimpleTestCase):
    def setUp(self):
        if not MOTO:
            self.skipTest("moto or django-storages is not installed.")
        from moto import mock_aws

        self.env = patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "eu-west-1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        import boto3

        self.s3 = boto3.client("s3", region_name="eu-west-1")
        self.s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        from django.core.files.storage import default_storage

        from .s3_storage import EncryptedS3Storage

        replacement = EncryptedS3Storage(bucket_name=BUCKET, region_name="eu-west-1", location="media", default_acl=None,
                                         file_overwrite=False, object_parameters={"ServerSideEncryption": "AES256"})
        patcher = patch.object(default_storage, "_wrapped", replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_check(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            call_command("check_storage", "--json", stdout=buffer)
        return {item["check"]: item for item in json.loads(buffer.getvalue())}

    def test_an_unprotected_bucket_fails_and_says_why(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer), self.assertRaises(SystemExit) as raised:
            call_command("check_storage", stdout=buffer)
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("FAIL  public access block", buffer.getvalue())

    def test_a_correctly_provisioned_bucket_passes(self):
        load_script().provision(self.s3, __import__("boto3").client("sts", region_name="eu-west-1"), BUCKET, "eu-west-1")
        # moto applies a bucket policy's Deny without evaluating its aws:SecureTransport condition, so it would refuse every
        # request here; on AWS the condition only blocks plain-HTTP requests. The policy itself is tested below.
        self.s3.delete_bucket_policy(Bucket=BUCKET)
        results = self.run_check()
        self.assertEqual({name: item["status"] for name, item in results.items() if item["status"] != "pass"}, {})
        self.assertEqual(results["write/read/delete round trip"]["status"], "pass")

    def test_root_credentials_are_refused(self):
        class Root:
            def get_caller_identity(self):
                return {"Arn": "arn:aws:iam::123456789012:root"}

        with patch("operations.storage_checks._client", return_value=(self.s3, Root())):
            buffer = io.StringIO()
            with redirect_stdout(buffer), self.assertRaises(SystemExit):
                call_command("check_storage", stdout=buffer)
        self.assertIn("ROOT credentials", buffer.getvalue())


class ProvisionScriptTests(SimpleTestCase):
    def setUp(self):
        if not MOTO:
            self.skipTest("moto is not installed.")
        from moto import mock_aws

        self.env = patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "eu-west-1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        import boto3

        self.s3 = boto3.client("s3", region_name="eu-west-1")
        self.sts = boto3.client("sts", region_name="eu-west-1")
        self.script = load_script()

    def test_it_creates_a_private_encrypted_versioned_bucket_and_is_idempotent(self):
        first = self.script.provision(self.s3, self.sts, BUCKET, "eu-west-1")
        second = self.script.provision(self.s3, self.sts, BUCKET, "eu-west-1")
        self.assertIn(("bucket", "applied"), first)
        self.assertIn(("bucket", "already exists and is reachable"), second)
        block = self.s3.get_public_access_block(Bucket=BUCKET)["PublicAccessBlockConfiguration"]
        self.assertTrue(all(block.values()))
        self.assertEqual(self.s3.get_bucket_versioning(Bucket=BUCKET)["Status"], "Enabled")
        rule = self.s3.get_bucket_encryption(Bucket=BUCKET)["ServerSideEncryptionConfiguration"]["Rules"][0]
        self.assertEqual(rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"], "AES256")
        lifecycle = self.s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"][0]
        self.assertEqual(lifecycle["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"], 7)
        policy = json.loads(self.s3.get_bucket_policy(Bucket=BUCKET)["Policy"])
        self.assertEqual(policy["Statement"][0]["Condition"], {"Bool": {"aws:SecureTransport": "false"}})

    def test_a_dry_run_changes_nothing(self):
        steps = self.script.provision(self.s3, self.sts, BUCKET, "eu-west-1", dry_run=True)
        self.assertIn(("bucket", "would apply"), steps)
        self.assertEqual([item["Name"] for item in self.s3.list_buckets()["Buckets"]], [])

    def test_it_refuses_root_credentials(self):
        class Root:
            def get_caller_identity(self):
                return {"Arn": "arn:aws:iam::123456789012:root"}

        with self.assertRaises(SystemExit) as raised:
            self.script.provision(self.s3, Root(), BUCKET, "eu-west-1")
        self.assertIn("root", str(raised.exception))

    def test_the_application_policy_is_least_privilege(self):
        policy = self.script.app_policy(BUCKET, "media")
        actions = {action for statement in policy["Statements" if "Statements" in policy else "Statement"] for action in
                   ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])}
        self.assertNotIn("s3:*", actions)
        self.assertNotIn("s3:PutBucketPolicy", actions)
        self.assertNotIn("s3:DeleteBucket", actions)
        object_statement = next(s for s in policy["Statement"] if s["Sid"] == "ReadWriteDeleteMedia")
        self.assertEqual(object_statement["Resource"], f"arn:aws:s3:::{BUCKET}/media/*")


@override_settings(IMAGE_ENCRYPTION_ENABLED=True, IMAGE_ENCRYPTION_KEY=KEY, IMAGE_ENCRYPTION_KEY_ID=1, IMAGE_ENCRYPTION_PREVIOUS_KEYS={},
                   CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class S3EndToEndTests(__import__("rest_framework.test", fromlist=["APITestCase"]).APITestCase):
    """The real upload and download endpoints with the S3 backend: the bucket holds ciphertext, the API returns the image."""

    def setUp(self):
        if not MOTO:
            self.skipTest("moto or django-storages is not installed.")
        from django.contrib.auth import get_user_model
        from django.core.files.storage import default_storage
        from moto import mock_aws

        self.env = patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "eu-west-1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        import boto3

        self.s3 = boto3.client("s3", region_name="eu-west-1")
        self.s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        from .s3_storage import EncryptedS3Storage

        patcher = patch.object(default_storage, "_wrapped", EncryptedS3Storage(
            bucket_name=BUCKET, region_name="eu-west-1", location="media", default_acl=None, file_overwrite=False,
            object_parameters={"ServerSideEncryption": "AES256"}))
        patcher.start()
        self.addCleanup(patcher.stop)
        from rest_framework_simplejwt.tokens import RefreshToken

        self.user = get_user_model().objects.create_user(username="s3user", email="s3@example.com", password="OrchidRiver!4729",
                                                         is_email_verified=True)
        token = RefreshToken.for_user(self.user)
        token["token_version"] = self.user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def test_an_uploaded_image_is_ciphertext_in_the_bucket_and_downloads_intact(self):
        from django.urls import reverse

        from uploads.models import ImagePreprocessingResult, ImageUpload
        from uploads.tests import make_image

        upload = make_image("holiday.jpg", size=(64, 48))
        original = upload.read()
        upload.seek(0)
        response = self.client.post(reverse("image-upload-single"), {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        image = ImageUpload.objects.get()
        key = f"media/{image.original_file.name}"
        self.assertTrue(key.startswith(f"media/users/{self.user.pk}/"))
        self.assertNotIn("holiday", key)  # the original name is metadata, never part of the key
        stored = self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        self.assertTrue(image_crypto.is_encrypted(stored[:image_crypto.HEADER_LENGTH]))
        self.assertNotIn(original[:30], stored)
        download = self.client.get(reverse("image-upload-download", args=[image.pk]))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), original)
        result = ImagePreprocessingResult.objects.get(image=image)
        self.assertTrue(result.processed_file.name)
        processed = self.s3.get_object(Bucket=BUCKET, Key=f"media/{result.processed_file.name}")["Body"].read()
        self.assertTrue(image_crypto.is_encrypted(processed[:image_crypto.HEADER_LENGTH]))

    def test_another_user_cannot_download_it(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        from rest_framework_simplejwt.tokens import RefreshToken

        from uploads.models import ImageUpload
        from uploads.tests import make_image

        self.client.post(reverse("image-upload-single"), {"file": make_image("a.jpg")}, format="multipart")
        image = ImageUpload.objects.get()
        other = get_user_model().objects.create_user(username="intruder", email="i@example.com", password="OrchidRiver!4729",
                                                     is_email_verified=True)
        token = RefreshToken.for_user(other)
        token["token_version"] = other.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        self.assertEqual(self.client.get(reverse("image-upload-download", args=[image.pk])).status_code, 404)


@override_settings(STORAGE_BACKEND="s3", IMAGE_ENCRYPTION_ENABLED=True, IMAGE_ENCRYPTION_KEY=KEY, IMAGE_ENCRYPTION_KEY_ID=1,
                   IMAGE_ENCRYPTION_PREVIOUS_KEYS={})
class CopyMediaToS3Tests(SimpleTestCase):
    def setUp(self):
        if not MOTO:
            self.skipTest("moto or django-storages is not installed.")
        import tempfile

        from moto import mock_aws

        self.env = patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "eu-west-1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        import boto3

        self.s3 = boto3.client("s3", region_name="eu-west-1")
        self.s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        from .s3_storage import EncryptedS3Storage

        self.storage = EncryptedS3Storage(bucket_name=BUCKET, region_name="eu-west-1", location="media", default_acl=None,
                                          file_overwrite=False, object_parameters={"ServerSideEncryption": "AES256"})
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "users/1/originals").mkdir(parents=True)
        self.plain = b"plain image bytes " * 200
        (self.root / "users/1/originals/plain.jpg").write_bytes(self.plain)
        ciphertext = b"".join(image_crypto.encrypt_chunks([b"already encrypted bytes " * 100], image_crypto.KeyRing(1, {1: KEY})))
        (self.root / "users/1/originals/sealed.jpg").write_bytes(ciphertext)

    def test_files_are_copied_encrypted_verified_and_the_originals_left_alone(self):
        from operations.media_to_s3 import copy_media

        before = {p.name: p.read_bytes() for p in (self.root / "users/1/originals").iterdir()}
        summary = copy_media(self.storage, self.root)
        self.assertEqual((summary["copied"], summary["failed"]), (2, 0))
        with self.storage.open("users/1/originals/plain.jpg", "rb") as handle:
            self.assertEqual(handle.read(), self.plain)
        self.assertTrue(image_crypto.is_encrypted(
            self.s3.get_object(Bucket=BUCKET, Key="media/users/1/originals/plain.jpg")["Body"].read()[:image_crypto.HEADER_LENGTH]))
        with self.storage.open("users/1/originals/sealed.jpg", "rb") as handle:
            self.assertEqual(handle.read(), b"already encrypted bytes " * 100)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "users/1/originals").iterdir()}, before)

    def test_a_second_run_skips_what_is_already_there(self):
        from operations.media_to_s3 import copy_media

        copy_media(self.storage, self.root)
        again = copy_media(self.storage, self.root)
        self.assertEqual((again["copied"], again["skipped"]), (0, 2))

    def test_a_dry_run_copies_nothing(self):
        from operations.media_to_s3 import copy_media

        summary = copy_media(self.storage, self.root, dry_run=True)
        self.assertEqual(summary["copied"], 2)
        self.assertFalse(self.storage.exists("users/1/originals/plain.jpg"))

    def test_a_file_that_cannot_be_copied_is_reported_and_the_run_goes_on(self):
        from operations.media_to_s3 import copy_media

        original = self.storage.save
        calls = {"count": 0}

        def flaky(name, content, *args, **kwargs):
            calls["count"] += 1
            if name.endswith("plain.jpg"):
                raise OSError("network down")
            return original(name, content, *args, **kwargs)

        with patch.object(self.storage, "save", side_effect=flaky):
            summary = copy_media(self.storage, self.root)
        self.assertEqual((summary["copied"], summary["failed"]), (1, 1))
        self.assertIn("network down", summary["failures"][0]["error"])
