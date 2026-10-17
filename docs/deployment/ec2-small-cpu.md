# Running on a small EC2 instance without a GPU

**Short version.** The full model set is not for this instance (Retinexformer 20 s per megapixel, Real-ESRGAN about 5 minutes for a 0.3 MP photo, 1-3 GB per model, on a fast laptop CPU).
The project can instead run its adaptive engine on **light engines only**, which need neither a GPU nor PyTorch:
low light through Zero-DCE (OpenCV, 0.3 MB), noise through OpenCV non-local means, and no deblurring, super-resolution or face restoration. Settings: `ec2-small-cpu.env.example`.
Nothing below was measured on EC2; it was measured on one Apple M1 CPU (8 cores) with multi-threaded OpenCV. An EC2 vCPU is slower and a 2-vCPU instance has a quarter of the cores, so **re-measure on the instance**
(`python scripts/benchmark_adaptive.py --images <photos> --out <dir> --profile cpu_small`).

## What the light profile costs (measured, M1 CPU, 8 threads, no PyTorch loaded)

| Photograph | Low-light lift | Denoise | Peak memory of the whole process |
|---|---|---|---|
| 0.3 MP (480x640) | 0.8 s | 0.9 s | 0.5 GB |
| 1.2 MP | 2.2 s | 1.9 s | 1.2 GB |
| 3.1 MP | 3.5 s | 3.3 s | 1.4 GB |
| 6.75 MP (2250x3000) | 7.6 s | 7.6 s | 1.7 GB |
| clean photograph | about 0.1 s (analysis only, no model) | | |

Compare the full model set on the same CPU: Retinexformer 24 s at 1.2 MP, NAFNet-SIDD 18 s, Real-ESRGAN minutes. Zero-DCE estimates its curve at 1024 px at most (`ZERO_DCE_MAX_INFERENCE_EDGE`) and applies it at full resolution,
which is why its time grows slowly with size.

## What you give up: the same photographs, both profiles (3 photographs, 640 px, synthetic degradations, against the clean photograph)

| Category | Light profile | Full model set |
|---|---|---|
| Dark | PSNR 9.6 -> **18.7**, SSIM 0.80, LPIPS 0.35, 0.7 s | PSNR 9.6 -> 16.5, SSIM 0.79, LPIPS **0.27**, 3.9 s |
| Noisy | PSNR 23.3 -> 26.0, SSIM 0.85, LPIPS 0.40, 0.9 s | PSNR 23.3 -> 26.5, SSIM 0.84, LPIPS 0.42, 8.0 s |
| Motion blur | **unchanged** (no deblurring model): PSNR 27.5 | PSNR 27.5 -> 29.3 (1 of 3 improved), 10.6 s |
| Defocus, low resolution, face damage | unchanged | small or no change |
| Clean | unchanged, 0.1 s | unchanged, 0.2 s |

Reading it honestly: for the two defects that matter most on a phone (dark and noisy photographs) the light profile is within a fraction of a dB of the full set on PSNR and about 5-9 times faster; on the perceptual metric (LPIPS) the
full set is better for dark photographs (0.27 against 0.35). It cannot deblur, enlarge or restore faces. When a defect needs a model that is not running, the result says so
(for example "The original photograph was kept: it has motion blur that this server's enabled models cannot correct.") and returns the original; it never claims to have fixed it.
Three photographs of one person is a demonstration, not a proof.

**Zero-DCE matters.** With `ZERO_DCE_ENABLED=False` the low-light engine falls to a plain OpenCV gamma: mean PSNR 13.2 against 18.7 on the same three dark photographs, with results marked partially enhanced. Zero-DCE is a
**non-commercial** model (README: CC BY-NC 4.0; the owner's approval is not on file). Production start-up therefore refuses it unless `NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True`: that is a licence decision for the owner, not a technical switch.
For a commercial service without it, expect weaker lifts until a permissively licensed light model is evaluated.

## Sizing (from the measured peaks; not a promise)

* **Instance.** The engine alone peaked at 1.7 GB at 6.75 MP. Add the operating system, gunicorn, one Celery worker and Redis; add PostgreSQL unless the database is managed (RDS). A 2 GiB instance is too small;
  **4 GiB (2 vCPU) is the practical minimum** with the database off the box and a swap file as a safety margin; 8 GiB is comfortable. The Django, Redis and database footprints were not measured here.
* **CPU credits.** Burstable instance types throttle under sustained load once their credits run out; a steady queue of photographs is a sustained load, so check the instance type's behaviour before trusting a burstable one.
* **One job at a time**: `--concurrency=1` on a worker consuming only `inference`. `ENHANCEMENT_MAX_INPUT_PIXELS=8000000` bounds the memory of one job (peak memory grows with pixels).
* **Throughput**: on this CPU about 3-8 s per photograph at 3-7 MP, so hundreds of photographs an hour per worker; on a 2-vCPU instance expect several times slower. Measure it.
* **Previews** render inside the API process. With the light profile they use the classical engines only (no PyTorch is loaded), so they fit.
* **Storage.** Use the instance's **IAM role** (instance profile) with the least-privilege policy printed by `scripts/aws_provision_bucket.py`; do not put access keys on the instance. The image bytes stay encrypted client-side. Real S3 has not been validated: run `python manage.py check_storage` on the instance first.

## Deploy checklist

1. Copy the lines from `ec2-small-cpu.env.example` into `.env`; install `requirements.txt` only (PyTorch is not needed).
2. Put the three small ONNX files (`zero_dce_epoch99.onnx`, `yunet_s_dynamic.onnx`, `face_recognition_sface_2021dec.onnx`, a few tens of MB in total; not committed to Git) under `.runtime/models/` and run `python scripts/model_manifest.py verify` to check them against the pinned checksums in `docs/gpu/model_manifest.json`.
3. `python manage.py check_storage`, `python manage.py migrate` (on the new instance's own database, never an existing one), then start gunicorn and the two Celery workers above.
4. Run `scripts/benchmark_adaptive.py --profile cpu_small` on the instance with photographs at the sizes you will accept, and keep the output.
5. Watch `python manage.py adaptive_report --days 1` (outcome rates, P50/P95 latency, rejection reasons) and the `adaptive_enhancement` log line.
