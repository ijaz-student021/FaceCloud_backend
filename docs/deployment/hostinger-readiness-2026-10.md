# Hostinger deployment readiness (assessment of 2026-10-15)

**Status: not validated on Hostinger.** No Hostinger VPS, no NVIDIA GPU and no AWS account were available. Everything numeric below was
measured on one Apple M1 laptop (8 cores, 8 GiB, PyTorch CPU device, one process), and **does not transfer** to a VPS: a vCPU is
usually slower than an M1 performance core, a VPS has no Apple GPU, and the memory and noisy neighbours differ. The numbers are for sizing the
*shape* of the deployment and for knowing what to measure on the real machine. Re-run `scripts/measure_inference.py` there before committing to a plan.

## Measured on this CPU (uncontended except where noted)

`python scripts/measure_inference.py --images <photos> --device cpu` (each model in a fresh process; warm = after the checkpoint is loaded;
peak = peak resident memory of the process, whose Django + OpenCV + PyTorch baseline is about 135 MB). A few runs overlapped with light
unrelated work on the machine, so treat each figure as +-20 %.

| Model | Image | Megapixels | Warm seconds | Peak memory |
|---|---|---|---|---|
| Retinexformer, 1024-px tiles (the old CPU behaviour) | 480x640 | 0.3 | 5.0-5.7 | 1.6 GB |
| | 960x1280 | 1.2 | 56-70 | 2.7 GB |
| | 1536x2048 | 3.1 | 181-218 | 2.7 GB |
| **Retinexformer, 512-px tiles (now the CPU default)** | 960x1280 | 1.2 | **24** | **1.7 GB** |
| Retinexformer, 256-px tiles (not chosen: up to 0.9 dB lower quality) | 960x1280 | 1.2 | 24 | 0.8 GB |
| NAFNet-SIDD (denoise) | 480x640 / 960x1280 / 1536x2048 | 0.3 / 1.2 / 3.1 | 3.9-4.4 / 18-19 / 36-38 | 1.1-1.4 GB |
| NAFNet-GoPro (deblur) | 480x640 / 960x1280 / 1536x2048 | 0.3 / 1.2 / 3.1 | 3.1-3.8 / 13.7-14.2 / 29-30 | 0.9-1.0 GB |
| Real-ESRGAN, output x2 (computes the native x4) | 480x640 | 0.3 | 275-289 | 2.6 GB |
| | larger | | not measured: 0.3 MP already needs ~5 minutes; larger sizes would take tens of minutes to hours | |

Cost of one adaptive run on a CPU = the sum of the operations the photograph needs, roughly linear in megapixels for NAFNet (about 11-15 s per MP here)
and Retinexformer (about 20 s per MP at 512-px tiles); Real-ESRGAN is not practical on a CPU at all. For a 12 MP phone photograph that is
roughly 4 minutes for a lift and 3 minutes for a denoise **on this M1**; the 180 s time budget would cut it.

What this means (conclusions that follow from the measurements, not from assumption):

1. **CPU-only inference is feasible for the lift, denoise and deblur at modest resolution, and not for Real-ESRGAN.** On a CPU worker set
   `HD_RESTORATION_ENABLED=False` (the router then records "No super-resolution model is switched on") unless a GPU is available.
2. **Limit the working resolution on CPU workers.** `ENHANCEMENT_MAX_INPUT_PIXELS` (default 24 MP) reduces larger sources first; a CPU
   worker wants about 2-4 MP, and `ADAPTIVE_TIME_BUDGET_SECONDS` sized to a few minutes. The budget is now enforced between tiles, not only between
   operations: a photograph that cannot be finished in time returns the original with "The enhancement ran out of time".
3. **Model memory is 1-3 GB per resident model** and the engine keeps one model at a time on machines under 12 GiB (`ENHANCEMENT_MODEL_RESIDENCY`).
   Plan **one inference process per worker (`--concurrency=1`) and at least 8 GiB** for a CPU worker; do not co-locate it with the API on a small VPS.
4. **Throughput on this M1** follows from the per-model times: a photograph that needs a lift and a denoise costs about 100 s at 3 MP and about 7 minutes at 12 MP,
   i.e. roughly 35 and 8 photographs an hour per worker. A clean photograph costs a second or two. A VPS will differ; this is arithmetic on the table above, not a measured queue.

## What the deployment should look like

```
Hostinger VPS (API)                                Inference worker(s)                      Storage
  gunicorn + Django REST        <-- broker -->     celery -A config worker                 private S3 bucket
  PostgreSQL, Redis (broker)                         -Q inference --concurrency=1           (STORAGE_BACKEND=s3,
  light Celery worker (-Q celery)                    ENHANCEMENT_QUEUE_NAME=inference         client-side AES-256-GCM)
  no model weights needed* (see previews)            PyTorch, model weights, 8 GiB+
```

* **Set `ENHANCEMENT_QUEUE_NAME=inference` in the environment of both the API and the worker** (the sender decides the queue), start the worker with `-Q inference`, and keep a
  second worker on the default queue for mail and cleanup. Nothing changes with the setting empty.
* **S3 is a prerequisite for a split**: the API and the worker are different machines, so local media files cannot be shared.
* **Django does not run inference** for jobs (Celery eager mode is refused by the production guard), with **one exception**: `POST .../previews/` renders in the request
  and, with the model sliders, loads PyTorch models inside the API process. Either give the API process the memory (and accept its first-request model load), or run
  those API workers with the model flags off (previews then use the classical fallbacks and will differ from the saved render), or put previews on their own gunicorn
  upstream with one worker. Changing this to a queued job is an API change and was not done.
* **GPU**: the worker is an ordinary Celery process, so a GPU machine (any provider) can join the same broker and bucket as a second pool. `RETINEXFORMER_DEVICE`/`HD_RESTORATION_DEVICE=auto` selects CUDA when present; **CUDA code
  paths have not been run** and nothing here claims CUDA support or speed. No always-on GPU is needed for anything but inference.
* **Models and licences**: the production guard refuses CodeFormer, GFPGAN, ArcFace, RetinaFace and (under a learned policy) Zero-DCE unless `NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True` (see `docs/model-licenses/commercial-inventory-2026-10.md`).

## Capacity planning recipe (use measured numbers, not these)

1. On the target machine run `scripts/measure_inference.py --device cpu` (and `--device cuda` on a GPU box) at the sizes you will cap to.
2. Per-photo seconds = sum of the models the typical photograph needs (see `manage.py adaptive_report` for the real mix of operations once in use).
3. Workers needed = arrival rate (photos/s) x per-photo seconds, rounded up, with the p95 latency (not the mean) driving the queue-wait target.
4. Memory = workers x (peak memory of the largest resident model + 0.3 GB); keep 30 % headroom; an out-of-memory kill on a CPU (exit 137) is silent to Python, so size, do not rely on the adapters' OOM retry (it catches framework errors, not the kernel's).
5. Transfer: every job reads the preprocessed image and writes one output through the API host or the worker; S3 egress to a non-AWS host is billed per GB. Estimate it from your photo sizes and check the current AWS and Hostinger prices; none are assumed here.

## AWS S3 readiness (real AWS NOT validated)

Implemented and tested against a simulated S3 (moto, 23 tests): client-side AES-256-GCM with a key ring, SSE, private bucket by construction (`scripts/aws_provision_bucket.py`),
least-privilege IAM policy, no signed URLs for ciphertext (downloads stream through the authenticated API), ranged header reads, `check_storage` refusing root credentials and
public buckets, `copy_media_to_s3`. Reviewed for key and nonce reuse: a fresh random 64-bit prefix per file plus a chunk counter under one key, the header, chunk index and
a final-chunk flag authenticated (truncation and reordering are detected); the associated data does not include the object path (finding L1 in the audit). **Pending, needs an authorised non-root IAM user and a test bucket:** `python scripts/aws_provision_bucket.py ...`,
then `python manage.py check_storage` (round trip, encryption, public-access block, versioning), then a cross-user access attempt, a large-object upload, and an interrupted-upload recovery. Never use root keys; the
tools refuse them.

## Troubleshooting

| Symptom | Likely cause | Check / action |
|---|---|---|
| Job stays `processing` | worker died | `python manage.py reap_stuck_jobs` (on a development machine after a restart: `--older-than-seconds 120`, because the default waits over an hour and only `celery beat` runs it on a schedule); claims lapse after `ENHANCEMENT_CLAIM_LEASE_SECONDS`; the user can retry |
| Job stays `queued` | no worker on its queue | `celery -A config inspect active_queues`; is `ENHANCEMENT_QUEUE_NAME` the same on API and worker? |
| Worker exits with 137 / vanishes | kernel out-of-memory | lower `ENHANCEMENT_MAX_INPUT_PIXELS`, `--concurrency=1`, tile sizes; more RAM |
| Many `original_preserved` with "ran out of time" | CPU too slow for the resolution | measure, cap the pixels, raise `ADAPTIVE_TIME_BUDGET_SECONDS`, or add a GPU worker |
| "enhancement models were not available" | checkpoint missing/corrupt, PyTorch missing | `python scripts/model_manifest.py verify`; `GET /api/system/health/` (admin) |
| Storage errors | credentials, policy, clock | `python manage.py check_storage` |
| Which operations are being rejected, and why | | `python manage.py adaptive_report --days 7` and the `adaptive_enhancement {...}` log line (outcome, per-stage seconds, models, rejection reasons) |
| Unexpected outcome on one photograph | | reproduce with `ADAPTIVE_DEBUG_ARTIFACTS=True` on a development machine only (stores copies of the photograph; refused in production), then `cleanup_enhancement_debug` |
