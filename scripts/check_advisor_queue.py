"""Exercise the cloud advisor through real Redis, real Celery worker processes and PostgreSQL.

Nothing runs eagerly or in-process: every advisor job goes through Redis to
separately started Celery workers (three single-process workers, so the shared
concurrency limit is exercised across processes). The *provider* is a scripted
server on 127.0.0.1 (``enhancement/fake_openai.py``) reached by the real
``openai`` SDK through ``OPENAI_BASE_URL``: no OpenAI account, key or internet is
involved, and what this shows is the machinery (queueing, locking, limits,
retries, cancellation), not how the real model behaves.

The script provides its own isolated infrastructure and removes it afterwards:
a dedicated Redis on its own port (no persistence), a new empty PostgreSQL
database (refused if one by that name exists) and a temporary media folder.

    venv/bin/python scripts/check_advisor_queue.py --db-host 127.0.0.1 --db-port 55432 --db-user fc_test

Results go to ``.runtime/advisor_queue/<name>/`` (report.md, results.json, worker
logs). Exit code is non-zero unless every scenario passed.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_PYTHON = ROOT / "venv" / "bin" / "python"


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--redis-port", type=int, default=6392)
    parser.add_argument("--database", default="facecloud_advisor_itest")
    parser.add_argument("--db-host", default="/tmp")
    parser.add_argument("--db-port", default="5432")
    parser.add_argument("--db-user", default=os.environ.get("USER", "postgres"))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--keep", action="store_true")
    return parser.parse_args()


ARGUMENTS = parse_arguments()
OUTPUT = ROOT / ".runtime" / "advisor_queue" / ARGUMENTS.name
OUTPUT.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="facecloud-advisor-queue-"))
MEDIA = WORK / "media"
MEDIA.mkdir()
CONCURRENCY_LIMIT = 2

sys.path.insert(0, str(ROOT))
from enhancement.fake_openai import FakeResponsesServer  # noqa: E402

SERVER = FakeResponsesServer().start()

ENVIRONMENT = {
    **os.environ,
    "DJANGO_DEBUG": "True",
    "DB_ENGINE": "django.db.backends.postgresql",
    "DB_NAME": ARGUMENTS.database,
    "DB_USER": ARGUMENTS.db_user,
    "DB_PASSWORD": "unused-local-auth",
    "DB_HOST": ARGUMENTS.db_host,
    "DB_PORT": str(ARGUMENTS.db_port),
    "MEDIA_ROOT": str(MEDIA),
    "CELERY_BROKER_URL": f"redis://127.0.0.1:{ARGUMENTS.redis_port}/0",
    "CELERY_RESULT_BACKEND": f"redis://127.0.0.1:{ARGUMENTS.redis_port}/1",
    "CACHE_URL": f"redis://127.0.0.1:{ARGUMENTS.redis_port}/2",  # the shared concurrency slots live here
    "CELERY_TASK_ALWAYS_EAGER": "False",
    "THUMBNAIL_PREGENERATE": "False",
    "NOTIFICATION_EMAILS_ENABLED": "False",
    "OPENAI_ADVISOR_ENABLED": "True",
    "OPENAI_API_KEY": "sk-harness-NOT-A-REAL-KEY",
    "OPENAI_ADVISOR_MODEL": "harness-model",
    "OPENAI_BASE_URL": SERVER.base_url,
    "OPENAI_ADVISOR_MAX_CONCURRENCY": str(CONCURRENCY_LIMIT),
    "OPENAI_ADVISOR_TIMEOUT_SECONDS": "15",
    "OPENAI_ADVISOR_DEADLINE_SECONDS": "180",
    "OPENAI_ADVISOR_MAX_ATTEMPTS": "3",
}
os.environ.update(ENVIRONMENT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

PROCESSES = {}
RESULTS = []


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def start_infrastructure():
    existing = subprocess.run(
        ["psql", "-h", ARGUMENTS.db_host, "-p", str(ARGUMENTS.db_port), "-U", ARGUMENTS.db_user, "-d", "postgres", "-Atc",
         f"select 1 from pg_database where datname = '{ARGUMENTS.database}'"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if existing:
        raise SystemExit(f"Database {ARGUMENTS.database} already exists; refusing to touch it.")
    subprocess.run(["createdb", "-h", ARGUMENTS.db_host, "-p", str(ARGUMENTS.db_port), "-U", ARGUMENTS.db_user,
                    ARGUMENTS.database], check=True)
    subprocess.run(
        ["redis-server", "--port", str(ARGUMENTS.redis_port), "--save", "", "--appendonly", "no", "--dir", str(WORK),
         "--daemonize", "yes", "--pidfile", str(WORK / "redis.pid"), "--logfile", str(WORK / "redis.log")],
        check=True,
    )
    for _attempt in range(50):
        if subprocess.run(["redis-cli", "-p", str(ARGUMENTS.redis_port), "ping"], capture_output=True, text=True).stdout.strip() == "PONG":
            break
        time.sleep(0.1)
    migrate = subprocess.run([str(WEB_PYTHON), "manage.py", "migrate", "--noinput"], cwd=ROOT, env=ENVIRONMENT,
                             capture_output=True, text=True)
    if migrate.returncode:
        raise SystemExit(migrate.stderr[-2000:])
    log(f"isolated database {ARGUMENTS.database} (:{ARGUMENTS.db_port}), Redis :{ARGUMENTS.redis_port}, "
        f"provider stand-in {SERVER.base_url}")


def stop_infrastructure():
    for name in list(PROCESSES):
        stop_worker(name)
    SERVER.stop()
    subprocess.run(["redis-cli", "-p", str(ARGUMENTS.redis_port), "shutdown", "nosave"], capture_output=True)
    if not ARGUMENTS.keep:
        from django.db import connections

        connections.close_all()
        subprocess.run(["dropdb", "-h", ARGUMENTS.db_host, "-p", str(ARGUMENTS.db_port), "-U", ARGUMENTS.db_user,
                        "--if-exists", "--force", ARGUMENTS.database], capture_output=True)
        shutil.rmtree(WORK, ignore_errors=True)


def start_worker(name):
    logfile = OUTPUT / f"worker-{name}.log"
    handle = open(logfile, "a")
    process = subprocess.Popen(
        [str(WEB_PYTHON), "-m", "celery", "-A", "config", "worker", "--pool=solo", "--loglevel=info",
         "-n", f"{name}@%h", "--without-gossip", "--without-mingle"],
        cwd=ROOT, env=ENVIRONMENT, stdout=handle, stderr=subprocess.STDOUT,
    )
    PROCESSES[name] = (process, handle)
    deadline = time.time() + 120
    while time.time() < deadline:
        if "ready." in logfile.read_text():
            return
        if process.poll() is not None:
            raise SystemExit(f"worker {name} exited:\n{logfile.read_text()[-3000:]}")
        time.sleep(0.5)
    raise SystemExit(f"worker {name} did not become ready")


def stop_worker(name):
    process, handle = PROCESSES.pop(name)
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
    handle.close()


def record(name, expectation, passed, **facts):
    RESULTS.append({"scenario": name, "expects": expectation, "passed": bool(passed), **facts})
    log(f"{'PASS' if passed else 'FAIL'}  {name}: {json.dumps(facts, default=str)[:500]}")


def wait_for(predicate, timeout=120, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# ---------------------------------------------------------------- application side

def setup_django():
    import django

    django.setup()


def make_results(user, count):
    import cv2
    import numpy as np
    from django.core.files.base import ContentFile

    from enhancement.models import EnhancementLevel, ImageEnhancementResult
    from enhancement.services import process_enhancement_result
    from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

    batch = UploadBatch.objects.create(owner=user, upload_type="multiple")
    results = []
    for index in range(count):
        yy, xx = np.mgrid[0:300, 0:400].astype(np.float32)
        luminance = 110 + 40 * np.sin(xx / 400 * (4 + index)) + 25 * np.cos(yy / 300 * 4)
        array = np.clip(np.stack([luminance * 0.85, luminance, luminance * 1.1], axis=2), 0, 255).astype(np.uint8)
        encoded = cv2.imencode(".jpg", array)[1].tobytes()
        name = f"queue-{index}.jpg"
        image = ImageUpload.objects.create(
            owner=user, batch=batch, original_name=name, file_extension=".jpg", mime_type="image/jpeg",
            file_size=len(encoded), width=400, height=300, status="uploaded")
        image.original_file.save(name, ContentFile(encoded))
        pre = ImagePreprocessingResult.objects.create(
            owner=user, image=image, status="completed", output_format="jpeg", output_name=name,
            output_width=400, output_height=300, processed_file_size=len(encoded))
        pre.processed_file.save(name, ContentFile(encoded))
        result = ImageEnhancementResult.objects.create(
            owner=user, image=image, preprocessing_result=pre, status="queued", requested_level=EnhancementLevel.LOW)
        results.append(process_enhancement_result(result))
    return results


def reset_state():
    from django.core.cache import cache

    from enhancement.models import AdvisorApiCall, AdvisorRecommendation, AdvisorRun

    AdvisorRecommendation.objects.all().delete()
    AdvisorRun.objects.all().delete()
    AdvisorApiCall.objects.all().delete()
    cache.clear()
    SERVER.requests.clear()
    SERVER.peak_in_flight = 0


def statuses(run_id):
    from enhancement.models import AdvisorRecommendation

    return list(AdvisorRecommendation.objects.filter(run_id=run_id).values_list("status", "origin", "fallback_reason"))


def all_finished(run_id):
    rows = statuses(run_id)
    return rows if rows and all(status in ("completed", "failed", "cancelled", "superseded") for status, *_ in rows) else None


def main():
    start_infrastructure()
    setup_django()
    from django.conf import settings

    from accounts.models import User
    from enhancement import advisor_service as service
    from enhancement.models import AdvisorApiCall, AdvisorRecommendation

    for index in range(ARGUMENTS.workers):
        start_worker(f"w{index}")
    log(f"{ARGUMENTS.workers} Celery workers ready")

    user = User.objects.create_user(username="advisor-queue", email="advisor-queue@example.com",
                                    password="ForestRiver!4821", is_email_verified=True)
    service.record_consent(user, True, settings.OPENAI_ADVISOR_POLICY_VERSION)
    results = make_results(user, 6)

    # 1. the concurrency limit holds across worker processes
    reset_state()
    SERVER.behaviours, SERVER.delay = [{}], 1.5
    run, items = service.create_run(user, results, "auto")
    for item in items:
        service.enqueue(item["recommendation"])
    rows = wait_for(lambda: all_finished(run.pk), timeout=180)
    record("concurrency_limit_across_processes",
           f"6 jobs on {ARGUMENTS.workers} workers; at most {CONCURRENCY_LIMIT} provider requests at once; all completed",
           rows is not None and SERVER.peak_in_flight <= CONCURRENCY_LIMIT and SERVER.peak_in_flight >= 2
           and all(r[0] == "completed" and r[1] == "openai" for r in rows) and len(SERVER.requests) == 6,
           peak_in_flight=SERVER.peak_in_flight, provider_requests=len(SERVER.requests), outcomes=rows)

    # 2. one image is rate-limited then retried, one is rejected: the others are unaffected
    reset_state()
    SERVER.behaviours, SERVER.delay = [{"status": 429, "retry_after": "1", "code": "rate_limit_exceeded"}, {"status": 401}, {}], 0.2
    run, items = service.create_run(user, results, "auto")
    for item in items:
        service.enqueue(item["recommendation"])
    rows = wait_for(lambda: all_finished(run.pk), timeout=240)
    outcomes = sorted(AdvisorApiCall.objects.values_list("outcome", flat=True))
    record("failures_stay_with_their_image",
           "one 429 retried and then served, one 401 falls back, four unaffected; each paid attempt in the ledger",
           rows is not None and all(r[0] == "completed" for r in rows)
           and sum(1 for r in rows if r[1] == "local_fallback" and r[2] == "not_authorized") == 1
           and sum(1 for r in rows if r[1] == "openai") == 5
           and outcomes.count("rate_limited") == 1 and outcomes.count("not_authorized") == 1 and outcomes.count("ok") == 5,
           outcomes=rows, ledger=outcomes, provider_requests=len(SERVER.requests))

    # 3. the same job delivered twice at once is sent once
    reset_state()
    SERVER.behaviours, SERVER.delay = [{}], 1.5
    recommendation, _ = service.create_recommendation(user, results[0], "auto")
    from enhancement.tasks import advisor_recommend

    advisor_recommend.apply_async(args=[recommendation.pk])
    advisor_recommend.apply_async(args=[recommendation.pk])
    row = wait_for(lambda: AdvisorRecommendation.objects.filter(pk=recommendation.pk, status="completed").first(), timeout=120)
    time.sleep(2)
    record("duplicate_delivery_sends_once", "one provider request, attempts == 1",
           row is not None and len(SERVER.requests) == 1 and AdvisorRecommendation.objects.get(pk=recommendation.pk).attempts == 1,
           provider_requests=len(SERVER.requests))

    # 4. cancelling a run: nothing finishes after it; an answer in flight is discarded
    reset_state()
    SERVER.behaviours, SERVER.delay = [{}], 5.0
    run, items = service.create_run(user, results[:4], "auto")
    for item in items:
        service.enqueue(item["recommendation"])
    wait_for(lambda: SERVER.in_flight >= 1, timeout=60)
    service.cancel_run(run)
    time.sleep(8)  # long enough for the in-flight answers to arrive
    rows = statuses(run.pk)
    record("cancel_discards_inflight_answers", "every job of the run ends cancelled; no answer is published",
           rows and all(r[0] == "cancelled" for r in rows)
           and not AdvisorRecommendation.objects.filter(run=run).exclude(adjustments={}).exists(),
           outcomes=rows, provider_requests=len(SERVER.requests))

    # the run never asked the provider anything that identifies the user or the files
    sent = json.dumps([r["body"] for r in SERVER.requests] or [{}])
    record("requests_carry_no_identity", "no email, username or file name in any request body",
           all(token not in sent for token in ("advisor-queue", "example.com", "queue-0.jpg", "original")), requests=len(SERVER.requests))


def write_report():
    passed = all(item["passed"] for item in RESULTS)
    (OUTPUT / "results.json").write_text(json.dumps(RESULTS, indent=2, default=str))
    lines = ["# Advisor queue integration", "",
             f"Provider: scripted local server (no OpenAI). Workers: {ARGUMENTS.workers} single-process Celery workers; "
             f"concurrency limit {CONCURRENCY_LIMIT}.", "", "| Scenario | Expectation | Result |", "|---|---|---|"]
    for item in RESULTS:
        lines.append(f"| {item['scenario']} | {item['expects']} | {'PASS' if item['passed'] else 'FAIL'} |")
    (OUTPUT / "report.md").write_text("\n".join(lines) + "\n")
    return passed


if __name__ == "__main__":
    code = 1
    try:
        main()
        code = 0 if write_report() else 1
    finally:
        stop_infrastructure()
    sys.exit(code)
