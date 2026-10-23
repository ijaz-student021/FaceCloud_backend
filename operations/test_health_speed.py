"""GET /api/system/health/ answers quickly when a service is down: the checks run together, each is given a deadline, and the answer is kept for a few seconds (BE-003)."""
import threading
import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User

from . import services
from .services import CHECKS, DEGRADED, HEALTHY, UNAVAILABLE, build_system_health

clear_health_cache = getattr(services, "clear_health_cache", lambda: None)             # the tests are meant to fail, not break, on the old code that has none

SERVICES = ["api", "database", "redis", "celery", "storage", "email", "models"]
TOP_LEVEL = {"status", "version", "checked_at", "response_time_ms", "checks", "jobs", "services", "process"}


def healthy():
    return {"status": HEALTHY, "latency_ms": 0.1}


class Base(TestCase):
    def setUp(self):
        clear_health_cache()
        self.release = threading.Event()
        self.addCleanup(self.release.set)                   # lets a deliberately stuck check end with the test
        self.admin = User.objects.create_user(username="hs-admin", email="hs-admin@example.com", password="Password!123", is_email_verified=True, role="admin")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def slow(self, status=HEALTHY):
        def check():
            self.release.wait(8)
            return {"status": status, "latency_ms": 8000.0}
        return check

    def stubbed(self, **overrides):
        return patch.dict(CHECKS, {name: healthy for name in CHECKS} | overrides)


class SpeedTests(Base):
    def test_with_redis_down_and_a_stuck_worker_inspection_it_answers_503_with_all_seven_services_in_under_four_seconds(self):
        # The real Redis check against a closed port, a worker inspection that would wait 8 s (it waited about 6 s for real), the rest as they are.
        with override_settings(CELERY_BROKER_URL="redis://127.0.0.1:1/0"), patch.dict(CHECKS, {"celery": self.slow()}):
            started = time.perf_counter()
            response = self.client.get("/api/system/health/")
            elapsed = time.perf_counter() - started
        self.assertEqual(response.status_code, 503, response.content)
        self.assertLess(elapsed, 4.0, f"took {elapsed:.1f} s")
        body = response.json()
        self.assertEqual([row["key"] for row in body["services"]], SERVICES)
        self.assertEqual(set(body["checks"]), set(SERVICES))
        self.assertEqual(body["status"], UNAVAILABLE)
        self.assertEqual(body["checks"]["redis"]["status"], UNAVAILABLE)
        self.assertEqual(body["checks"]["celery"], {"status": DEGRADED, "latency_ms": 2000.0, "code": "timeout"})
        self.assertEqual(body["checks"]["database"]["status"], HEALTHY)                         # what was fine is still reported as fine

    def test_with_everything_up_it_answers_200(self):
        with self.stubbed():
            response = self.client.get("/api/system/health/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], HEALTHY)

    def test_the_answer_keeps_its_shape(self):
        with self.stubbed():
            body = self.client.get("/api/system/health/").json()
        self.assertEqual(set(body), TOP_LEVEL)
        self.assertEqual(set(body["services"][0]), {"key", "name", "status", "latency_ms", "checked_at", "detail"})
        self.assertEqual(set(body["jobs"]), {"pending", "active", "failed"})
        self.assertEqual(set(body["process"]), {"started_at", "uptime_seconds"})

    def test_the_checks_run_together_not_one_after_another(self):
        def wait_one_and_a_half_seconds():
            time.sleep(1.5)
            return healthy()

        with patch.dict(CHECKS, {"redis": wait_one_and_a_half_seconds, "celery": wait_one_and_a_half_seconds, "email": wait_one_and_a_half_seconds}):
            started = time.perf_counter()
            health = build_system_health()
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 3.0, f"took {elapsed:.1f} s (one after another would be 4.5 s)")
        self.assertEqual(health["checks"]["redis"]["status"], HEALTHY)

    @override_settings(SYSTEM_HEALTH_DEADLINE_SECONDS=0.3)
    def test_a_check_that_misses_the_deadline_is_a_timeout_whose_severity_follows_the_service(self):
        with self.stubbed(redis=self.slow(), email=self.slow(), storage=self.slow()):
            health = build_system_health()
        self.assertEqual({name: health["checks"][name]["status"] for name in ("redis", "storage", "email")}, {"redis": UNAVAILABLE, "storage": UNAVAILABLE, "email": DEGRADED})
        self.assertEqual({health["checks"][name]["code"] for name in ("redis", "storage", "email")}, {"timeout"})
        self.assertEqual(health["status"], UNAVAILABLE)
        self.assertEqual(health["checks"]["api"]["status"], HEALTHY)

    def test_a_check_that_raises_is_that_checks_failure_and_the_others_are_still_reported(self):
        def broken():
            raise RuntimeError("boom")

        with self.stubbed(celery=broken, redis=broken):
            health = build_system_health()
        self.assertEqual(health["checks"]["celery"], {"status": DEGRADED, "latency_ms": 0.0, "code": "check_failed"})
        self.assertEqual(health["checks"]["redis"], {"status": UNAVAILABLE, "latency_ms": 0.0, "code": "check_failed"})
        self.assertEqual(len(health["services"]), 7)

    def test_the_real_checks_still_answer_when_run_in_threads(self):
        health = build_system_health()                      # database, storage, models, email, api: the real ones, each in its own thread
        self.assertEqual(health["checks"]["database"]["status"], HEALTHY)
        self.assertEqual(health["checks"]["storage"]["status"], HEALTHY)
        self.assertEqual(health["checks"]["api"]["status"], HEALTHY)
        self.assertEqual(set(health["checks"]), set(SERVICES))


class CacheTests(Base):
    def counting(self):
        calls = []

        def check():
            calls.append(1)
            return healthy()

        return calls, check

    def test_two_requests_within_five_seconds_run_the_checks_once(self):
        calls, check = self.counting()
        with self.stubbed(api=check):
            first = self.client.get("/api/system/health/").json()
            second = self.client.get("/api/system/health/").json()
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["checked_at"], first["checked_at"])                       # the answer says when it was checked

    def test_after_the_cache_time_the_checks_run_again(self):
        calls, check = self.counting()
        with self.stubbed(api=check), override_settings(SYSTEM_HEALTH_CACHE_SECONDS=0.2):
            self.client.get("/api/system/health/")
            time.sleep(0.4)
            self.client.get("/api/system/health/")
        self.assertEqual(len(calls), 2)

    @override_settings(SYSTEM_HEALTH_CACHE_SECONDS=0)
    def test_the_cache_can_be_switched_off(self):
        calls, check = self.counting()
        with self.stubbed(api=check):
            self.client.get("/api/system/health/")
            self.client.get("/api/system/health/")
        self.assertEqual(len(calls), 2)

    def test_building_the_answer_directly_never_uses_the_cache(self):
        calls, check = self.counting()
        with self.stubbed(api=check):
            build_system_health()
            build_system_health()
        self.assertEqual(len(calls), 2)

    def test_a_503_is_kept_too_and_is_still_a_503(self):
        with self.stubbed(redis=lambda: {"status": UNAVAILABLE, "latency_ms": 1.0, "code": "connection_failed"}):
            first = self.client.get("/api/system/health/")
            second = self.client.get("/api/system/health/")
        self.assertEqual((first.status_code, second.status_code), (503, 503))
        self.assertEqual(second.json()["services"][2]["status"], "down")

    def test_only_an_administrator_can_ask(self):
        person = User.objects.create_user(username="hs-person", email="hs-person@example.com", password="Password!123", is_email_verified=True)
        client = APIClient()
        client.force_authenticate(person)
        self.assertEqual(client.get("/api/system/health/").status_code, 403)
        self.assertEqual(APIClient().get("/api/system/health/").status_code, 401)

    def test_the_checks_module_still_exposes_its_functions(self):
        for name in ("check_api", "check_database", "check_redis", "check_celery", "check_storage", "check_email", "check_models"):
            self.assertTrue(callable(getattr(services, name)), name)
