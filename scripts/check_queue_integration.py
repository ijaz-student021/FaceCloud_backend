"""Exercise the real Redis/Celery flow end to end, with real model checkpoints.

Nothing here runs eagerly or in-process: every task goes through Redis to a
separately started Celery worker. The script provides its own isolated
infrastructure and removes it afterwards:

* a dedicated Redis server on its own port, with no persistence;
* a new, empty PostgreSQL database (refused if one by that name exists);
* a temporary media folder.

Two kinds of worker are started, as a deployment would run them: the web
environment's worker on the default queue (preprocessing, thumbnails), and the
GFPGAN worker environment on the ``gfpgan`` queue, which receives every
enhancement task when ``GFPGAN_QUEUE_ENABLED`` is on.

    venv/bin/python scripts/check_queue_integration.py --portrait ~/Downloads/portrait.jpg

Scenarios: routing and a real GFPGAN enhancement through upload, preprocessing,
processing, storage, progress and download; duplicate delivery to two workers
at once; a real database connection failure mid-task, then retry; cancellation
mid-batch; and a worker killed mid-task, then restarted. To keep the last one
short, the broker visibility timeout, task time limit and claim lease are
lowered for the run (90 s, 80 s and 60 s); the report says so.

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
WORKER_PYTHON = ROOT / ".runtime" / "venvs" / "gfpgan" / "bin" / "python"


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--portrait", required=True)
    parser.add_argument("--name", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--redis-port", type=int, default=6391)
    parser.add_argument("--database", default="facecloud_queue_itest")
    parser.add_argument("--db-host", default="/tmp")
    parser.add_argument("--db-user", default=os.environ.get("USER", "postgres"))
    parser.add_argument("--keep", action="store_true", help="Leave the database and Redis running afterwards.")
    parser.add_argument("--scenarios", default="", help="Comma-separated subset to run (default: all).")
    parser.add_argument("--visibility-timeout", type=int, default=90,
                        help="Broker visibility timeout for the run, seconds (production default 4500). It must "
                             "exceed the task time limit (80 s here) and the claim lease (60 s here).")
    return parser.parse_args()


ARGUMENTS = parse_arguments()
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
    "DB_PORT": "5432",
    "MEDIA_ROOT": str(MEDIA),
    "CELERY_BROKER_URL": f"redis://127.0.0.1:{ARGUMENTS.redis_port}/0",
    "CELERY_RESULT_BACKEND": f"redis://127.0.0.1:{ARGUMENTS.redis_port}/1",
    "CELERY_TASK_ALWAYS_EAGER": "False",
    "GFPGAN_ENABLED": "True",
    "GFPGAN_QUEUE_ENABLED": "True",
    "IDENTITY_GUARD_ENABLED": "True",
    "THUMBNAIL_PREGENERATE": "False",
    "NOTIFICATION_EMAILS_ENABLED": "False",
    # Lowered so the worker-restart scenario finishes in minutes, not an hour.
    "CELERY_VISIBILITY_TIMEOUT": str(ARGUMENTS.visibility_timeout),
    "ENHANCEMENT_CLAIM_LEASE_SECONDS": "60",
    "CELERY_TASK_TIME_LIMIT": "80",
    "CELERY_TASK_SOFT_TIME_LIMIT": "70",
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
    existing = subprocess.run(
        ["psql", "-h", ARGUMENTS.db_host, "-U", ARGUMENTS.db_user, "-d", "postgres", "-Atc",
         f"select 1 from pg_database where datname = '{ARGUMENTS.database}'"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if existing:
        raise SystemExit(f"Database {ARGUMENTS.database} already exists; refusing to touch it.")
    subprocess.run(["createdb", "-h", ARGUMENTS.db_host, "-U", ARGUMENTS.db_user, ARGUMENTS.database], check=True)
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
        subprocess.run(["dropdb", "-h", ARGUMENTS.db_host, "-U", ARGUMENTS.db_user, "--if-exists", "--force",
                        ARGUMENTS.database], capture_output=True)
        shutil.rmtree(WORK, ignore_errors=True)


def start_worker(name, python, queue):
    logfile = OUTPUT / f"worker-{name}.log"
    handle = open(logfile, "a")
    handle.write(f"\n===== started {time.strftime('%H:%M:%S')} =====\n")
    handle.flush()
    process = subprocess.Popen(
        [str(python), "-m", "celery", "-A", "config", "worker", "--pool=solo", "--loglevel=info",
         "-Q", queue, "-n", f"{name}@%h", "--without-gossip", "--without-mingle"],
        cwd=ROOT, env=ENVIRONMENT, stdout=handle, stderr=subprocess.STDOUT,
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
    restoration = parameters.get("face_restoration", {})
    stage = [entry for entry in parameters.get("stage_reports", []) if entry["stage"] == "face_restoration"]
    routed = deliveries(row.task_id)
    record(
        "routing_and_full_flow",
        "preprocessing on the web worker, enhancement on the GFPGAN worker; real GFPGAN applied with identity verified; "
        "file stored once; download returns the stored image; progress reports completion",
        bool(finished) and row.status == "completed" and routed == {"gfpgan": 1}
        and restoration.get("applied") and parameters.get("identity_status") == "verified"
        and download.status_code == 200 and body == stored and decoded is not None
        and list(decoded.shape[:2]) == [row.output_height, row.output_width],
        status=row.status, enhancement_task_delivered_to=routed,
        preprocessing_task_delivered_to=deliveries(preprocess_task) if preprocess_task else "n/a",
        gfpgan_applied=restoration.get("applied"), gfpgan_device=(stage[0].get("device") if stage else None),
        identity_status=parameters.get("identity_status"), similarity=parameters.get("identity_guard", {}).get("similarity"),
        download_status=download.status_code, download_matches_stored_file=body == stored,
        output=[row.output_width, row.output_height], processing_ms=row.processing_duration_ms,
        progress={key: progress.get(key) for key in ("status", "progress_percentage", "completed", "total") if key in progress},
    )
    cv2.imwrite(str(OUTPUT / "routing_and_full_flow__before_after.jpg"),
                np.hstack([pictures["degraded_face"], cv2.resize(decoded, pictures["degraded_face"].shape[1::-1])]))
    return row


def scenario_duplicate_delivery(api, pictures):
    from enhancement.tasks import process_enhancement_image

    start_worker("gfpgan-2", WORKER_PYTHON, "gfpgan")
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
                         for name in ("gfpgan", "gfpgan-2"))
    files = stored_outputs(row.pk)
    record(
        "duplicate_delivery",
        "two deliveries of one task; the image is enhanced and published once",
        row.status == "completed" and sum(routed.values()) >= 2 and len(files) == 1,
        status=row.status, deliveries=routed, task_outcome_lines=finished_lines, output_files=files,
        claim_attempt=row.claim_attempt,
    )
    stop_worker("gfpgan-2")


def scenario_database_failure_then_retry(api, pictures):
    from django.db import connection

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
    text = worker_log("gfpgan")
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
    batch = api.upload(("worker-restart.jpg", pictures["portrait_large"]))
    image = api.wait_preprocessed(batch["id"])[0]
    queued = api.enhance(image.pk)
    wait_for(lambda: result_row(queued["id"]).status == "processing", 120, interval=0.1)
    time.sleep(1.0)
    stop_worker("gfpgan", kill=True)
    killed_at = time.time()
    left = result_row(queued["id"])
    log("GFPGAN worker killed mid-task (SIGKILL); restarting it")
    start_worker("gfpgan", WORKER_PYTHON, "gfpgan")
    done = wait_for(lambda: result_row(queued["id"]).status in {"completed", "failed"}, 420, interval=1.0)
    row = result_row(queued["id"])
    record(
        "worker_killed_then_restarted",
        "the task is redelivered after the visibility timeout, waits out the dead worker's claim, takes over and completes once",
        bool(done) and left.status == "processing" and row.status == "completed" and len(stored_outputs(row.pk)) == 1,
        status_when_killed=left.status, final_status=row.status, seconds_to_recover=round(time.time() - killed_at),
        deliveries=deliveries(row.task_id), output_files=stored_outputs(row.pk),
        note=f"visibility timeout {ARGUMENTS.visibility_timeout} s, task limit 80 s, claim lease 60 s for this run; defaults are 75 min, 60 min and 30 min",
    )


def main():
    start_infrastructure()
    try:
        setup_django()
        start_worker("web", WEB_PYTHON, "celery")
        start_worker("gfpgan", WORKER_PYTHON, "gfpgan")
        api = Api()
        pictures = images(Path(ARGUMENTS.portrait).expanduser())
        chosen = {name.strip() for name in ARGUMENTS.scenarios.split(",") if name.strip()}
        for scenario in (
            scenario_routing_and_full_flow, scenario_duplicate_delivery, scenario_database_failure_then_retry,
            scenario_cancellation, scenario_worker_killed_then_restarted,
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
                 "Real Redis, real Celery workers (web environment on `celery`, GFPGAN environment on `gfpgan`), "
                 f"a throwaway PostgreSQL database and real checkpoints. Visibility timeout {ARGUMENTS.visibility_timeout} s, "
                 "task time limit 80 s and claim lease 60 s for this run.", "", "| Scenario | Expected | Result |", "|---|---|---|"]
        lines += [f"| {row['scenario']} | {row['expects']} | {'**PASS**' if row['passed'] else '**FAIL**'} |" for row in RESULTS]
        (OUTPUT / "report.md").write_text("\n".join(lines) + "\n")
        stop_infrastructure()
        log(f"wrote {OUTPUT}")
    return 0 if RESULTS and all(row["passed"] for row in RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
