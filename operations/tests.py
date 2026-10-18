from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .services import DEGRADED, HEALTHY, UNAVAILABLE, build_system_health


User = get_user_model()


class SystemHealthServiceTests(SimpleTestCase):
    @patch("operations.services.job_counts", return_value={"pending": 1, "active": 2, "failed": 3})
    @patch.dict(
        "operations.services.CHECKS",
        {
            "api": lambda: {"status": HEALTHY, "latency_ms": 0.1},
            "database": lambda: {"status": HEALTHY, "latency_ms": 0.1},
            "redis": lambda: {"status": HEALTHY, "latency_ms": 0.1},
            "storage": lambda: {"status": HEALTHY, "latency_ms": 0.1},
            "email": lambda: {"status": HEALTHY, "latency_ms": 0.1},
        },
        clear=True,
    )
    def test_healthy_aggregation_and_job_counts(self, mocked_counts):
        result = build_system_health()
        self.assertEqual(result["status"], HEALTHY)
        self.assertEqual(result["jobs"], {"pending": 1, "active": 2, "failed": 3})
        mocked_counts.assert_called_once()

    @patch("operations.services.job_counts", return_value={"pending": 0, "active": 0, "failed": 0})
    @patch.dict(
        "operations.services.CHECKS",
        {
            "database": lambda: {"status": HEALTHY, "latency_ms": 0.1},
            "redis": lambda: {"status": UNAVAILABLE, "latency_ms": 0.1, "code": "connection_failed"},
            "storage": lambda: {"status": HEALTHY, "latency_ms": 0.1},
        },
        clear=True,
    )
    def test_required_failure_makes_service_unavailable(self, mocked_counts):
        self.assertEqual(build_system_health()["status"], UNAVAILABLE)

    @patch("operations.services.job_counts", return_value={"pending": 0, "active": 0, "failed": 0})
    @patch.dict(
        "operations.services.CHECKS",
        {
            "database": lambda: {"status": HEALTHY, "latency_ms": 0.1},
            "redis": lambda: {"status": HEALTHY, "latency_ms": 0.1},
            "storage": lambda: {"status": HEALTHY, "latency_ms": 0.1},
            "email": lambda: {"status": DEGRADED, "latency_ms": 0.1, "code": "connection_failed"},
        },
        clear=True,
    )
    def test_optional_failure_makes_service_degraded(self, mocked_counts):
        self.assertEqual(build_system_health()["status"], DEGRADED)


@override_settings(FACECLOUD_VERSION="test-version")
class SystemHealthServicesTests(SimpleTestCase):
    """The rows the System Health screen draws, taken from the checks and the job counts."""

    @patch("operations.services.job_counts", return_value={"pending": 4, "active": 2, "failed": 0})
    @patch.dict(
        "operations.services.CHECKS",
        {
            "database": lambda: {"status": HEALTHY, "latency_ms": 0.4},
            "redis": lambda: {"status": HEALTHY, "latency_ms": 1.2},
            "celery": lambda: {"status": DEGRADED, "latency_ms": 90.0, "code": "no_workers", "workers": 0},
            "storage": lambda: {"status": HEALTHY, "latency_ms": 0.6, "available_bytes": 5000},
        },
        clear=True,
    )
    def test_each_check_becomes_a_named_row_with_the_screen_s_state_words(self, _counts):
        health = build_system_health()
        rows = {row["key"]: row for row in health["services"]}
        self.assertEqual([row["key"] for row in health["services"]], ["database", "redis", "celery", "storage"])
        self.assertEqual((rows["database"]["name"], rows["database"]["status"], rows["database"]["latency_ms"]), ("Database", "operational", 0.4))
        self.assertEqual((rows["celery"]["status"], rows["celery"]["detail"], rows["celery"]["workers"]), ("degraded", "no_workers", 0))
        self.assertEqual(rows["celery"]["queue_size"], 6)                  # waiting plus running
        self.assertEqual(rows["storage"]["available_bytes"], 5000)
        self.assertNotIn("cpu_percent", rows["database"])                  # nothing is claimed that was not measured

    @patch("operations.services.job_counts", return_value={"pending": None, "active": None, "failed": None})
    @patch.dict("operations.services.CHECKS", {"celery": lambda: {"status": UNAVAILABLE, "latency_ms": 0}, "database": lambda: {"status": HEALTHY, "latency_ms": 0}, "redis": lambda: {"status": HEALTHY, "latency_ms": 0}, "storage": lambda: {"status": HEALTHY, "latency_ms": 0}}, clear=True)
    def test_an_unknown_queue_is_null_not_zero_and_a_failed_check_reads_down(self, _counts):
        rows = {row["key"]: row for row in build_system_health()["services"]}
        self.assertIsNone(rows["celery"]["queue_size"])
        self.assertEqual(rows["celery"]["status"], "down")

    @patch("operations.services.job_counts", return_value={"pending": 0, "active": 0, "failed": 0})
    @patch.dict("operations.services.CHECKS", {"database": lambda: {"status": HEALTHY, "latency_ms": 0}, "redis": lambda: {"status": HEALTHY, "latency_ms": 0}, "storage": lambda: {"status": HEALTHY, "latency_ms": 0}}, clear=True)
    def test_the_process_says_when_it_started(self, _counts):
        process = build_system_health()["process"]
        self.assertGreaterEqual(process["uptime_seconds"], 0)
        self.assertIn("T", process["started_at"])


class SystemHealthApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="health-user",
            email="health-user@example.com",
            password="Password!123",
            role="user",
            is_email_verified=True,
        )
        self.admin = User.objects.create_user(
            username="health-admin",
            email="health-admin@example.com",
            password="Password!123",
            role="admin",
            is_email_verified=True,
        )
        self.url = reverse("operations:system-health")

    def authenticate(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def test_anonymous_and_non_admin_access_are_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)
        self.authenticate(self.user)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)

    @patch("operations.views.build_system_health")
    def test_admin_receives_health_contract(self, mocked_health):
        mocked_health.return_value = {
            "status": HEALTHY,
            "version": "test-version",
            "checked_at": "2026-09-17T08:00:00+00:00",
            "response_time_ms": 1.25,
            "checks": {"database": {"status": HEALTHY, "latency_ms": 0.1}},
            "jobs": {"pending": 0, "active": 0, "failed": 0},
        }
        self.authenticate(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["version"], "test-version")
        self.assertNotIn("error", response.data["checks"]["database"])

    @patch("operations.views.build_system_health")
    def test_admin_receives_503_for_required_dependency_failure(self, mocked_health):
        mocked_health.return_value = {
            "status": UNAVAILABLE,
            "version": "test-version",
            "checked_at": "2026-09-17T08:00:00+00:00",
            "response_time_ms": 1.25,
            "checks": {"database": {"status": UNAVAILABLE, "code": "connection_failed"}},
            "jobs": {"pending": None, "active": None, "failed": None},
        }
        self.authenticate(self.admin)
        self.assertEqual(
            self.client.get(self.url).status_code,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
