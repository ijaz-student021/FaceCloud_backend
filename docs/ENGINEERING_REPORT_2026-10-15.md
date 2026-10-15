# FaceCloud AI backend: engineering report (2026-10-15)

Branch `feat/focused-model-set`, Draft PR #72 (stacked on #71; merge order #67 -> #68 -> #71 -> #72). Nothing is merged or deployed. `adaptive_v1` stays opt-in;
CodeFormer and GFPGAN stay off by default; the frontend was not touched. Every number below was measured on 16 phone photographs (one phone, mostly one person) with
**synthetic** degradations on an Apple M1 with 8 GiB, unless a section says otherwise. Detail and reproduction commands are in the documents linked per section.

## 1. Executive technical summary

The adaptive engine worked as designed on decisions (one input, one validated output, honest outcomes) but a review against measurements found that its main visible
product, the low-light lift, **lost colour** (the brown shirt turned grey-brown) and, worse, the earlier colour fix made the validator reject lifts that should have been
accepted. The cause was measured (the network returns pale colours; a square-root chroma gain under-restored them) and fixed without tuning a constant: the photograph keeps its
own colours in linear light and takes only brightness from the network. On held-out photographs chroma retention against the clean photograph went from 0.56-0.81 (raw network) and
0.61-0.92 (before) to 0.83-1.10, hue error from 6-10 to 2.5-3.7 degrees; in the full engine a lift that used to be rejected is now accepted (PSNR 12.7 -> 16.1, chroma retention 0.26 -> 0.95).
Other fixes: a noise measure that can see lift grain, explicit identity states, a time budget that bounds model calls, a conservative super-resolution rule, a CPU tile cap (memory 2.7 -> 1.7 GB,
time 56-70 -> 24 s), metrics, a separable inference queue, and a production guard against unlicensed models. Not solved and not claimable: identity calibration, real-noise and real-blur performance,
real S3, CUDA, human preference, commercial licensing. **The system is implemented and tested; it is not validated for production.**

## 2. Existing architecture assessment

Kept, because it works and is covered by tests: the Django/DRF/Celery job system (one active job per image by a partial unique constraint; claim, lease, renewal and takeover; retry only for dropped
database connections; a worker that lost its claim publishes nothing; ownership checks on every endpoint), the encrypted private storage design, uploads (decompression bomb refusal, ICC to sRGB,
EXIF orientation, no EXIF in outputs), checkpoint integrity (`weights_only`, pinned SHA-256), the production guard. The adaptive engine (analyzer, router, candidate pool, validator, identity checker) is
a sound structure: it re-measures after every accepted step, returns the original when nothing passes, and records why. Weak points are in section 3.

## 3. Critical weaknesses identified

Full prioritised audit with file, function, evidence, consequence, fix and verification: `docs/evaluation/2026-10-15-engineering-audit.md`. In brief: C1 colour loss and wrongly rejected lifts (fixed); C2 commercial licences (guarded, open);
C3 identity gate blind to drift and uncalibrated (mitigated, open); H1 noise blind spot (fixed); H2 blur typing weaker than reported (measured); H3 identity reporting inaccuracies (fixed); H4 time budget did not bound model calls (fixed);
H5 inference not separable (fixed, opt-in); H6 previews run models in the API process (documented, open); H7 no real S3, CUDA, real noise or human evaluation (open).

## 4. Root-cause analysis of the image-quality issues

*Grey-brown colour.* Two causes. (a) Retinexformer's own output is pale and hue-shifted (retention 0.56-0.81; hue 6-10 degrees) and noisy in chroma. (b) The shipped correction scaled the source's CIELAB chroma by the square root of the lightness gain, but CIELAB chroma grows
roughly in proportion to lightness under an exposure change, so it under-restored (up to about 40 % paler on the darkest inputs); the old colour gate compared absolute a/b and could not see pallor, and the combination rejected valid lifts. A larger exponent
over-saturates (retention up to 1.5), so an exponent search was rejected for chromaticity preservation in linear light.
*Visible grain after a lift.* The only noise estimate was the integer median of a 3x3 residual; it read 2.0 on a result with clearly visible coarse grain. A flat-region measure was added; grain remains partly under-read when strongly correlated.
*Over-processing risk.* A small face alone triggered a whole-photograph x2 enlargement. Removed.
Method and tables: `docs/evaluation/2026-10-15-lowlight-colour.md`.

## 5. Changes implemented

Colour (`adaptive/colour.py`, `operations.run_low_light`, validator colour gate); noise (`analyzer.flat_noise`, `noise.level`, validator noise gate); identity reporting (`service.identity_report`); failed-job summary; small-face routing; cooperative time budget
(`runtime.deadline_until`, enforced between tiles); CPU tile cap for Retinexformer; structured run log and `manage.py adaptive_report`; `ENHANCEMENT_QUEUE_NAME` routing (`config/celery_routes.py`); production guard rules
(`ADAPTIVE_DEBUG_ARTIFACTS`, non-commercial models). Evaluation tooling: `scripts/evaluate_lowlight_colour.py`, `evaluate_denoising.py`, `evaluate_blur_routing.py`, `measure_inference.py`, `calibrate_identity.py`.
Rejected after measuring: tuning a chroma exponent; a "denoise after every lift" rule (denoising worsens LPIPS on lift grain); changing blur thresholds (no held-out evidence of net gain).

## 6. Exact files modified

Code: `config/{celery_routes,production_guard,settings}.py`, `enhancement/adaptive/{analyzer,colour,operations,router,service,validator}.py`, `enhancement/{lowlight,runtime,serializers,services}.py`,
`operations/{adaptive_metrics}.py`, `operations/management/commands/adaptive_report.py`, `.env.example`. Tests: `enhancement/test_adaptive_{colour,engine,real_models}.py`, `enhancement/test_{model_set,runtime}.py`,
`config/test_production_guard.py`, `operations/test_adaptive_metrics.py`. Scripts and docs as listed in the commit. No frontend file, migration, model checkpoint, credential or photograph is in the change.

## 7. Model configuration improvements

Retinexformer: colour from the photograph (measured), CPU tile 512 (measured: memory -37 %, time -57 to -66 % at 1.2 MP, quality within 0.06 dB of whole-image; 256 loses up to 0.9 dB, so not chosen). NAFNet-SIDD: two variants (full and 65 %) confirmed by measurement
(65 % better on light-to-moderate noise, full better on heavy noise; full strength smooths faces: texture kept 0.74-0.89). NAFNet-GoPro: routing unchanged (helps linear motion up to ~27 px by +5 to +7.6 dB, not camera-shake paths,
helps large defocus). Real-ESRGAN: x2 only, only for a genuinely small photograph, not practical on CPU (about 5 minutes for 0.3 MP). CodeFormer/GFPGAN: off, blocked in production. Settings added: `ADAPTIVE_LOWLIGHT_COLOUR`, `RETINEXFORMER_CPU_TILE_SIZE`,
`ADAPTIVE_FACE_UPSCALE_BELOW` (default now 0), `ENHANCEMENT_QUEUE_NAME`, `NONCOMMERCIAL_MODELS_ACKNOWLEDGED`.

## 8. Image-quality benchmark comparison

Low-light colour, held-out photographs (chroma retention, 1.0 = clean): raw 0.56-0.81, before 0.61-0.92, now 0.83-1.10; hue shift 6.0-10.4 / 3.2-4.7 / 2.5-3.7 degrees; LPIPS equal or better (`2026-10-15-lowlight-colour.md`).
Denoising: Gaussian 12 PSNR 26.7 -> 31.2 (65 % blend) / 29.9 (full); heavy phone-like 28.5 -> 31.3 (full); no benefit after a lift (`2026-10-15-denoising.md`).
Blur routing, held-out: runs where the model helps 25/34, where it hurts 9 of 38 runs, 90 % of the gain captured (`2026-10-15-blur-routing.md`).
Whole engine through the real HTTP API (re-run on this commit, 6 cases, all checks pass): dark portrait enhanced PSNR 12.7 -> 16.1, SSIM 0.41 -> 0.78, LPIPS 0.44 -> 0.29; motion blur 32.6 -> 40.6, LPIPS 0.30 -> 0.17, ArcFace to clean 0.77 -> 0.98;
noisy 23.4 -> 24.8; dark and defocused 12.7 -> 23.8 (ArcFace to clean 0.87 -> 0.81); clean photograph and the badly blurred second photograph returned byte-identical.
These are synthetic degradations, and PSNR and SSIM do not measure appearance; the colour contact sheets were looked at by the author only.

## 9. Identity validation findings

The gate compares a candidate with the degraded input, not the unseen clean face: in the benchmark it read 0.85-0.91 where ArcFace similarity to the clean face had dropped 0.07-0.13, and the 0.70 ArcFace threshold rests on five data points.
Dark faces cannot be embedded, so a lift of a very dark face is unverifiable. SFace and ArcFace do not share a scale and sometimes disagree. Face restoration therefore stays off, is never accepted without verified identity, and the API now says what was and was not established
(`identity_verification.state`: verified / uncertain / not_verifiable / not_applicable, with scope and notice; an unavailable comparison is never reported as passed). The dark-and-defocused case shows the limit in practice: the lift was accepted while identity similarity to the clean face fell, and the engine
reported what it could check. **Calibration is not done**: `scripts/calibrate_identity.py` implements the procedure (genuine/impostor distributions per degradation, threshold at a stated false-match rate chosen on one half of the people and judged on the other); it was smoke-tested only
and needs a consented, diverse, labelled set.

## 10. CPU performance and memory measurements

`docs/deployment/hostinger-readiness-2026-10.md`: Retinexformer 5 s (0.3 MP), 24 s (1.2 MP, tile 512), peak 1.7 GB; NAFNet-SIDD 4 / 18 / 37 s at 0.3 / 1.2 / 3.1 MP, 1.1-1.4 GB; NAFNet-GoPro 3.5 / 14 / 30 s, about 1 GB; Real-ESRGAN x2 275-289 s at 0.3 MP, 2.6 GB. Apple M1 CPU only; not a VPS.

## 11. Hostinger deployment readiness

Not validated on Hostinger. The assessment (same document) gives a measured sizing method, the API / inference-worker / S3 split, the setting that separates the inference queue, the preview exception (models load inside the API process), and a troubleshooting table.
Verdict: the design is deployable as a split; the numbers must be re-measured on the real machine.

## 12. AWS S3 readiness

Implemented and tested against simulated S3 (23 moto tests); nonce and key handling reviewed (random 64-bit prefix + chunk counter; header, index and finality authenticated; path not in the associated data). **Real AWS validation is pending**: it needs an authorised non-root IAM user and a test bucket
(`aws_provision_bucket.py`, `check_storage`, a cross-user attempt, large and interrupted uploads). Not run.

## 13. Security findings

Verified sound (unit level): authentication and ownership, download authorisation, upload validation and decompression-bomb refusal, ICC and EXIF handling, checkpoint integrity, secrets and TLS guard, no private content in logs (the new log line carries no pixels, paths or user data), debug artifacts off by default.
New: production refuses debug artifacts. Open: preview rendering loads models in the API process (resource and attack surface); 100 MP upload limit against API memory; encryption associated data not bound to the object path and a 64-bit random nonce prefix (negligible at the expected scale);
`manage.py test --parallel` crashes with an unpicklable traceback (not investigated). Dependency audit and static analysis run in CI.

## 14. Commercial model licensing status

`docs/model-licenses/commercial-inventory-2026-10.md`. Non-commercial: CodeFormer, ArcFace/InsightFace weights, Zero-DCE (owner's approval not on file). Unresolved: GFPGAN permission, RetinaFace weights, and the **training-data terms of every checkpoint** (never checked). Source code of Retinexformer, NAFNet, Real-ESRGAN is permissive, which does not settle their checkpoints.
No licensor was contacted, nothing was bought, nothing is assumed. As configured the system is academic-use only.

## 15. Automated test results

Local, this commit: **1,914 tests OK, 11 skipped** (the skips are real-model and real-photo tests that need checkpoints and `FACECLOUD_SAMPLE_PHOTOS`); `ruff` clean; `bandit -ll` no medium/high; no pending migrations; OpenAPI schema without warnings. CI result for the pushed commit: see section 16 addendum.
These are mostly mocked tests (stand-in model runners, stand-in identity, simulated S3): they show decisions and contracts, not photograph quality. Test kinds: `docs/evaluation/test-evidence-matrix.md`.

## 16. Real-model integration test results

With the real checkpoints and the sample photographs: the adaptive real-model tests pass (clean photograph untouched; dark photograph lifted by the real Retinexformer and recorded; real lift of a real photograph keeps its colour within the gate's limits; real RetinaFace runs);
`scripts/verify_adaptive_api.py` through the real endpoints with the real models: 6/6 cases pass every check (one output file, correct size, no debug files, preserved results pixel-identical, all three outcomes occur).
Not run: any CUDA device, real S3, a real broker with a killed worker, concurrent users, real smartphone noise, a human panel.

## 17. Remaining technical limitations

Identity uncalibrated; dark faces unverifiable; lift grain only partly measured and not denoisable by the SIDD model; blur typing approximate and untested on real shake and subject motion; global blur measure misses subject-only blur; Real-ESRGAN unusable on CPU and unproven by no-reference test;
calibration data small, one phone, mostly one person; skin-tone diversity unmeasured; every degradation synthetic; one machine; one person looked at the outputs.

## 18. Recommended future improvements

1. Collect a consented, licensed, diverse set (several phones, indoor/outdoor, ages, skin tones, real night noise, real shake) with calibration/test separation; rerun the five scripts; add a structured human rating.
2. Calibrate identity with `calibrate_identity.py` before any face restoration is considered; add an independent second embedding for agreement.
3. Move preview rendering with model sliders to a queued job (an API change for the frontend owner).
4. Per-file encryption subkeys and the path in the associated data (format version).
5. A face-region blur measure; a no-reference super-resolution benefit probe; per-device CPU/GPU defaults from a measured table.
6. Test on CUDA and on the Hostinger machine; a real broker failure drill.

## 19. Production deployment blockers

1. Commercial licences (section 14): unresolved or non-commercial components and unchecked data terms.
2. No real-world validation: real noise/blur, diverse people, human preference, identity calibration.
3. No real AWS S3 validation; no CUDA validation; no Hostinger measurement.
4. Preview-in-API-process architecture needs a decision.
5. Frontend integration of the Auto Enhance contract (owner of the frontend; intentionally untouched).

## 20. Release-readiness verdict

| Capability | IMPLEMENTED | TESTED | VALIDATED | PRODUCTION-READY |
|---|---|---|---|---|
| Adaptive engine, one output, honest outcomes | yes | yes (mocked + real models, 6 HTTP cases) | on 16 photographs, synthetic degradations only | **no** |
| Natural colour for low-light lifts | yes | yes (unit, real-model, held-out measurement) | on synthetic darkening of 16 photographs; no real night photographs, no human panel | **no** |
| Denoising | yes | yes | synthetic noise only | **no** |
| Motion deblurring routing | yes | yes | synthetic blur only; approximate typing | **no** |
| Super-resolution | yes (conservative) | yes | not on CPU; benefit unmeasured | **no** |
| Face restoration | yes, off by default | yes (mocked, one real run) | **no**; identity unverifiable | **no** (blocked in production) |
| Identity reporting | yes | yes | not calibrated | **no** |
| Job reliability (claims, retries, ownership) | yes | yes (unit level) | no real broker or crash drill | **no** |
| Private S3 storage | yes | yes (simulated S3) | **no** (no real AWS) | **no** |
| Time/memory budgets | yes | yes | one CPU, one machine | **no** |
| Observability | yes (log line, report command) | yes | n/a | partial (no dashboards or alerts) |
| Licensing | guarded | yes | **unresolved** | **no** |
| Hostinger / GPU deployment | design and sizing method | no | **no** | **no** |

**Verdict:** a well-tested, honest, academically defensible prototype whose decisions are evidence-backed where evidence exists, with its limits stated. It is not production-ready, and the missing evidence (real data, real services, licences) cannot be produced by more code.
