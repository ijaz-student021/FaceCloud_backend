# What each kind of evidence covers (and what it does not)

A green test run and a measured benchmark are different kinds of evidence. This page says which is which, so a claim in a report can be
traced to the right kind. Counts are from 2026-10-15 on this branch.

| Kind | What it exercises | Where | Result | What it cannot show |
|---|---|---|---|---|
| **Mocked / unit** | Decisions, gates, routing, job claim/retry/ownership, API contract, with stand-in model runners and stand-in identity | `manage.py test` (1,9xx tests; see the final report for the exact count) | passing, 10 skipped | That a photograph looks better; that a real model behaves |
| **Real-tensor** | PyTorch tiling, memory fallback, deadline, on this machine's device, with stand-in networks | `enhancement/test_runtime.py::RealTorchTests` | passing | A real checkpoint's behaviour |
| **Real-model** | The real Retinexformer / NAFNet / Real-ESRGAN / CodeFormer checkpoints on real photographs | `enhancement/test_adaptive_real_models.py` (needs checkpoints, PyTorch and `FACECLOUD_SAMPLE_PHOTOS`; skips otherwise) and the scripts below | see the final report | Photographs other than the 16 used; other devices |
| **Real-model benchmark** | Whole-engine outcomes against clean references (PSNR, SSIM, LPIPS, identity similarity) on synthetic degradations | `scripts/benchmark_adaptive.py`, `scripts/verify_adaptive_api.py`, `scripts/evaluate_lowlight_colour.py`, `scripts/evaluate_denoising.py`, `scripts/evaluate_blur_routing.py` | `docs/evaluation/2026-10-15-*.md` | Real noise, real blur, real people's judgement |
| **Real HTTP end to end** | Upload -> enhance -> poll -> download through the real endpoints with the real models, throwaway database, eager Celery | `scripts/verify_adaptive_api.py` | all checks passed in the earlier run (6 cases) | A real broker, a killed worker, concurrent users |
| **Simulated storage** | Encrypted S3 backend against moto | `config/test_s3_storage.py` (23 tests) | passing | Real AWS: permissions, bucket policy, latency, cost |
| **Real storage** | The same against a real bucket | `manage.py check_storage` | **NOT RUN**: no authorised, non-root credentials | everything about real AWS |
| **GPU / CUDA** | CUDA-specific code and speed | n/a | **NOT RUN**: no NVIDIA GPU. Apple MPS was used locally; it is not CUDA | any claim of CUDA support or speed |
| **CPU sizing** | Latency and peak memory per model and image size | `scripts/measure_inference.py` | `docs/deployment/hostinger-readiness-2026-10.md` (Apple M1 CPU only) | A Hostinger VPS: it must be re-run there |
| **Human evaluation** | Whether people prefer the output | n/a | **NOT DONE**: the author looked at contact sheets | anything about perceived quality beyond one person's look |
| **Identity calibration** | Genuine/impostor distributions, threshold at a stated false-match rate | `scripts/calibrate_identity.py` | **smoke-tested only**: no consented multi-photo identity dataset | any threshold's validity |

Commands: `python manage.py test`; `uvx ruff check .`; `python manage.py makemigrations --check --dry-run`; `python manage.py spectacular` (no warnings);
`bandit -r ... -ll`; the CI workflow `.github/workflows/ci.yml` runs the same set plus `pip-audit` and a production-start check.
