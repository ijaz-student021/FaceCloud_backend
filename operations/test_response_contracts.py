"""The documented dashboard and analytics responses match what the builders return.

Each payload is built from real rows, then compared with its serializer key by
key at every level, so a field added to a builder without being documented (or
documented but not returned) fails here.
"""
from django.test import TestCase
from rest_framework import serializers
from rest_framework.test import APIClient

from accounts.dashboard import build_user_dashboard
from accounts.models import User
from accounts.serializers import UserDashboardSerializer
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .analytics import build_admin_analytics
from .serializers import AdminAnalyticsSerializer


def assert_same_shape(test, payload, serializer, path="response"):
    if isinstance(serializer, serializers.ListSerializer):
        test.assertIsInstance(payload, list, path)
        for index, item in enumerate(payload):
            assert_same_shape(test, item, serializer.child, f"{path}[{index}]")
        return
    if not isinstance(serializer, serializers.Serializer):
        return
    fields = serializer.get_fields()
    required = {name for name, field in fields.items() if field.required}
    test.assertTrue(required <= set(payload), f"{path}: missing {sorted(required - set(payload))}")
    test.assertTrue(set(payload) <= set(fields), f"{path}: undocumented {sorted(set(payload) - set(fields))}")
    for name, value in payload.items():
        assert_same_shape(test, value, fields[name], f"{path}.{name}")


class ResponseContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="contract", email="contract@example.com",
                                             password="ForestRiver!4821", is_email_verified=True)
        self.admin = User.objects.create_user(username="contract-admin", email="contract-admin@example.com",
                                              password="ForestRiver!4821", is_email_verified=True, role="admin")
        batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        image = ImageUpload.objects.create(owner=self.user, batch=batch, original_name="a.jpg", status="uploaded",
                                           file_size=1000)
        ImageUpload.objects.create(owner=self.user, batch=batch, original_name="b.svg", status="rejected")
        ImageQualityDecision.objects.create(
            owner=self.user, image=image, status="completed", decision="rejected", overall_score=20.0,
            rejection_reasons=["too_dark"],
            preprocessing_result=ImagePreprocessingResult.objects.create(owner=self.user, image=image,
                                                                         status="completed"),
        )

    def test_the_dashboard_matches_its_documented_shape(self):
        payload = build_user_dashboard(self.user)
        self.assertTrue(payload["recent_activity"] and payload["recent_uploads"])
        assert_same_shape(self, payload, UserDashboardSerializer())

    def test_the_analytics_match_their_documented_shape(self):
        payload = build_admin_analytics()
        self.assertTrue(payload["top_rejection_reasons"])
        assert_same_shape(self, payload, AdminAnalyticsSerializer())

    def test_the_endpoints_render_through_the_serializers(self):
        client = APIClient()
        client.force_authenticate(self.user)
        dashboard = client.get("/api/user/dashboard/").json()
        self.assertEqual(dashboard["summary"]["rejected_images"], 2)
        self.assertEqual(len(dashboard["uploads_last_7_days"]), 7)
        client.force_authenticate(self.admin)
        analytics = client.get("/api/system/analytics/").json()
        self.assertEqual(set(analytics["range"]), {"from", "to"})
        self.assertEqual(analytics["top_rejection_reasons"], [{"value": "too_dark", "count": 1}])
