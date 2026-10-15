# Model, queue and visual validation, 2026-10-14

Evaluation record only. The code changes it led to are in their own commits
(`58ee365` Zero-DCE provenance, `b23524b` broker timeout, `9d45d29` diverged
tiles and deblur routing). Images and raw results are under `.runtime/` (not in
Git); paths are given so the run can be inspected and repeated.

**Hardware:** Apple M1, macOS. PyTorch 2.14 on MPS; OpenCV DNN models on the CPU.
**CUDA was not tested.** Nothing here says anything about NVIDIA behaviour.

## 1. Checkpoints

All 13 manifest files except the private curve model are installed and match
the manifest (`venv/bin/python scripts/model_manifest.py verify`).

| Component | Checksum | Device | Direct inference | Pipeline stage | Status |
|---|---|---|---|---|---|
| Zero-DCE (official Epoch99, ONNX) | match (after re-export, see `docs/model-licenses/zero-dce-provenance.md`) | CPU | ok, 0.6 s | accepted | Validated |
| EDSR x2 | match | CPU | ok, 13 s at 360 px | accepted | Validated |
| NAFNet GoPro width64 | match | MPS | ok, 7 s at 900 px | deblur kept | Runs; off (see 3) |
| Real-ESRGAN x4plus | match | MPS | ok, 11 s at 640 px | accepted | Runs; off (see 3) |
| PureVision 1x | match | MPS | ok, 3.5 s at 900 px | accepted | Runs; off (licence) |
| PureScale 2x | match | MPS | ok, 8 s at 640 px | accepted | Runs; off (licence) |
| GFPGAN v1.4 + RetinaFace + ParseNet | match (3 files) | MPS | ok, 1 face | accepted, identity verified | Runs; optional, off |
| Colorization SIGGRAPH17 | match | MPS | ok | accepted | Runs; off (quality) |
| YuNet | match | CPU | 1 face, 5 landmarks | used by face checks | Validated |
| SFace | match | CPU | 128-d, self-similarity 1.0 | verified, 0.98 | Validated |
| Private curve model `facecloud_curve_best_opencv.onnx` | **missing** | – | – | – | Blocked: not on this machine; optional |

Source: `.runtime/model_validation/2026-10-14-final/` (`report.md`,
`results.json`, `<component>__*.jpg`). All twelve combination and failure cases
passed in the same run (exposure + denoise; deblur + exactly one upscaler;
GFPGAN with real identity, refused without consent, flagged under AI Max;
colorization on grayscale only; missing checkpoint; checksum mismatch;
out-of-memory retried at a smaller tile; repeated out-of-memory; inference
error; cancellation before a model stage). Out-of-memory and inference errors
were injected around the real models; a genuine one was not provoked.

## 2. Redis and Celery

`scripts/check_queue_integration.py`, run `2026-10-14-final`: its own Redis
(port 6391, no persistence), its own PostgreSQL database (created and dropped),
temporary media, real Celery workers: the web environment on `celery`, the
GFPGAN environment on `gfpgan` (two of them for the duplicate case). Visibility
timeout 90 s, task limit 80 s, claim lease 60 s for the run; production defaults
are 4500 s, 3600 s and 1800 s.

| Scenario | Result | Evidence |
|---|---|---|
| Routing and full flow | PASS | Preprocessing, quality and duplicate tasks only on the web worker; every enhancement task only on GFPGAN workers. Real GFPGAN on MPS, identity verified (0.97); downloaded bytes equal the stored file; progress 100 %. |
| Duplicate delivery | PASS | One task delivered three times across two workers; one output file, no orphans. |
| Database connection killed mid-task | PASS | `pg_terminate_backend` on the worker's connection; task retried, resumed its own claim (attempt 1), completed in 35 s, one file. |
| Cancellation mid-batch | PASS | Job and all four results cancelled; no output for any. |
| Worker killed (SIGKILL) and restarted | PASS | Redelivered after the visibility timeout, waited out the dead claim, completed after 132 s, one file. |

Found by this run and fixed (`b23524b`): with a visibility timeout shorter than
the claim lease, a task waiting on another worker's claim was redelivered
repeatedly and a solo worker stalled for ten minutes. Settings now refuse that
combination.

Not covered: the stuck-job reaper (beat was not run), a worker killed while
holding a batch, Redis itself failing, and a real (not injected) GPU
out-of-memory error.

## 3. Visual evaluation

**Photos.** Six stock photographs from the user's Downloads folder
(Unsplash/Pexels), reduced to 1600 px: three portraits (low-key studio light;
harsh sun with deep shadow; bright studio with drawn lines on the face) and
three without faces (dark low-key hands; white high-key product shot; busy
still life). **No group photograph was available**, and the portraits cover
three skin tones at most. This is not a representative set.

Two kinds of case are kept apart: **real** (the photo as it is) and
**synthetic** (the same photo darkened, noised, blurred, compressed or made
grayscale). Results: `.runtime/visual_eval/<run>/` with `summary.md`,
`results.json`, full comparisons and `*__crops.jpg` (full-resolution face and
background crops).

### Real photos, one optional model at a time

| Run | Median time (max) | Peak host / GPU MB | What it did | Visual verdict |
|---|---|---|---|---|
| Default (Zero-DCE, EDSR, denoise, guards) | 1.3 s (1.6) | 1258 / – | Brightened the two low-key photos; others nearly untouched | Portraits natural. **The dark "hands" photo was lifted to grey, flat blacks** (SSIM to the original 0.62): the pipeline cannot tell intent from underexposure. |
| + HD (NAFNet, Real-ESRGAN) | 73 s (101) | 818 / 1395 | 2x on every photo; NAFNet only where blur was measured | Before the fix, a tile of colour noise in the hair of two portraits. After it, clean; one tile rejected. Sharper skin and stubble, slightly harsher. Too slow on this machine. |
| + PureVision | 7 s (7.6) | 1432 / 1137 | Ran on every photo | Subtle; raised clipped highlights on the still life (0.06 % to 0.54 %). Not justified on clean photos. |
| + PureScale | 43 s (50) | 1588 / 1067 | 2x on every photo | Subtle; no visible defect. Upscales photos that did not need it. |
| + GFPGAN | 2.5 s (10.9) | 1356 / 2591 | Restored the one mildly blurred portrait; refused nothing else | Modest at the 25 % blend; identity verified (0.97). |

### Synthetic degradations

| Run | Finding |
|---|---|
| Default, dark | Portraits recovered well (SSIM to the clean photo 0.55-0.59 to 0.74-0.91). |
| Default, noisy / blurred / compressed | Small or no change on portraits; the dark "hands" photo again brightened away from its original. |
| HD, blurred and compressed (after the fix) | No gain over the default pipeline on these mild blurs; the compressed sun portrait that dropped to 0.865 before the fix now holds at 0.944. |
| PureVision, compressed | Mixed: small gain on the still life and studio portrait, losses on the two low-key portraits. |
| GFPGAN, blurred faces | Applied to three faces, identity verified 0.98-0.99. |
| Colorization, grayscale | **Rainbow blotches on the background of both colour portraits** (a second, unrelated portrait shows the same defect seen on 2026-10-13). Identity similarity stayed above 0.91, which is exactly why similarity alone is not a quality measure. |

## 4. Recommended settings (this machine)

| Setting | Recommendation | Evidence |
|---|---|---|
| `ZERO_DCE_ENABLED` | on | Integrity and inference validated; good recovery of underexposed portraits. Known risk: deliberately dark photos are brightened. |
| `AI_RESTORATION_ENABLED` (EDSR) | on | Validated; runs only on genuinely low-resolution photos. |
| YuNet, SFace, `IDENTITY_GUARD_ENABLED` | on | Validated; required for honest identity reporting and for GFPGAN's gate. |
| `HD_RESTORATION_ENABLED`, `NAFNET_DEBLUR_ENABLED` | off | Work after the fixes, but 60-100 s per 1.7 MP photo on MPS and no measured gain on these photos. Reconsider on CUDA. |
| `PUREVISION_ENABLED`, `PURESCALE_ENABLED` | off | Non-commercial licence; they run on every photo, defect or not. |
| `GFPGAN_ENABLED` | off by default, optional | Works, gated and reported honestly; generative. Requires the GFPGAN worker. |
| `COLORIZATION_ENABLED` | off | Visible colour blotches on two of two portraits. |

`GFPGAN_REQUIRE_IDENTITY_CHECK` stays on. The identity bypass under AI Max is
reported in results, comparisons, recommendations and CSV exports (commits
`eb87434`, `e4a929e`).

## 5. Still unverified

- Any NVIDIA/CUDA behaviour, timing or memory.
- The private FaceCloud curve model (file not available).
- Group photographs; a wider range of skin tones and real (not synthetic) noise,
  blur and compression.
- Visual quality on real full-resolution photos above 1600 px (inputs were
  reduced for the optional models).
- The stuck-job reaper, and Celery with the default (prefork) pool.
- That a deliberately dark photo should be left dark: there is no rule for it.
