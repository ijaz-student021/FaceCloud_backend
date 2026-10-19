"""Refuse to start in production on an unsafe configuration.

With ``DJANGO_DEBUG=False`` the application is presumed to be facing real users,
so every rule below is a hard stop, reported together so an operator fixes the
whole list in one pass instead of one restart per mistake. ``manage.py check
--deploy`` still runs afterwards for Django's own checks.

Each rule is a function of the settings mapping that returns a list of problems,
so they are unit-tested one by one (`config/test_production_guard.py`).
"""

from urllib.parse import urlsplit

PLACEHOLDERS = ("replace-with", "example.com", "changeme", "change-me", "your-")
DEV_MAIL_BACKENDS = ("console", "locmem", "dummy", "filebased")
DEV_HOSTS = {"testserver", "*"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}  # nosec B104 - a deny-list entry for FRONTEND_URL, never bound
MIN_SECRET_LENGTH = 50
MIN_KEY_LENGTH = 32
MIN_HSTS_SECONDS = 31_536_000  # one year


def _is_placeholder(value):
    lowered = str(value).lower()
    return any(marker in lowered for marker in PLACEHOLDERS)


def _secret_problems(name, value, minimum):
    if not value or len(str(value)) < minimum:
        return [f"{name} must be at least {minimum} characters."]
    if _is_placeholder(value) or str(value).startswith("django-insecure-"):
        return [f"{name} is still a placeholder or development value."]
    if len(set(str(value))) < 12:
        return [f"{name} has too little variety to be a random secret."]
    return []


MAX_LOGIN_PER_HOUR = 100
MAX_LOGIN_IP_PER_HOUR = 1000
_PERIOD_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def _per_hour(rate):
    """``"300/hour"`` -> 300.0; ``"5/minute"`` -> 300.0; ``None`` when the text is not a rate."""
    count, _, period = str(rate).partition("/")
    if not count.isdigit() or period not in _PERIOD_SECONDS:
        return None
    return int(count) * 3600 / _PERIOD_SECONDS[period]


def secrets_rule(cfg):
    problems = _secret_problems("DJANGO_SECRET_KEY", cfg.get("SECRET_KEY"), MIN_SECRET_LENGTH)
    embedding = cfg.get("IDENTITY_EMBEDDING_KEY")
    if embedding == cfg.get("SECRET_KEY"):
        problems.append(
            "IDENTITY_EMBEDDING_KEY must be set to its own secret; it currently falls back to "
            "DJANGO_SECRET_KEY, so rotating one would lock or expose the other."
        )
    else:
        problems += _secret_problems("IDENTITY_EMBEDDING_KEY", embedding, MIN_KEY_LENGTH)
    if not cfg.get("IMAGE_ENCRYPTION_ENABLED"):
        problems.append("IMAGE_ENCRYPTION_ENABLED must be True: stored images have to be encrypted at rest.")
    else:
        images = cfg.get("IMAGE_ENCRYPTION_KEY")
        problems += _secret_problems("IMAGE_ENCRYPTION_KEY", images, MIN_KEY_LENGTH)
        if images in (cfg.get("SECRET_KEY"), embedding):
            problems.append(
                "IMAGE_ENCRYPTION_KEY must differ from DJANGO_SECRET_KEY and IDENTITY_EMBEDDING_KEY."
            )
    return problems


def hosts_and_origins_rule(cfg):
    problems = []
    hosts = list(cfg.get("ALLOWED_HOSTS") or [])
    if not hosts:
        problems.append("DJANGO_ALLOWED_HOSTS must list the hostnames the API is served on.")
    for host in hosts:
        if host in DEV_HOSTS or _is_placeholder(host):
            problems.append(f"DJANGO_ALLOWED_HOSTS contains '{host}', which is not a real production host.")
    if cfg.get("CORS_ALLOW_ALL_ORIGINS"):
        problems.append("CORS_ALLOW_ALL_ORIGINS must be False in production.")
    origins = list(cfg.get("CORS_ALLOWED_ORIGINS") or [])
    if not origins:
        problems.append("CORS_ALLOWED_ORIGINS must list the frontend origin(s).")
    for origin in origins:
        if not str(origin).startswith("https://") or _is_placeholder(origin):
            problems.append(f"CORS_ALLOWED_ORIGINS entry '{origin}' must be a real https:// origin.")
    already_flagged = set()
    for name in ("PUBLIC_BACKEND_URL", "FRONTEND_URL"):
        value = str(cfg.get(name) or "")
        if not value.startswith("https://") or _is_placeholder(value):
            problems.append(f"{name} must be the real https:// address (got '{value}').")
            already_flagged.add(name)
    # FRONTEND_URL is in every verification and password-reset email; PUBLIC_BACKEND_URL is in every share link's
    # public_url. A localhost address would send real users to their own machine, so a loopback host is refused
    # even when it is written as https.
    frontend_host = (urlsplit(str(cfg.get("FRONTEND_URL") or "")).hostname or "").lower()
    if frontend_host in LOCAL_HOSTS:
        problems.append(f"FRONTEND_URL must be the public address of the frontend, not '{frontend_host}': emailed links would not work.")
    backend_host = (urlsplit(str(cfg.get("PUBLIC_BACKEND_URL") or "")).hostname or "").lower()
    if backend_host in LOCAL_HOSTS and "PUBLIC_BACKEND_URL" not in already_flagged:
        problems.append(
            f"PUBLIC_BACKEND_URL must be the public address of this API, not '{backend_host}': share links would not open for recipients."
        )
    return problems


def transport_rule(cfg):
    problems = []
    if not cfg.get("SECURE_SSL_REDIRECT") and not cfg.get("SECURE_PROXY_SSL_HEADER"):
        problems.append(
            "Set SECURE_SSL_REDIRECT=True, or TRUST_PROXY_SSL_HEADER=True when a proxy terminates TLS."
        )
    for name in ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE"):
        if not cfg.get(name):
            problems.append(f"{name} must be True.")
    if int(cfg.get("SECURE_HSTS_SECONDS") or 0) < MIN_HSTS_SECONDS:
        problems.append(f"SECURE_HSTS_SECONDS must be at least {MIN_HSTS_SECONDS} (one year).")
    return problems


def infrastructure_rule(cfg):
    problems = []
    database = (cfg.get("DATABASES") or {}).get("default", {})
    if "sqlite" in str(database.get("ENGINE", "")):
        problems.append("Production needs PostgreSQL (DB_ENGINE), not SQLite.")
    password = str(database.get("PASSWORD") or "")
    if not password or _is_placeholder(password):
        problems.append("DB_PASSWORD must be a real database password.")
    backend = str(cfg.get("EMAIL_BACKEND") or "")
    if any(f".{kind}." in backend or backend.endswith(kind) for kind in DEV_MAIL_BACKENDS):
        problems.append("EMAIL_BACKEND is a development backend; use SMTP so verification and reset emails are sent.")
    elif not cfg.get("EMAIL_HOST") or _is_placeholder(cfg.get("EMAIL_HOST")):
        problems.append("EMAIL_HOST must be a real SMTP server.")
    elif str(cfg.get("EMAIL_HOST")).lower() not in LOCAL_HOSTS:
        for name in ("EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD"):
            value = cfg.get(name)
            if not value or _is_placeholder(value):
                problems.append(f"{name} must be set: the SMTP server needs credentials to send verification and reset emails.")
    if cfg.get("CELERY_TASK_ALWAYS_EAGER"):
        problems.append("CELERY_TASK_ALWAYS_EAGER must be False: jobs belong on the workers, not in web requests.")
    cache = str(((cfg.get("CACHES") or {}).get("default") or {}).get("BACKEND", ""))
    if not cache or "locmem" in cache or "dummy" in cache:
        problems.append(
            "CACHE_URL must point at a shared cache (Redis): with a per-process cache every rate limit "
            "multiplies by the number of processes and resets on restart."
        )
    return problems


REDIS_SCHEMES = ("redis://", "rediss://", "unix://")


def explicit_rule(cfg):
    """Settings that must be a deliberate choice, not a development default that happens to work on the machine running the check.

    ``ENV_EXPLICIT`` (built in settings.py) records which of them the operator really set. Each message names the variable, so a missing one is found in one pass.
    """
    problems = []
    explicit = cfg.get("ENV_EXPLICIT") or {}
    for name in ("DB_NAME", "DB_USER", "DB_HOST"):
        if name in explicit and not explicit[name]:
            problems.append(f"{name} must be set (the PostgreSQL database, user and host).")
    for name in ("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
        value = str(cfg.get(name) or "")
        if name in explicit and not explicit[name]:
            problems.append(f"{name} must be set to the Redis address the workers use (it defaults to a development address).")
        elif not value.startswith(REDIS_SCHEMES) or _is_placeholder(value):
            problems.append(f"{name} must be a redis:// or rediss:// address (got '{value}').")
    cache_url = str(cfg.get("CACHE_URL") or "")
    if cache_url and not cache_url.startswith(REDIS_SCHEMES):
        problems.append("CACHE_URL must be a redis:// or rediss:// address.")
    if str(cfg.get("STORAGE_BACKEND", "filesystem")) == "filesystem":
        if "MEDIA_ROOT" in explicit and not explicit["MEDIA_ROOT"]:
            problems.append("MEDIA_ROOT must be set to the directory (or mounted volume) that holds the uploads, for example /srv/facecloud/media: the default sits inside the code folder and is lost on a redeploy.")
    for origin in cfg.get("CSRF_TRUSTED_ORIGINS") or []:
        if not str(origin).startswith("https://") or "*" in str(origin) or _is_placeholder(origin):
            problems.append(f"CSRF_TRUSTED_ORIGINS entry '{origin}' must be a real https:// origin without a wildcard.")
    return problems


def policy_rule(cfg):
    problems = []
    if not cfg.get("BIOMETRIC_CONSENT_REQUIRED"):
        problems.append(
            "BIOMETRIC_CONSENT_REQUIRED must be True: face embeddings may only be created with recorded consent."
        )
    if cfg.get("OPENAI_ADVISOR_ENABLED"):
        # Switching the cloud advisor on is a decision to send previews to an external service: it must be
        # complete (a key from the secret store and a chosen model), never half-configured.
        if not cfg.get("OPENAI_API_KEY") or _is_placeholder(cfg.get("OPENAI_API_KEY")):
            problems.append("OPENAI_ADVISOR_ENABLED needs a real OPENAI_API_KEY from the secret store.")
        if cfg.get("OPENAI_BASE_URL"):
            problems.append("OPENAI_BASE_URL must be empty in production: previews may only go to the provider itself.")
        if not cfg.get("OPENAI_ADVISOR_MODEL"):
            problems.append("OPENAI_ADVISOR_ENABLED needs OPENAI_ADVISOR_MODEL (an image-capable model).")
    threshold = int(cfg.get("LOGIN_LOCK_THRESHOLD") or 0)
    if not 3 <= threshold <= 20:
        problems.append("LOGIN_LOCK_THRESHOLD must be between 3 and 20.")
    rates = (cfg.get("REST_FRAMEWORK") or {}).get("DEFAULT_THROTTLE_RATES") or {}
    for scope, ceiling in (("login", MAX_LOGIN_PER_HOUR), ("login_ip", MAX_LOGIN_IP_PER_HOUR)):
        rate = rates.get(scope)
        per_hour = _per_hour(rate) if rate else None
        if rate and (per_hour is None or per_hour > ceiling):
            problems.append(
                f"The {scope} rate limit ({rate}) must not exceed {ceiling}/hour in production (it counts failed sign-ins only); "
                f"THROTTLE_RATE_{scope.upper()} is for demos."
            )
    return problems


def model_rule(cfg):
    """Private photographs must not be written to debug artifacts, and models without a verified commercial licence need a decision."""
    problems = []
    if cfg.get("ADAPTIVE_DEBUG_ARTIFACTS"):
        problems.append("ADAPTIVE_DEBUG_ARTIFACTS must be False in production: it stores copies of users' photographs and intermediate "
                        "results outside their normal storage.")
    restricted = []
    if cfg.get("CODEFORMER_ENABLED"):
        restricted.append("CodeFormer (S-Lab licence, non-commercial)")
    if cfg.get("GFPGAN_ENABLED"):
        restricted.append("GFPGAN (weights used under a permission that is not on file)")
    if cfg.get("IDENTITY_PROVIDER") == "arcface_w600k_r50_v1":
        restricted.append("ArcFace / InsightFace weights (non-commercial)")
    if cfg.get("FACE_DETECTOR_PROVIDER") == "retinaface_v1":
        restricted.append("RetinaFace weights (terms not verified)")
    if cfg.get("ENHANCEMENT_RENDER_POLICY", "lightweight_v1") != "lightweight_v1" and cfg.get("ZERO_DCE_ENABLED"):
        restricted.append("Zero-DCE (licence not on file)")
    if restricted and not cfg.get("NONCOMMERCIAL_MODELS_ACKNOWLEDGED"):
        problems.append(
            "These models are enabled without a verified commercial licence: " + "; ".join(restricted) + ". Disable them, or set "
            "NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True once the licence position has been decided (docs/model-licenses/model-set-2026-10.md)."
        )
    return problems


RULES = (secrets_rule, hosts_and_origins_rule, transport_rule, infrastructure_rule, explicit_rule, policy_rule, model_rule)


def production_problems(cfg):
    """Every reason this configuration is unsafe for production (empty when fine)."""
    problems = []
    for rule in RULES:
        problems.extend(rule(cfg))
    return problems


def format_problems(problems):
    lines = "\n".join(f"  - {problem}" for problem in problems)
    return (
        f"Refusing to start: DJANGO_DEBUG is False but the configuration is not production-safe "
        f"({len(problems)} problem{'s' if len(problems) != 1 else ''}):\n{lines}\n"
        "See 'Before deploying' in README.md."
    )
