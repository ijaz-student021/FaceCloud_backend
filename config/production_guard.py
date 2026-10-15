"""Refuse to start in production on an unsafe configuration.

With ``DJANGO_DEBUG=False`` the application is presumed to be facing real users,
so every rule below is a hard stop, reported together so an operator fixes the
whole list in one pass instead of one restart per mistake. ``manage.py check
--deploy`` still runs afterwards for Django's own checks.

Each rule is a function of the settings mapping that returns a list of problems,
so they are unit-tested one by one (`config/test_production_guard.py`).
"""

PLACEHOLDERS = ("replace-with", "example.com", "changeme", "change-me", "your-")
DEV_MAIL_BACKENDS = ("console", "locmem", "dummy", "filebased")
DEV_HOSTS = {"testserver", "*"}
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
    two_factor = cfg.get("TWO_FACTOR_SECRET_KEY")
    if two_factor == cfg.get("SECRET_KEY"):
        problems.append(
            "TWO_FACTOR_SECRET_KEY must be set to its own secret; it currently falls back to "
            "DJANGO_SECRET_KEY, so rotating one would lock or expose the other."
        )
    else:
        problems += _secret_problems("TWO_FACTOR_SECRET_KEY", two_factor, MIN_KEY_LENGTH)
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
    for name in ("PUBLIC_BACKEND_URL", "FRONTEND_URL"):
        value = str(cfg.get(name) or "")
        if not value.startswith("https://") or _is_placeholder(value):
            problems.append(f"{name} must be the real https:// address (got '{value}').")
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
    if cfg.get("CELERY_TASK_ALWAYS_EAGER"):
        problems.append("CELERY_TASK_ALWAYS_EAGER must be False: jobs belong on the workers, not in web requests.")
    cache = str(((cfg.get("CACHES") or {}).get("default") or {}).get("BACKEND", ""))
    if not cache or "locmem" in cache or "dummy" in cache:
        problems.append(
            "CACHE_URL must point at a shared cache (Redis): with a per-process cache every rate limit "
            "multiplies by the number of processes and resets on restart."
        )
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
    return problems


RULES = (secrets_rule, hosts_and_origins_rule, transport_rule, infrastructure_rule, policy_rule)


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
