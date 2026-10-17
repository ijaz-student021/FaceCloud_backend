"""Exercise the real Redis/Celery flow end to end.

Nothing here runs eagerly or in-process: every task goes through Redis to a
separately started Celery worker. The script provides its own isolated
infrastructure and removes it afterwards:

* a dedicated Redis server on its own port, with no persistence;
* a new, empty PostgreSQL database (refused if one by that name exists);
* a temporary media folder.

One worker environment (the web environment) serves the default queue, as a
deployment would run it: preprocessing, thumbnails and enhancement. The
enhancement pipeline is OpenCV and Pillow only; there is no separate model worker.

    venv/bin/python scripts/check_queue_integration.py --portrait ~/Downloads/portrait.jpg

(Any photograph will do; the name is historical.)

Scenarios: a full upload, preprocessing, enhancement, storage, progress and
download flow; duplicate delivery to two workers at once; a real database
connection failure mid-task, then retry; cancellation mid-batch; a worker killed
mid-task, then restarted; a duplicate arriving while the original runs one stage
longer than the claim lease (renewal, bounded waiting); an old worker that fails
after another took the result over; and a worker killed in the middle of a batch.
Long stages and failures are injected by ``scripts/queue_harness_worker.py``
around the real pipeline. To keep the run short, the claim lease, task time limit
and broker visibility timeout are lowered (60 s, 200 s and 300 s); the report says so.

Results go to ``.runtime/queue_integration/<name>/`` (report.md, results.json,
worker logs). Exit code is non-zero unless every scenario passed.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_PYTHON = ROOT / "venv" / "bin" / "python"


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--portrait", required=True)
    parser.add_argument("--name", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--redis-port", type=int, default=6391)
    parser.add_argument("--database", default="facecloud_queue_itest")
    parser.add_argument("--db-host", default=os.environ.get("PGHOST", "/tmp"))  # nosec B108 - the usual PostgreSQL socket directory
    parser.add_argument("--db-port", default="5432")
    parser.add_argument("--db-user", default=os.environ.get("USER", "postgres"))
    parser.add_argument("--keep", action="store_true", help="Leave the database and Redis running afterwards.")
    parser.add_argument("--scenarios", default="", help="Comma-separated subset to run (default: all).")
    parser.add_argument("--visibility-timeout", type=int, default=300,
                        help="Broker visibility timeout for the run, seconds (production default 6300). It must "
                             "exceed the task time limit plus the claim lease (200 s + 60 s here).")
    return parser.parse_args()


ARGUMENTS = parse_arguments()
LEASE = 60
TIME_LIMIT = 200  # covers the longest injected stage (150 s); solo workers do not enforce it
HARNESS_WORKER = ROOT / "scripts" / "queue_harness_worker.py"
OUTPUT = ROOT / ".runtime" / "queue_integration" / ARGUMENTS.name
OUTPUT.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="facecloud-queue-"))
MEDIA = WORK / "media"
MEDIA.mkdir()

ENVIRONMENT = {
    **os.environ,
    "DJANGO_DEBUG": "True",
    "DB_ENGINE": "django.db.backends.postgresql",
    "DB_NAME": ARGUMENTS.database,
    "DB_USER": ARGUMENTS.db_user,
    "DB_PASSWORD": "unused-local-socket-auth",
    "DB_HOST": ARGUMENTS.db_host,
    "DB_PORT": str(ARGUMENTS.db_port),
    "MEDIA_ROOT": str(MEDIA),
    "CELERY_BROKER_URL": f"redis://127.0.0.1:{ARGUMENTS.redis_port}/0",
    "CELERY_RESULT_BACKEND": f"redis://127.0.0.1:{ARGUMENTS.redis_port}/1",
    "CELERY_TASK_ALWAYS_EAGER": "False",
    "IDENTITY_GUARD_ENABLED": "True",
    "THUMBNAIL_PREGENERATE": "False",
    "NOTIFICATION_EMAILS_ENABLED": "False",
    # Lowered so the worker-restart scenario finishes in minutes, not an hour.
    "CELERY_VISIBILITY_TIMEOUT": str(ARGUMENTS.visibility_timeout),
    "ENHANCEMENT_CLAIM_LEASE_SECONDS": str(LEASE),
    "CELERY_TASK_TIME_LIMIT": str(TIME_LIMIT),
    "CELERY_TASK_SOFT_TIME_LIMIT": str(TIME_LIMIT - 10),
    "TEMP_FILE_MAX_AGE_HOURS": "1",
}
os.environ.update(ENVIRONMENT)
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

PROCESSES = {}
RESULTS = []


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def start_infrastructure():
    # The name goes in as a psql variable (quoted by psql), never into the SQL text.
    existing = subprocess.run(
        ["psql", "-h", ARGUMENTS.db_host, "-p", str(ARGUMENTS.db_port), "-U", ARGUMENTS.db_user, "-d", "postgres",
         "-Atq", "-v", f"wanted={ARGUMENTS.database}"],
        input="select 1 from pg_database where datname = :'wanted'", capture_output=True, text=True, check=True,
    ).stdout.strip()
    if existing:
        raise SystemExit(f"Database {ARGUMENTS.database} already exists; refusing to touch it.")
    subprocess.run(["createdb", "-h", ARGUMENTS.db_host, "-p", str(ARGUMENTS.db_port), "-U", ARGUMENTS.db_user, ARGUMENTS.database], check=True)
    subprocess.run(
        ["redis-server", "--port", str(ARGUMENTS.redis_port), "--save", "", "--appendonly", "no",
         "--dir", str(WORK), "--daemonize", "yes", "--pidfile", str(WORK / "redis.pid"),
         "--logfile", str(WORK / "redis.log")],
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
    log(f"isolated database {ARGUMENTS.database}, Redis on :{ARGUMENTS.redis_port}, media in {MEDIA}")


def stop_infrastructure():
    for name in list(PROCESSES):
        stop_worker(name)
    subprocess.run(["redis-cli", "-p", str(ARGUMENTS.redis_port), "shutdown", "nosave"], capture_output=True)
    if not ARGUMENTS.keep:
        from django.db import connections

        connections.close_all()
        subprocess.run(["dropdb", "-h", ARGUMENTS.db_host, "-p", str(ARGUMENTS.db_port), "-U", ARGUMENTS.db_user, "--if-exists", "--force",
                        ARGUMENTS.database], capture_output=True)
        shutil.rmtree(WORK, ignore_errors=True)


def start_worker(name, python, queue, fault=""):
    """Start a real Celery worker; ``fault`` is a HARNESS_FAULT spec (see queue_harness_worker.py)."""
    logfile = OUTPUT / f"worker-{name}.log"
    handle = open(logfile, "a")
    handle.write(f"\n===== started {time.strftime('%H:%M:%S')} fault={fault or 'none'} =====\n")
    handle.flush()
    process = subprocess.Popen(
        [str(python), str(HARNESS_WORKER), "-A", "config", "worker", "--pool=solo", "--loglevel=info",
         "-Q", queue, "-n", f"{name}@%h", "--without-gossip", "--without-mingle"],
        cwd=ROOT, env={**ENVIRONMENT, "HARNESS_FAULT": fault}, stdout=handle, stderr=subprocess.STDOUT,
    )
    PROCESSES[name] = (process, handle, logfile)
    deadline = time.time() + 120
    while time.time() < deadline:
        if "ready." in logfile.read_text():
            log(f"worker {name} ready on queue '{queue}' ({python.parent.parent.name} environment)")
            return
        if process.poll() is not None:
            raise SystemExit(f"worker {name} exited:\n{logfile.read_text()[-3000:]}")
        time.sleep(0.5)
    raise SystemExit(f"worker {name} did not become ready")


def stop_worker(name, kill=False):
    process, handle, _logfile = PROCESSES.pop(name)
    if process.poll() is None:
        process.send_signal(signal.SIGKILL if kill else signal.SIGTERM)
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
    handle.close()


def worker_log(name):
    return (OUTPUT / f"worker-{name}.log").read_text()


def deliveries(task_id):
    """Which workers received ``task_id``, and how often each."""
    found = {}
    for path in OUTPUT.glob("worker-*.log"):
        count = len(re.findall(rf"Task [\w.]+\[{re.escape(task_id)}\] received", path.read_text()))
        if count:
            found[path.stem.removeprefix("worker-")] = count
    return found


def record(name, expectation, passed, **facts):
    RESULTS.append({"scenario": name, "expects": expectation, "passed": bool(passed), **facts})
    log(f"{'PASS' if passed else 'FAIL'}  {name}: {json.dumps(facts, default=str)[:400]}")


# ---------------------------------------------------------------- application side

def setup_django():
    import django

    django.setup()


def images(portrait_path):
    import cv2

    portrait = cv2.imread(str(portrait_path), cv2.IMREAD_COLOR)

    def resized(longest):
        scale = longest / max(portrait.shape[:2])
        return cv2.resize(portrait, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    degraded = cv2.GaussianBlur(resized(420), (0, 0), 1.6)
    degraded = cv2.imdecode(cv2.imencode(".jpg", degraded, [cv2.IMWRITE_JPEG_QUALITY, 35])[1], cv2.IMREAD_COLOR)
    return {"degraded_face": degraded, "portrait_large": resized(2400), "portrait_medium": resized(1400)}


def jpeg(image, name):
    import cv2
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes(),
                              content_type="image/jpeg")


class Api:
    def __init__(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.create_user(
            username=f"queue-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.user = user
        self.client = APIClient()
        self.client.force_authenticate(user)

    def upload(self, *named_images):
        if len(named_images) == 1:
            response = self.client.post("/api/images/upload/single/", {"file": jpeg(*named_images[0][::-1])}, format="multipart")
        else:
            response = self.client.post(
                "/api/images/upload/multiple/", {"files": [jpeg(image, name) for name, image in named_images]},
                format="multipart",
            )
        assert response.status_code == 201, response.content[:500]
        return response.json()

    def wait_preprocessed(self, batch_id, timeout=120):
        from uploads.models import ImagePreprocessingResult, ImageUpload

        deadline = time.time() + timeout
        while time.time() < deadline:
            images = list(ImageUpload.objects.filter(batch_id=batch_id, status="uploaded"))
            done = ImagePreprocessingResult.objects.filter(image__in=images, status="completed").count()
            if images and done == len(images):
                return images
            time.sleep(0.5)
        raise AssertionError("preprocessing did not finish")

    def enhance(self, image_id, **body):
        response = self.client.post(f"/api/enhancements/images/{image_id}/", {"level": "auto", "style": "natural", **body}, format="json")
        assert response.status_code == 202, response.content[:500]
        return response.json()

    def enhance_batch(self, batch_id):
        response = self.client.post(f"/api/enhancements/batches/{batch_id}/", {"level": "auto"}, format="json")
        assert response.status_code == 201, response.content[:500]
        return response.json()


def wait_for(predicate, timeout, interval=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def result_row(result_id):
    from enhancement.models import ImageEnhancementResult

    return ImageEnhancementResult.objects.get(pk=result_id)


def stored_outputs(result_id):
    """This result's own output file on disk ([] if none), plus any orphan beside it.

    Output files get random names, so "published once" is checked as: the
    result's file exists, and no file in its folder is unreferenced by any
    result (an orphan would be a second, discarded or duplicate publication).
    """
    from enhancement.models import ImageEnhancementResult

    row = result_row(result_id)
    if not row.enhanced_file:
        return []
    folder = MEDIA / Path(row.enhanced_file.name).parent
    referenced = {
        Path(name).name for name in ImageEnhancementResult.objects.filter(owner_id=row.owner_id)
        .exclude(enhanced_file="").values_list("enhanced_file", flat=True)
    }
    on_disk = {path.name for path in folder.iterdir()} if folder.is_dir() else set()
    own = Path(row.enhanced_file.name).name
    return ([own] if own in on_disk else []) + sorted(f"ORPHAN:{name}" for name in on_disk - referenced)


# ---------------------------------------------------------------- scenarios

def scenario_routing_and_full_flow(api, pictures):
    import cv2
    import numpy as np

    batch = api.upload(("degraded_face.jpg", pictures["degraded_face"]))
    image = api.wait_preprocessed(batch["id"])[0]
    preprocess_task = batch.get("processing_task_id") or ""
    queued = api.enhance(image.pk)
    finished = wait_for(lambda: result_row(queued["id"]).status in {"completed", "failed"} and result_row(queued["id"]), 180)
    row = result_row(queued["id"])
    parameters = row.applied_parameters or {}
    download = api.client.get(f"/api/enhancements/results/{row.pk}/download/")
    body = b"".join(download.streaming_content) if hasattr(download, "streaming_content") else download.content
    with row.enhanced_file.open("rb") as handle:
        stored = handle.read()
    decoded = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
    progress = api.client.get(f"/api/images/batches/{batch['id']}/progress/").json()
    retired_keys = [key for key in ("zero_dce", "hd_restoration", "purevision", "purescale", "face_restoration", "colorization")
                    if key in parameters]
    routed = deliveries(row.task_id)
    record(
        "routing_and_full_flow",
        "preprocessing and enhancement run on the worker; the result is the lightweight pipeline's, with no retired stage; "
        "file stored once; download returns the stored image; progress reports completion",
        bool(finished) and row.status == "completed" and routed == {"enhance": 1}
        and parameters.get("render_policy", {}).get("name") == "lightweight_v1" and not retired_keys
        and str(parameters.get("algorithm", "")).startswith("opencv_adaptive_restoration_router_v2")
        and download.status_code == 200 and body == stored and decoded is not None
        and list(decoded.shape[:2]) == [row.output_height, row.output_width],
        status=row.status, enhancement_task_delivered_to=routed,
        preprocessing_task_delivered_to=deliveries(preprocess_task) if preprocess_task else "n/a",
        algorithm=parameters.get("algorithm"), retired_keys_in_result=retired_keys,
        identity_status=parameters.get("identity_status"),
        download_status=download.status_code, download_matches_stored_file=body == stored,
        output=[row.output_width, row.output_height], processing_ms=row.processing_duration_ms,
        progress={key: progress.get(key) for key in ("status", "progress_percentage", "completed", "total") if key in progress},
    )
    cv2.imwrite(str(OUTPUT / "routing_and_full_flow__before_after.jpg"),
                np.hstack([pictures["degraded_face"], cv2.resize(decoded, pictures["degraded_face"].shape[1::-1])]))
    return row


def scenario_duplicate_delivery(api, pictures):
    from enhancement.tasks import process_enhancement_image

    start_worker("enhance-2", WEB_PYTHON, "celery")
    batch = api.upload(("duplicate.jpg", pictures["portrait_medium"]))
    image = api.wait_preprocessed(batch["id"])[0]
    queued = api.enhance(image.pk)
    # The same task, delivered again at once: both workers may take one each.
    process_enhancement_image.apply_async(args=[queued["id"]], task_id=queued["task_id"])
    wait_for(lambda: result_row(queued["id"]).status in {"completed", "failed"}, 240)
    time.sleep(5)  # let the second delivery finish
    row = result_row(queued["id"])
    routed = deliveries(row.task_id)
    finished_lines = sum(len(re.findall(rf"Task enhancement.process_image\[{row.task_id}\] (succeeded|retry)", worker_log(name)))
                         for name in ("enhance", "enhance-2"))
    files = stored_outputs(row.pk)
    record(
        "duplicate_delivery",
        "two deliveries of one task; the image is enhanced and published once",
        row.status == "completed" and sum(routed.values()) >= 2 and len(files) == 1,
        status=row.status, deliveries=routed, task_outcome_lines=finished_lines, output_files=files,
        claim_attempt=row.claim_attempt,
    )
    stop_worker("enhance-2")


def scenario_database_failure_then_retry(api, pictures):
    """The lightweight pipeline finishes in well under a second, so a worker is held inside it
    (an injected 25 s stage, after the claim) for the connection to be killed mid-task."""
    from django.db import connection

    stop_worker("enhance")
    start_worker("db-victim", WEB_PYTHON, "celery", fault="sleep:25")
    try:
        batch = api.upload(("db-failure.jpg", pictures["portrait_large"]))
        image = api.wait_preprocessed(batch["id"])[0]
        queued = api.enhance(image.pk)
        processing = wait_for(lambda: result_row(queued["id"]).status == "processing", 120, interval=0.1)
        time.sleep(1.0)  # into the pipeline, past the claim
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = current_database() and pid <> pg_backend_pid()"
            )
            terminated = len(cursor.fetchall())
        started = time.time()
        wait_for(lambda: result_row(queued["id"]).status in {"completed", "failed"}, 900)
        seconds_to_complete = round(time.time() - started)
        row = result_row(queued["id"])
        text = worker_log("db-victim")
        retried = len(re.findall(rf"Task enhancement.process_image\[{row.task_id}\] retry", text))
        record(
            "database_failure_then_retry",
            "the worker's database connection is killed mid-task; the task retries, resumes its own claim and completes once",
            bool(processing) and terminated >= 1 and row.status == "completed" and retried >= 1 and row.claim_attempt >= 1
            and len(stored_outputs(row.pk)) == 1 and seconds_to_complete < 180,
            connections_terminated=terminated, retries_logged=retried, status=row.status,
            seconds_from_failure_to_completion=seconds_to_complete,
            claim_attempt=row.claim_attempt, output_files=stored_outputs(row.pk),
        )
    finally:
        if "db-victim" in PROCESSES:
            stop_worker("db-victim")
        start_worker("enhance", WEB_PYTHON, "celery")


def scenario_cancellation(api, pictures):
    from enhancement.models import EnhancementBatchJob

    named = [(f"cancel-{index}.jpg", pictures["portrait_medium"]) for index in range(4)]
    batch = api.upload(*named)
    api.wait_preprocessed(batch["id"])
    job = api.enhance_batch(batch["id"])
    wait_for(lambda: EnhancementBatchJob.objects.get(pk=job["id"]).results.filter(status="processing").exists(), 120, interval=0.1)
    cancel = api.client.post(f"/api/images/batches/{batch['id']}/cancel/")
    final = wait_for(lambda: EnhancementBatchJob.objects.get(pk=job["id"]).status in {"cancelled", "completed", "partial", "failed"}
                     and EnhancementBatchJob.objects.get(pk=job["id"]), 240)
    job_row = EnhancementBatchJob.objects.get(pk=job["id"])
    results = list(job_row.results.order_by("pk"))
    statuses = [row.status for row in results]
    cancelled_with_files = [row.pk for row in results if row.status == "cancelled" and row.enhanced_file]
    record(
        "cancellation_mid_batch",
        "cancel while the first image is processing: the job ends cancelled, unfinished images are cancelled and get no file",
        bool(final) and cancel.status_code == 200 and job_row.status == "cancelled" and "cancelled" in statuses
        and not cancelled_with_files and "processing" not in statuses,
        job_status=job_row.status, result_statuses=statuses, cancelled_results_with_files=cancelled_with_files,
    )


def scenario_worker_killed_then_restarted(api, pictures):
    """Held inside the pipeline by an injected 20 s stage, so the kill lands mid-task."""
    stop_worker("enhance")
    start_worker("victim", WEB_PYTHON, "celery", fault="sleep:20")
    batch = api.upload(("worker-restart.jpg", pictures["portrait_large"]))
    image = api.wait_preprocessed(batch["id"])[0]
    queued = api.enhance(image.pk)
    wait_for(lambda: result_row(queued["id"]).status == "processing", 120, interval=0.1)
    time.sleep(1.0)
    stop_worker("victim", kill=True)
    killed_at = time.time()
    left = result_row(queued["id"])
    log("Enhancement worker killed mid-task (SIGKILL); restarting a normal one")
    start_worker("enhance", WEB_PYTHON, "celery")
    done = wait_for(lambda: result_row(queued["id"]).status in {"completed", "failed"}, 420, interval=1.0)
    row = result_row(queued["id"])
    record(
        "worker_killed_then_restarted",
        "the task is redelivered after the visibility timeout, waits out the dead worker's claim, takes over and completes once",
        bool(done) and left.status == "processing" and row.status == "completed" and len(stored_outputs(row.pk)) == 1,
        status_when_killed=left.status, final_status=row.status, seconds_to_recover=round(time.time() - killed_at),
        deliveries=deliveries(row.task_id), output_files=stored_outputs(row.pk),
        note=f"visibility timeout {ARGUMENTS.visibility_timeout} s, task limit {TIME_LIMIT} s, claim lease {LEASE} s for this run; defaults are 105 min, 60 min and 30 min",
    )


def task_outcomes(name, task_id):
    """What each execution of ``task_id`` on worker ``name`` returned, or that it retried."""
    text = worker_log(name)
    pattern = rf"Task enhancement\.process_\w+\[{re.escape(task_id)}\] (succeeded in [\d.]+s: .*|retry: .*)"
    return [line[:160] for line in re.findall(pattern, text)]


def watch_row(result_id, until, timeout, interval=1.0):
    """Sample a result row until ``until(row)``: statuses, claim tokens and claim times seen."""
    seen = {"statuses": [], "tokens": [], "claimed_at": []}
    deadline = time.time() + timeout
    row = result_row(result_id)
    while time.time() < deadline:
        row = result_row(result_id)
        for key, value in (("statuses", row.status), ("tokens", row.claim_token), ("claimed_at", row.claimed_at)):
            if value and (not seen[key] or seen[key][-1] != value):
                seen[key].append(value)
        if until(row):
            break
        time.sleep(interval)
    return row, seen


def renewal_span(claimed_at_values):
    """Seconds between the earliest and latest claim time seen while one claim was held.

    Not first-to-last: the completing save writes the row as the worker loaded
    it, which puts ``claimed_at`` back to the original claim time.
    """
    return round((max(claimed_at_values) - min(claimed_at_values)).total_seconds()) if len(claimed_at_values) > 1 else 0


def scenario_duplicate_while_original_renews(api, pictures):
    """The original runs one 150 s stage (> two leases) with no checkpoint in it."""
    from enhancement.tasks import MAX_CLAIM_WAITS, process_enhancement_image

    stop_worker("enhance")
    start_worker("slow-original", WEB_PYTHON, "celery", fault="sleep:150")
    try:
        batch = api.upload(("renewal.jpg", pictures["portrait_medium"]))
        image = api.wait_preprocessed(batch["id"])[0]
        queued = api.enhance(image.pk)
        wait_for(lambda: result_row(queued["id"]).status == "processing", 120, interval=0.2)
        start_worker("duplicate", WEB_PYTHON, "celery")
        process_enhancement_image.apply_async(args=[queued["id"]], task_id=queued["task_id"])
        row, seen = watch_row(queued["id"], lambda row: row.status in {"completed", "failed"}, 400)
        time.sleep(5)
        original, duplicate = task_outcomes("slow-original", row.task_id), task_outcomes("duplicate", row.task_id)
        span = renewal_span(seen["claimed_at"])
        record(
            "duplicate_while_original_renews",
            "the original's claim is renewed through a stage longer than the lease; the duplicate waits a bounded "
            "number of times, never takes over, and leaves the result to the original, which publishes once",
            row.status == "completed" and len(seen["tokens"]) == 1 and span > LEASE
            and any("COMPLETED" in line or "'completed'" in line for line in original)
            and sum("retry" in line for line in duplicate) == MAX_CLAIM_WAITS
            and any("in_progress" in line for line in duplicate)
            and len(stored_outputs(row.pk)) == 1,
            status=row.status, claim_tokens_seen=len(seen["tokens"]), claim_renewed_over_seconds=span,
            claim_times_seen=len(set(seen["claimed_at"])),
            lease_seconds=LEASE, original_outcomes=original, duplicate_outcomes=duplicate,
            output_files=stored_outputs(row.pk),
        )
    finally:
        for name in ("slow-original", "duplicate"):
            if name in PROCESSES:
                stop_worker(name)
        start_worker("enhance", WEB_PYTHON, "celery")


def scenario_old_worker_fails_after_takeover(api, pictures):
    """The original is frozen past its lease, another execution takes over, then
    the original wakes up and fails."""
    from enhancement.tasks import process_enhancement_image

    stop_worker("enhance")
    start_worker("old", WEB_PYTHON, "celery", fault="sleep_then_fail:20")
    try:
        batch = api.upload(("takeover.jpg", pictures["portrait_medium"]))
        image = api.wait_preprocessed(batch["id"])[0]
        queued = api.enhance(image.pk)
        wait_for(lambda: result_row(queued["id"]).status == "processing", 120, interval=0.2)
        time.sleep(3)
        first_token = result_row(queued["id"]).claim_token
        PROCESSES["old"][0].send_signal(signal.SIGSTOP)
        log("old worker frozen (SIGSTOP) holding the claim")
        start_worker("new", WEB_PYTHON, "celery", fault="sleep:45")
        process_enhancement_image.apply_async(args=[queued["id"]], task_id=queued["task_id"])
        taken = wait_for(lambda: result_row(queued["id"]).claim_token not in {"", first_token}, 240)
        PROCESSES["old"][0].send_signal(signal.SIGCONT)
        log("old worker resumed (SIGCONT) after the takeover; it will now fail")
        row, seen = watch_row(queued["id"], lambda row: row.status in {"completed", "failed"}, 300, interval=0.5)
        time.sleep(5)
        old_log = worker_log("old")
        record(
            "old_worker_fails_after_takeover",
            "an old worker whose claim lapsed fails after another execution took the result over; the failure "
            "does not touch the new owner's row, which completes and is published once",
            bool(taken) and "injected failure" in old_log and "failed" not in seen["statuses"]
            and row.status == "completed" and len(stored_outputs(row.pk)) == 1,
            taken_over=bool(taken), old_worker_failed="injected failure" in old_log,
            statuses_seen=seen["statuses"], final_status=row.status,
            old_outcomes=task_outcomes("old", row.task_id), new_outcomes=task_outcomes("new", row.task_id),
            output_files=stored_outputs(row.pk),
        )
    finally:
        for name in ("old", "new"):
            if name in PROCESSES:
                PROCESSES[name][0].send_signal(signal.SIGCONT)
                stop_worker(name)
        start_worker("enhance", WEB_PYTHON, "celery")


def scenario_worker_killed_mid_batch(api, pictures):
    """Killed shortly before the visibility timeout, so the batch is redelivered
    while the dead worker's claim on the current image is still live."""
    from enhancement.models import EnhancementBatchJob

    stop_worker("enhance")
    start_worker("batch-a", WEB_PYTHON, "celery", fault="sleep:60")
    try:
        named = [(f"batch-death-{index}.jpg", pictures["portrait_medium"]) for index in range(6)]
        batch = api.upload(*named)
        api.wait_preprocessed(batch["id"])
        job = api.enhance_batch(batch["id"])
        dispatched = time.time()
        rows = lambda: list(EnhancementBatchJob.objects.get(pk=job["id"]).results.order_by("pk"))
        # Kill late enough that the redelivery (visibility timeout after the
        # first delivery, plus Redis's ~10 s restore cycle) arrives while the
        # claim the dead worker last renewed is still within its lease.
        start_worker("batch-b", WEB_PYTHON, "celery")  # idle until the batch is redelivered
        kill_after = ARGUMENTS.visibility_timeout - 15
        wait_for(lambda: time.time() - dispatched >= kill_after, kill_after + 10, interval=1.0)
        interrupted = [row for row in rows() if row.status == "processing"]
        renewed = 0
        if interrupted:
            renewed = round((interrupted[0].claimed_at - interrupted[0].started_at).total_seconds())
        stop_worker("batch-a", kill=True)
        killed_at = time.time()
        log(f"batch worker killed (SIGKILL) {round(killed_at - dispatched)} s after dispatch, "
            f"mid-image {[row.pk for row in interrupted]}")
        final = wait_for(lambda: EnhancementBatchJob.objects.get(pk=job["id"]).status in {"completed", "partial", "failed"}
                         and EnhancementBatchJob.objects.get(pk=job["id"]), 600, interval=2.0)
        job_row = EnhancementBatchJob.objects.get(pk=job["id"])
        statuses = [row.status for row in rows()]
        outcomes = task_outcomes("batch-b", job_row.task_id)
        record(
            "worker_killed_mid_batch",
            "the batch is redelivered while the dead worker's claim is live; it comes back for that image once the "
            "claim lapses instead of skipping it; every image completes once and the job counts are right",
            bool(final) and len(interrupted) == 1 and job_row.status == "completed" and statuses == ["completed"] * 6
            and job_row.successful_images == 6 and any("retry" in line for line in outcomes)
            and all(len(stored_outputs(row.pk)) == 1 for row in rows()),
            killed_seconds_after_dispatch=round(killed_at - dispatched), interrupted=[row.pk for row in interrupted],
            interrupted_claim_renewed_for_seconds=renewed, job_status=job_row.status, result_statuses=statuses,
            counts=[job_row.processed_images, job_row.successful_images, job_row.failed_images],
            seconds_to_finish_after_kill=round(time.time() - killed_at), batch_outcomes_after_redelivery=outcomes,
            output_files=[len(stored_outputs(row.pk)) for row in rows()],
        )
    finally:
        for name in ("batch-a", "batch-b"):
            if name in PROCESSES:
                stop_worker(name)
        start_worker("enhance", WEB_PYTHON, "celery")


def main():
    start_infrastructure()
    try:
        setup_django()
        start_worker("enhance", WEB_PYTHON, "celery")
        api = Api()
        pictures = images(Path(ARGUMENTS.portrait).expanduser())
        chosen = {name.strip() for name in ARGUMENTS.scenarios.split(",") if name.strip()}
        for scenario in (
            scenario_routing_and_full_flow, scenario_duplicate_delivery, scenario_database_failure_then_retry,
            scenario_cancellation, scenario_worker_killed_then_restarted,
            scenario_duplicate_while_original_renews, scenario_old_worker_fails_after_takeover,
            scenario_worker_killed_mid_batch,
        ):
            if chosen and scenario.__name__.removeprefix("scenario_") not in chosen:
                continue
            try:
                scenario(api, pictures)
            except Exception as error:  # one broken scenario must not hide the others
                record(scenario.__name__.removeprefix("scenario_"), "", False, error=f"{type(error).__name__}: {error}"[:500])
    finally:
        (OUTPUT / "results.json").write_text(json.dumps(RESULTS, indent=2, default=str))
        lines = [f"# Queue integration `{ARGUMENTS.name}`", "",
                 "Real Redis, real Celery workers (web environment, default queue), "
                 f"a throwaway PostgreSQL database and the real pipeline. Visibility timeout {ARGUMENTS.visibility_timeout} s, "
                 f"task time limit {TIME_LIMIT} s and claim lease {LEASE} s for this run.", "", "| Scenario | Expected | Result |", "|---|---|---|"]
        lines += [f"| {row['scenario']} | {row['expects']} | {'**PASS**' if row['passed'] else '**FAIL**'} |" for row in RESULTS]
        (OUTPUT / "report.md").write_text("\n".join(lines) + "\n")
        stop_infrastructure()
        log(f"wrote {OUTPUT}")
    return 0 if RESULTS and all(row["passed"] for row in RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
