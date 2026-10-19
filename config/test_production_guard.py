import os
import secrets
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from config.production_guard import format_problems, production_problems


def random_secret(length=64):
    return secrets.token_urlsafe(length)[:length]


def good_config():
    """A configuration that passes every rule."""
    return {
        "SECRET_KEY": random_secret(),
        "IDENTITY_EMBEDDING_KEY": random_secret(48),
        "IMAGE_ENCRYPTION_ENABLED": True,
        "IMAGE_ENCRYPTION_KEY": random_secret(48),
        "ALLOWED_HOSTS": ["api.facecloud-app.net"],
        "CORS_ALLOW_ALL_ORIGINS": False,
        "CORS_ALLOWED_ORIGINS": ["https://app.facecloud-app.net"],
        "PUBLIC_BACKEND_URL": "https://api.facecloud-app.net",
        "FRONTEND_URL": "https://app.facecloud-app.net",
        "SECURE_SSL_REDIRECT": True,
        "SECURE_PROXY_SSL_HEADER": None,
        "SESSION_COOKIE_SECURE": True,
        "CSRF_COOKIE_SECURE": True,
        "SECURE_HSTS_SECONDS": 31_536_000,
        "DATABASES": {"default": {"ENGINE": "django.db.backends.postgresql", "PASSWORD": random_secret(24)}},
        "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "EMAIL_HOST": "smtp.mailer-service.net",
        "CELERY_TASK_ALWAYS_EAGER": False,
        "CACHES": {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                               "LOCATION": "redis://cache.internal:6379/2"}},
        "BIOMETRIC_CONSENT_REQUIRED": True,
        "LOGIN_LOCK_THRESHOLD": 5,
    }


class GuardRuleTests(SimpleTestCase):
    def problems(self, **changes):
        config = good_config()
        config.update(changes)
        return production_problems(config)

    def assertFlags(self, expected, **changes):
        joined = " | ".join(self.problems(**changes))
        self.assertIn(expected, joined, f"{changes} was not flagged: {joined or 'no problems'}")

    def test_the_login_rate_may_be_raised_a_little_but_not_turned_into_a_demo_setting(self):
        def rest(rate):
            return {"DEFAULT_THROTTLE_RATES": {"login": rate}}
        self.assertEqual(self.problems(REST_FRAMEWORK=rest("20/hour")), [])
        self.assertEqual(self.problems(REST_FRAMEWORK=rest("100/hour")), [])
        self.assertEqual(self.problems(REST_FRAMEWORK=rest("1/minute")), [])
        self.assertFlags("login rate limit", REST_FRAMEWORK=rest("300/hour"))
        self.assertFlags("login rate limit", REST_FRAMEWORK=rest("5/minute"))     # 300 an hour
        self.assertFlags("login rate limit", REST_FRAMEWORK=rest("often"))

    def test_the_per_address_failed_sign_in_ceiling_has_its_own_cap(self):
        def rest(rate):
            return {"DEFAULT_THROTTLE_RATES": {"login": "20/hour", "login_ip": rate}}
        self.assertEqual(self.problems(REST_FRAMEWORK=rest("300/hour")), [])
        self.assertEqual(self.problems(REST_FRAMEWORK=rest("1000/hour")), [])
        self.assertFlags("login_ip rate limit", REST_FRAMEWORK=rest("5000/hour"))

    def test_a_correct_production_configuration_has_no_problems(self):
        self.assertEqual(production_problems(good_config()), [])

    def test_secret_key_rules(self):
        for value in ("django-insecure-abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklm", "short", "a" * 60,
                      "replace-with-a-long-random-production-secret-0123456789abcdef", ""):
            with self.subTest(value=value[:20]):
                self.assertFlags("DJANGO_SECRET_KEY", SECRET_KEY=value)

    def test_the_embedding_key_must_be_its_own_secret(self):
        key = random_secret()
        self.assertFlags("IDENTITY_EMBEDDING_KEY must be set to its own secret", SECRET_KEY=key, IDENTITY_EMBEDDING_KEY=key)
        self.assertFlags("IDENTITY_EMBEDDING_KEY must be at least", IDENTITY_EMBEDDING_KEY="tooshort")

    def test_image_encryption_is_mandatory_with_a_distinct_strong_key(self):
        self.assertFlags("IMAGE_ENCRYPTION_ENABLED must be True", IMAGE_ENCRYPTION_ENABLED=False)
        self.assertFlags("IMAGE_ENCRYPTION_KEY must be at least", IMAGE_ENCRYPTION_KEY="short")
        config = good_config()
        for reused in ("SECRET_KEY", "IDENTITY_EMBEDDING_KEY"):
            with self.subTest(reused=reused):
                self.assertFlags("IMAGE_ENCRYPTION_KEY must differ", IMAGE_ENCRYPTION_KEY=config[reused],
                                 SECRET_KEY=config["SECRET_KEY"], IDENTITY_EMBEDDING_KEY=config["IDENTITY_EMBEDDING_KEY"])

    def test_allowed_hosts_must_be_real(self):
        self.assertFlags("DJANGO_ALLOWED_HOSTS must list", ALLOWED_HOSTS=[])
        for hosts in (["*"], ["testserver", "api.facecloud-app.net"], ["api.example.com"]):
            with self.subTest(hosts=hosts):
                self.assertFlags("not a real production host", ALLOWED_HOSTS=hosts)

    def test_cors_must_be_closed_and_https(self):
        self.assertFlags("CORS_ALLOW_ALL_ORIGINS must be False", CORS_ALLOW_ALL_ORIGINS=True)
        self.assertFlags("CORS_ALLOWED_ORIGINS must list", CORS_ALLOWED_ORIGINS=[])
        self.assertFlags("must be a real https:// origin", CORS_ALLOWED_ORIGINS=["http://app.facecloud-app.net"])
        self.assertFlags("must be a real https:// origin", CORS_ALLOWED_ORIGINS=["https://example.com"])

    def test_public_urls_must_be_real_https_addresses(self):
        self.assertFlags("PUBLIC_BACKEND_URL", PUBLIC_BACKEND_URL="http://127.0.0.1:8000")
        self.assertFlags("FRONTEND_URL", FRONTEND_URL="https://example.com")

    def test_the_backend_url_must_be_set_and_may_not_point_at_this_machine(self):
        """Every share link's public_url is built from PUBLIC_BACKEND_URL; its development default is 127.0.0.1."""
        for value in (None, "", "http://127.0.0.1:8000"):
            with self.subTest(value=value):
                self.assertFlags("PUBLIC_BACKEND_URL must be the real https", PUBLIC_BACKEND_URL=value)
        for value in ("https://localhost", "https://127.0.0.1:8443", "https://[::1]", "https://LOCALHOST:8000/api"):
            with self.subTest(value=value):
                self.assertFlags("PUBLIC_BACKEND_URL must be the public address", PUBLIC_BACKEND_URL=value)
        self.assertEqual(self.problems(PUBLIC_BACKEND_URL="https://api.facecloud-app.net/"), [])

    def test_the_frontend_url_may_not_be_http_or_point_at_this_machine(self):
        """Every verification and reset email links to FRONTEND_URL."""
        self.assertFlags("FRONTEND_URL must be the real https", FRONTEND_URL="http://app.facecloud-app.net")
        for value in ("https://localhost", "https://localhost:5173", "https://127.0.0.1", "https://127.0.0.1:8443/app", "https://[::1]",
                      "https://LOCALHOST"):
            with self.subTest(value=value):
                self.assertFlags("FRONTEND_URL must be the public address", FRONTEND_URL=value)
        self.assertEqual(self.problems(FRONTEND_URL="https://app.facecloud-app.net/"), [])

    def test_transport_security_is_required(self):
        self.assertFlags("SECURE_SSL_REDIRECT=True", SECURE_SSL_REDIRECT=False)
        self.assertEqual(
            self.problems(SECURE_SSL_REDIRECT=False, SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https")), [],
        )
        self.assertFlags("SESSION_COOKIE_SECURE", SESSION_COOKIE_SECURE=False)
        self.assertFlags("CSRF_COOKIE_SECURE", CSRF_COOKIE_SECURE=False)
        for seconds in (0, 3600):
            with self.subTest(seconds=seconds):
                self.assertFlags("SECURE_HSTS_SECONDS", SECURE_HSTS_SECONDS=seconds)

    def test_database_rules(self):
        self.assertFlags("PostgreSQL", DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "PASSWORD": "x" * 20}})
        for password in ("", "replace-with-a-strong-database-password"):
            with self.subTest(password=password):
                self.assertFlags("DB_PASSWORD", DATABASES={"default": {"ENGINE": "django.db.backends.postgresql", "PASSWORD": password}})

    def test_email_must_really_send(self):
        for backend in ("django.core.mail.backends.console.EmailBackend", "django.core.mail.backends.locmem.EmailBackend"):
            with self.subTest(backend=backend):
                self.assertFlags("development backend", EMAIL_BACKEND=backend)
        self.assertFlags("EMAIL_HOST", EMAIL_HOST="")
        self.assertFlags("EMAIL_HOST", EMAIL_HOST="smtp.example.com")

    def test_jobs_must_not_run_inside_web_requests(self):
        self.assertFlags("CELERY_TASK_ALWAYS_EAGER", CELERY_TASK_ALWAYS_EAGER=True)

    def test_rate_limits_need_a_shared_cache(self):
        local = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
        self.assertTrue(any("CACHE_URL" in problem for problem in self.problems(CACHES=local)))
        self.assertTrue(any("CACHE_URL" in problem for problem in self.problems(CACHES={})))

    def test_the_cloud_advisor_may_be_off_or_complete_but_never_half_configured(self):
        self.assertEqual(self.problems(OPENAI_ADVISOR_ENABLED=False), [])
        self.assertFlags("OPENAI_API_KEY", OPENAI_ADVISOR_ENABLED=True, OPENAI_API_KEY="", OPENAI_ADVISOR_MODEL="m")
        self.assertFlags("OPENAI_API_KEY", OPENAI_ADVISOR_ENABLED=True, OPENAI_API_KEY="changeme", OPENAI_ADVISOR_MODEL="m")
        self.assertFlags("OPENAI_ADVISOR_MODEL", OPENAI_ADVISOR_ENABLED=True, OPENAI_API_KEY=random_secret(48), OPENAI_ADVISOR_MODEL="")
        self.assertFlags("OPENAI_BASE_URL", OPENAI_ADVISOR_ENABLED=True, OPENAI_API_KEY=random_secret(48),
                         OPENAI_ADVISOR_MODEL="m", OPENAI_BASE_URL="http://127.0.0.1:9/v1")
        self.assertEqual(self.problems(OPENAI_ADVISOR_ENABLED=True, OPENAI_API_KEY=random_secret(48), OPENAI_ADVISOR_MODEL="m"), [])

    def test_policy_rules(self):
        self.assertFlags("BIOMETRIC_CONSENT_REQUIRED must be True", BIOMETRIC_CONSENT_REQUIRED=False)
        for threshold in (0, 2, 21):
            with self.subTest(threshold=threshold):
                self.assertFlags("LOGIN_LOCK_THRESHOLD", LOGIN_LOCK_THRESHOLD=threshold)

    def test_debug_artifacts_and_unlicensed_models_are_refused_unless_acknowledged(self):
        self.assertFlags("ADAPTIVE_DEBUG_ARTIFACTS must be False", ADAPTIVE_DEBUG_ARTIFACTS=True)
        for name, change in (("CodeFormer", {"CODEFORMER_ENABLED": True}), ("GFPGAN", {"GFPGAN_ENABLED": True}),
                             ("ArcFace", {"IDENTITY_PROVIDER": "arcface_w600k_r50_v1"}),
                             ("RetinaFace", {"FACE_DETECTOR_PROVIDER": "retinaface_v1"}),
                             ("Zero-DCE", {"ENHANCEMENT_RENDER_POLICY": "adaptive_v1", "ZERO_DCE_ENABLED": True})):
            with self.subTest(model=name):
                self.assertFlags(name, **change)
                self.assertEqual(self.problems(NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True, **change), [])
        # Zero-DCE is ignored by the default policy, so its default-on setting alone is not a problem
        self.assertEqual(self.problems(ZERO_DCE_ENABLED=True), [])

    def test_a_default_development_configuration_is_reported_all_at_once(self):
        problems = production_problems({
            "SECRET_KEY": "django-insecure-development-only-change-before-deployment-9f3a7c1d",
            "IDENTITY_EMBEDDING_KEY": "django-insecure-development-only-change-before-deployment-9f3a7c1d",
            "ALLOWED_HOSTS": ["127.0.0.1", "localhost", "testserver"],
            "DATABASES": {"default": {"ENGINE": "django.db.backends.sqlite3"}},
            "EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend",
            "CELERY_TASK_ALWAYS_EAGER": True,
        })
        self.assertGreaterEqual(len(problems), 15)
        message = format_problems(problems)
        self.assertIn(f"{len(problems)} problems", message)
        self.assertEqual(message.count("\n  - "), len(problems))


class StartupRefusalTests(SimpleTestCase):
    """Start the real settings module in a subprocess, as a deployment would."""

    def run_check(self, env_overrides, *arguments):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("DJANGO_", "IMAGE_ENCRYPTION_"))}
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, "manage.py", "check", *arguments],
            cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=120,
        )

    def test_production_mode_on_default_settings_refuses_to_start_and_lists_the_problems(self):
        result = self.run_check({"DJANGO_DEBUG": "False", "DJANGO_SECRET_KEY": "django-insecure-x" * 4})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to start", result.stderr)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
        self.assertGreaterEqual(result.stderr.count("\n  - "), 3)

    def test_a_correct_production_environment_starts(self):
        secret = random_secret()
        env = {
            "DJANGO_DEBUG": "False",
            "DJANGO_SECRET_KEY": secret,
            "DJANGO_ALLOWED_HOSTS": "api.facecloud-app.net",
            "CORS_ALLOW_ALL_ORIGINS": "False",
            "CORS_ALLOWED_ORIGINS": "https://app.facecloud-app.net",
            "PUBLIC_BACKEND_URL": "https://api.facecloud-app.net",
            "FRONTEND_URL": "https://app.facecloud-app.net",
            "SECURE_SSL_REDIRECT": "True",
            "SESSION_COOKIE_SECURE": "True",
            "CSRF_COOKIE_SECURE": "True",
            "SECURE_HSTS_SECONDS": "31536000",
            "SECURE_HSTS_INCLUDE_SUBDOMAINS": "True",
            "SECURE_HSTS_PRELOAD": "True",
            "DB_ENGINE": "django.db.backends.postgresql",
            "DB_NAME": "facecloud", "DB_USER": "facecloud", "DB_PASSWORD": random_secret(24),
            "DB_HOST": "127.0.0.1", "DB_PORT": "5432",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST": "smtp.mailer-service.net",
            "CELERY_TASK_ALWAYS_EAGER": "False",
            "CACHE_URL": "redis://127.0.0.1:6379/2",
            "IDENTITY_EMBEDDING_KEY": random_secret(48),
            "IMAGE_ENCRYPTION_ENABLED": "True",
            "IMAGE_ENCRYPTION_KEY": random_secret(48),
                "BIOMETRIC_CONSENT_REQUIRED": "True",
            "FACIAL_DATA_RETENTION_DAYS": "365",
        }

        result = self.run_check(env)

        self.assertEqual(result.returncode, 0, result.stderr[-1500:])

    def test_the_repository_env_example_is_refused_until_its_placeholders_are_replaced(self):
        example = Path(settings.BASE_DIR) / ".env.example"
        env = {}
        for line in example.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"')
        result = self.run_check(env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to start", result.stderr)


class VisibilityTimeoutTests(SimpleTestCase):
    """A broker timeout shorter than a task's own delays makes Redis redeliver it."""

    def start(self, **environment):
        import os
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "DJANGO_DEBUG": "True", "DB_ENGINE": "django.db.backends.sqlite3", **environment}
        return subprocess.run(
            [sys.executable, "-c", "import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); django.setup()"],
            cwd=root, env=env, capture_output=True, text=True,
        )

    def test_a_timeout_shorter_than_the_claim_lease_refuses_to_start(self):
        started = self.start(CELERY_VISIBILITY_TIMEOUT="30", ENHANCEMENT_CLAIM_LEASE_SECONDS="60",
                             CELERY_TASK_TIME_LIMIT="20", CELERY_TASK_SOFT_TIME_LIMIT="15", TEMP_FILE_MAX_AGE_HOURS="1")
        self.assertNotEqual(started.returncode, 0)
        self.assertIn("CELERY_VISIBILITY_TIMEOUT must be longer", started.stderr)

    def test_a_timeout_shorter_than_the_task_time_limit_refuses_to_start(self):
        started = self.start(CELERY_VISIBILITY_TIMEOUT="600", CELERY_TASK_TIME_LIMIT="3600")
        self.assertNotEqual(started.returncode, 0)
        self.assertIn("CELERY_VISIBILITY_TIMEOUT must be longer", started.stderr)

    def test_a_timeout_that_covers_the_run_but_not_a_claim_wait_before_it_refuses_to_start(self):
        """P2-7: the old defaults. A claim wait (up to the 1800 s lease) followed
        by a run of up to the 3600 s limit outlasts a 4500 s timeout."""
        started = self.start(CELERY_VISIBILITY_TIMEOUT="4500", CELERY_TASK_TIME_LIMIT="3600",
                             ENHANCEMENT_CLAIM_LEASE_SECONDS="1800")
        self.assertNotEqual(started.returncode, 0)
        self.assertIn("CELERY_VISIBILITY_TIMEOUT must be longer", started.stderr)

    def test_the_boundary_is_the_time_limit_plus_the_longest_delay(self):
        common = dict(CELERY_TASK_TIME_LIMIT="80", CELERY_TASK_SOFT_TIME_LIMIT="70",
                      ENHANCEMENT_CLAIM_LEASE_SECONDS="60", TEMP_FILE_MAX_AGE_HOURS="1")
        self.assertNotEqual(self.start(CELERY_VISIBILITY_TIMEOUT="140", **common).returncode, 0)
        self.assertEqual(self.start(CELERY_VISIBILITY_TIMEOUT="141", **common).returncode, 0)

    def test_the_example_environment_values_start(self):
        from pathlib import Path

        values = dict(
            line.split("=", 1) for line in (Path(__file__).resolve().parents[1] / ".env.example").read_text().splitlines()
            if line.split("=", 1)[0] in {"CELERY_VISIBILITY_TIMEOUT", "CELERY_TASK_TIME_LIMIT",
                                         "CELERY_TASK_SOFT_TIME_LIMIT", "ENHANCEMENT_CLAIM_LEASE_SECONDS"}
        )
        self.assertEqual(len(values), 4)
        started = self.start(**values)
        self.assertEqual(started.returncode, 0, started.stderr[-800:])

    def test_the_defaults_and_a_valid_short_test_setup_start(self):
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.start(CELERY_VISIBILITY_TIMEOUT="150", ENHANCEMENT_CLAIM_LEASE_SECONDS="60",
                                    CELERY_TASK_TIME_LIMIT="80", CELERY_TASK_SOFT_TIME_LIMIT="70",
                                    TEMP_FILE_MAX_AGE_HOURS="1").returncode, 0)


class CeleryRoutesTests(SimpleTestCase):
    def test_default_has_no_routes_and_the_queue_setting_isolates_every_inference_task(self):
        from config.celery_routes import INFERENCE_TASKS, task_routes

        self.assertEqual(task_routes(), {})
        routed = task_routes("inference")
        self.assertEqual(set(routed), set(INFERENCE_TASKS))
        self.assertTrue(all(route == {"queue": "inference"} for route in routed.values()))

    def test_the_gfpgan_queue_keeps_priority_for_the_two_enhancement_tasks(self):
        from config.celery_routes import task_routes

        routed = task_routes("inference", gfpgan_enabled=True, gfpgan_queue="gfpgan")
        self.assertEqual(routed["enhancement.process_image"], {"queue": "gfpgan"})
        self.assertEqual(routed["enhancement.process_batch"], {"queue": "gfpgan"})
        self.assertEqual(routed["enhancement.process_gallery_edit"], {"queue": "inference"})
