# 2026-10-21: adaptive API verification, very dark photographs, and identity that cannot be verified

Status: branch `fix/adaptive-verification-identity-honesty`, made from `origin/main` at `8a700ad58`. **Not pushed, not merged, no default changed.**
All eight evidence files below were produced by the commit `b3aad14` of this branch from a clean working tree (`git.dirty: false`); later commits add only tests, documentation and the evidence itself.
Nothing here is a claim of production readiness.

## 1. What this changes

| | Before | Now |
|---|---|---|
| `scripts/verify_adaptive_api.py` | no consent (so the identity gate was silently off), a 400 crashed it, the stored result of a refused photo was never checked, tiles of the photographs were always written | grants or withholds consent explicitly (`--consent`), reports every case as exactly one of `enhanced`, `partially_enhanced`, `original_preserved`, `failed`, `expected_rejection`, checks the original is byte-identical, writes sanitised evidence, tiles only with `--save-tiles`, non-zero exit on any failure |
| a photo rejected only for darkness | always `rejected`, so never enhanced | unchanged by default; `QUALITY_UNDEREXPOSED_FACE_ACTION=review` (opt-in) makes it `needs_review` when a face was accepted and darkness is the only reason |
| identity that cannot be verified (adaptive engine) | operations that do not synthesise a face were applied; reported as `not_verifiable` but not named as a mode | unchanged by default and now named: `identity_verification.mode` is `limited_unverified`; `ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY=fail_closed` (opt-in) keeps the original (`failed_closed`) |

Files: `scripts/verify_adaptive_api.py`; `quality_control/services.py` (`make_decision`); `enhancement/adaptive/identity_policy.py` (new), `service.py`
(`identity_report`, the fail-closed stop), `validator.py` (`_identity_gate`); `config/settings.py`, `.env.example`; `docs/FRONTEND_API_GUIDE.md`; tests:
`quality_control/test_underexposed_face_policy.py`, `enhancement/test_identity_unverified_policy.py`, `enhancement/test_verification_script.py`.

## 2. Why a severe-underexposure photograph is rejected before enhancement (traced)

1. Upload, preprocessing and the face check run, then `quality_control.services.process_quality_decision` calls `make_decision`. It adds the reject reason
   `severe_underexposure` when the mean luminance is under `QUALITY_REJECT_MIN_LUMINANCE` (25) and `contrast_too_low` when the contrast (standard deviation) is
   under `QUALITY_REJECT_CONTRAST` (12). The darkened test photographs measure a mean luminance of 16.5-16.6: both reasons, decision `rejected`.
   Blur and a low resolution already never reject a photograph that has a face (`photo_has_face`: the restoration chain exists for them); darkness was left out of that rule.
2. `enhancement/views.py` `ImageEnhanceView.post` calls `rejection_of(image)` (`enhancement/eligibility.py`), which excludes any photograph whose
   `quality_decision.decision` is `rejected`, and answers **HTTP 400 `["This photo was rejected by the checks, so it is not enhanced."]`**. Nothing is created: no
   result row, no file. The batch route filters the same way (`enhancement/batch_jobs.py`).
3. That rule arrived on 2026-10-18 (commit `20bb8c4`), after the 2026-10-15 engine evidence, which is why those dark cases were enhanced then and are refused now.
   The old verification script crashed on the refusal (`queued.json()["id"]` on a list) and, with no consent granted, ran the identity gate switched off.

## 3. The policy, and the choices it leaves to the owner

Two **independent, opt-in** settings; both default to today's behaviour.

| Setting | Default | Other value | Effect |
|---|---|---|---|
| `QUALITY_UNDEREXPOSED_FACE_ACTION` | `reject` | `review` | only when darkness is the *only* reject reason, a face was found and the face check did not reject it: `needs_review` (the same reasons are kept) instead of `rejected`. Accepts nothing; a tiny, blown-out, faceless or face-rejected photograph stays rejected; an organization's upload also lands in the moderator queue |
| `ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY` | `limited` | `fail_closed` | photograph with a face whose identity cannot be verified (no consent, no model, face too dark/small to embed, inconclusive comparison): `limited` applies the operations that do not synthesise a face and labels the result `mode: limited_unverified`; `fail_closed` applies nothing and returns the original (`mode: failed_closed`, outcome `original_preserved`) |

`ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY` is read **only by the adaptive engine** (`ENHANCEMENT_RENDER_POLICY=adaptive_v1`, still opt-in). Under the default `lightweight_v1` the setting changes nothing (a test pins this), so choosing `fail_closed` has no effect until `adaptive_v1` is switched on.

In both modes `verified` is false unless a comparison held, face restoration is never applied without a verified identity, and the original is returned unless a
candidate passes the engine's quality, colour, artifact and identity gates. `identity_verification.mode` is one of `verified`, `limited_unverified`,
`failed_closed`, `not_applicable`.

**Decided by the owner on 2026-10-21: D1 = `reject` (keep), D2 = `fail_closed`.** Section 8 documents that combination. The options below are kept for the record.

**Decisions I did not make for you** (each needs a product or legal choice, or consent wording that does not exist yet):

* **D1, very dark photographs.** (a) keep `reject`; (b) `review`: they become eligible for enhancement and, in an organization, for a moderator. With (b), nearly every such
  photograph also falls under D2, because a face that dark cannot be embedded (see the table in section 4).
* **D2, identity that cannot be verified.** (a) `limited` + the existing notice (`identity_notice`), which the frontend must show; (b) `fail_closed`: strictest, and it
  means an account without biometric consent gets no enhancement of photographs with faces, and dark faces are never enhanced; (c) `limited` plus an explicit
  acknowledgement the user gives before such a result is produced: needs new consent wording and an endpoint or field for it (not built); (d) a per-cause policy, for example
  `fail_closed` only when consent is absent: not built.
* The notice text, any acknowledgement wording and where it is recorded are **not** written here.

Suggested, not applied: keep the defaults for the academic demonstration (and show runs 1 and 3 below); for a production service prefer `fail_closed` unless a
consent/acknowledgement flow exists; switch on `review` only with a moderator workflow.

## 4. Evidence

Runs 1-6: the same two photographs and the same six synthetic cases, on the same machine, differing only in the flags in the heading of each column
(`--consent`, `--quality-underexposed`, `--identity-policy`). Runs 7 and 8, the chosen combination, are in section 8. Every case passed every check in every run (`all_checks_pass: true`, exit status 0).

Classification of each case:

| case | 1 default-consent | 2 default-no-consent | 3 review-consent | 4 review-no-consent | 5 review-failclosed-consent | 6 review-failclosed-no-consent |
|---|---|---|---|---|---|---|
| dark_portrait | expected rejection | expected rejection | enhanced | enhanced | original preserved | original preserved |
| noisy_portrait | partially enhanced | partially enhanced | partially enhanced | partially enhanced | partially enhanced | original preserved |
| motion_blur | partially enhanced | partially enhanced | partially enhanced | partially enhanced | partially enhanced | original preserved |
| dark_and_defocused | expected rejection | expected rejection | partially enhanced | partially enhanced | original preserved | original preserved |
| clean_photograph | original preserved | original preserved | original preserved | original preserved | original preserved | original preserved |
| motion_blur_second_photo | original preserved | partially enhanced | original preserved | partially enhanced | original preserved | original preserved |

`identity_verification.mode` of each case (`-` = the request was refused, so there is no result):

| case | 1 default-consent | 2 default-no-consent | 3 review-consent | 4 review-no-consent | 5 review-failclosed-consent | 6 review-failclosed-no-consent |
|---|---|---|---|---|---|---|
| dark_portrait | - | - | limited_unverified | limited_unverified | failed_closed | failed_closed |
| noisy_portrait | verified | limited_unverified | verified | limited_unverified | verified | failed_closed |
| motion_blur | verified | limited_unverified | verified | limited_unverified | verified | failed_closed |
| dark_and_defocused | - | - | limited_unverified | limited_unverified | failed_closed | failed_closed |
| clean_photograph | not_applicable | not_applicable | not_applicable | not_applicable | not_applicable | not_applicable |
| motion_blur_second_photo | not_applicable | limited_unverified | not_applicable | limited_unverified | not_applicable | failed_closed |

Run key: 1 default settings, consent granted; 2 default settings, consent absent; 3 `review`, consent granted; 4 `review`, consent absent; 5 `review` + `fail_closed`, consent granted;
6 `review` + `fail_closed`, consent absent.

Metrics against the clean photograph, run 4 (`review`, `limited`, consent absent; the full set for every run is in the JSON):

| case | quality decision | PSNR in -> out | SSIM in -> out | SFace to the clean face, in -> out | seconds |
|---|---|---|---|---|---|
| dark_portrait | needs_review (severe_underexposure, contrast_too_low), mean luminance 16.6 | 12.7 -> 16.1 | 0.408 -> 0.781 | 0.9168 -> 0.9347 | 5.3 |
| noisy_portrait | accepted, mean luminance 68.1 | 23.4 -> 24.8 | 0.647 -> 0.740 | 0.8454 -> 0.8637 | 7.7 |
| motion_blur | accepted, mean luminance 68.1 | 32.6 -> 40.6 | 0.918 -> 0.972 | 0.8065 -> 0.9856 | 6.5 |
| dark_and_defocused | needs_review (severe_underexposure, contrast_too_low), mean luminance 16.5 | 12.7 -> 23.5 | 0.396 -> 0.846 | 0.8608 -> 0.772 | 8.9 |
| clean_photograph | accepted, mean luminance 68.2 | unchanged | unchanged | 1.0 -> 1.0 | 0.5 |
| motion_blur_second_photo | needs_review (face_validation_required), mean luminance 123.2 | 25.7 -> 31.4 | 0.790 -> 0.936 | 0.5043 -> 0.891 | 5.6 |

What the numbers say, and do not say:

* **Run 1 against 2 (consent).** The same photographs, only the consent differs. `motion_blur_second_photo` is `original_preserved` with consent (both candidates failed the identity gate)
  and `partially_enhanced` without it (the gate cannot run, the geometry checks pass, the deblur is applied): without consent the default `limited` policy lets through a change the gate would have
  blocked. Run 6 (`fail_closed`, no consent) keeps all six originals unchanged.
* **Dark photographs (runs 3-4).** The face is too dark to embed, so identity is `unavailable` and the mode is `limited_unverified` in both. `dark_and_defocused` rises 10.8 dB in PSNR while the SFace
  similarity of its face to the *clean* face falls from 0.861 to 0.772: a drift the engine could not see. This is the case for `fail_closed` (runs 5-6 keep the original), and it is one photograph.
* **The gate is not an oracle.** In run 1 the second photograph is kept because its candidates failed `identity_changed` against a face that was itself blurred (the blurred face scores only 0.504
  against the clean one); the candidate accepted when the gate is off (runs 2, 4) scores 0.891 against the clean face. So the gate can refuse a real improvement as well as miss a drift.
* **`original_preserved` with `identity_status: verified`** (run 1, `clean_photograph` and the second photograph) means only that the output is the upload; the mode is `not_applicable`. A frontend should not
  show an identity badge on a result that was not changed.
* Timings are one run each on a loaded 8 GiB laptop, cold models included, Celery eager: 0.3-14 s per case, 3-41 s per run. They are not a benchmark.

## 5. Reproduce

Environment of these runs: macOS-27.2-arm64-arm-64bit, arm64, 8 cores, 8 GiB RAM, Python 3.12.13, torch 2.14.0 (devices: {'cuda': False, 'mps': True}),
OpenCV 5.0.0, NumPy 2.5.3, Django 6.1.2, LPIPS not installed (so no LPIPS numbers). Celery eager, filesystem storage, a throwaway test database, encryption off.
Settings forced by the script are in each JSON under `settings`; checkpoints (all match their pinned SHA-256 and `docs/gpu/model_manifest.json`):

| model | file | bytes | SHA-256 | matches the pin |
|---|---|---|---|---|
| retinexformer | Retinexformer_NTIRE.pth | 6476406 | `ba460a6882090ff3312c11fe96b3e7dc5be8e013b8c3b3a7ef6a529cc5ff27fa` | yes |
| nafnet_gopro | NAFNet-GoPro-width64.pth | 271778961 | `329d3ab4077b8d6b7ff61de376e483714667960bf85be027bf4335cda701196f` | yes |
| nafnet_sidd | NAFNet-SIDD-width64.pth | 464154961 | `cd685efaae01f7c4e9951f2deab05780079c8eb1e49ed664b72f6db04dabb445` | yes |
| realesrgan | RealESRGAN_x4plus.pth | 67040989 | `4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1` | yes |
| zero_dce | zero_dce_epoch99.onnx | 320301 | `e5cb6f921c7997f89105f6aa86242f30ff3f5379f5c6cd5282eb2aada14dae14` | yes |
| yunet | yunet_s_dynamic.onnx | 237823 | `daede24002cc8590f457998cc5e6c9e8b82f45f6bd969506a8913cd4ed10a2d3` | yes |
| sface | face_recognition_sface_2021dec.onnx | 38696353 | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` | yes |

Test photographs (not in the repository, and never to be committed): any folder with at least two JPEG/PNG/WebP portraits you have the right to use; the first two in sorted file-name order are used
(`--second-photo` picks another). They should show a clear, front-facing face and be at least about 900 pixels on the long side. The runs above used two private phone portraits (1200x1600 and 1204x1600, no EXIF; the SFace similarity between their two faces, measured separately, is 0.22, so most likely two different people), identified here only by the first 16 hex characters of their SHA-256 (`ede8d889288af4aa`, `043bd1b46cd90958`), scaled to a 640-pixel long edge. A different pair of photographs gives different numbers.

```
cd FaceCloud_backend
export DJANGO_DEBUG=True DB_ENGINE=django.db.backends.sqlite3 DB_NAME=/tmp/verify.sqlite3
PY=.runtime/venvs/gfpgan/bin/python
$PY scripts/verify_adaptive_api.py --images PHOTOS --out OUT/1-default-consent --label 1-default-consent --consent granted
$PY scripts/verify_adaptive_api.py --images PHOTOS --out OUT/2-default-no-consent --label 2-default-no-consent --consent absent
$PY scripts/verify_adaptive_api.py --images PHOTOS --out OUT/3-review-consent --label 3-review-consent --consent granted --quality-underexposed review
$PY scripts/verify_adaptive_api.py --images PHOTOS --out OUT/4-review-no-consent --label 4-review-no-consent --consent absent --quality-underexposed review
$PY scripts/verify_adaptive_api.py --images PHOTOS --out OUT/5-review-failclosed-consent --label 5-review-failclosed-consent --consent granted --quality-underexposed review --identity-policy fail_closed
$PY scripts/verify_adaptive_api.py --images PHOTOS --out OUT/6-review-failclosed-no-consent --label 6-review-failclosed-no-consent --consent absent --quality-underexposed review --identity-policy fail_closed
```

Use the worker environment (it has PyTorch); the script exits non-zero if any case fails a check. `--save-tiles` also writes side-by-side images of your photographs (do not commit them);
`--full-responses` keeps the whole API response in the JSON. No `.env` is read for the settings that matter: the script forces them (`BIOMETRIC_CONSENT_REQUIRED=True`, the detector, the identity provider and
its pinned checksums from the committed manifest, the licence record from `.runtime/licenses` when the environment does not set one).

Evidence paths (runs 1-8): compact copies (no photographs, no face crops, links and task ids redacted, bulky measurement reports trimmed) are committed under
`docs/evaluation/evidence/2026-10-21/<label>.json`; the same runs in full, including `verification.json`, are on the author's machine at
`~/FaceCloud_verification_evidence/2026-10-21/<label>/`.

## 6. Tests

New tests (58), all passing; none of the existing tests was changed:

| File | Tests | Covers |
|---|---|---|
| `quality_control/test_underexposed_face_policy.py` | 15 | severe underexposure and low contrast: rejected by default; with `review`, `needs_review` only when darkness is the sole reason and a face was accepted; no face, a rejected face, another reject reason (too small, blown out) stay rejected; the reasons are kept; end to end through `process_quality_decision` and the enhancement endpoint (400 and nothing created by default, 202 with the opt-in); the original file is never touched |
| `enhancement/test_identity_unverified_policy.py` | 23 | consent present and absent, identity model missing, face too dark to embed, inconclusive and failed comparisons, no face, a photograph that needs nothing, under `limited` and `fail_closed`; `verified` is true only when a comparison held (checked over every combination); the original is returned byte for byte and no model runs under `fail_closed`; the validator refuses an unverified candidate on its own; through the real job code and the API with `BIOMETRIC_CONSENT_REQUIRED=True`; the setting changes nothing under `lightweight_v1` and reaches the engine under `adaptive_v1` |
| `enhancement/test_verification_script.py` | 20 | the script's classification (a refused photograph is never `enhanced`), redaction, response trimming, the identity-honesty check, the summary, hashes, the recorded command line keeps no local path, and the `policy_*` checks of the chosen combination (a dark case must be refused for underexposure; no unverified change, no model run, no change without consent) |

Each group was also checked the other way: twelve deliberate breaks of the new code (the review branch, its conditions, the fail-closed stop, the validator gate, a `verified` flag, a `mode`, the
classification, four of the `policy_*` checks) each made tests fail, and the code was restored.

Regression, run on this branch (the same commands as CI; the worker environment is not needed for them):

| Check | Result |
|---|---|
| `manage.py test`, SQLite | Ran 2679 tests, OK (skipped=45: the real-model tests that need the checkpoints, PyTorch or sample photographs, as in CI) |
| `manage.py test`, PostgreSQL | Ran 2679 tests, OK (skipped=40) |
| real-model enhancement tests in the PyTorch environment with the checkpoints installed (`test_adaptive_real_models`, `test_adaptive_engine`, `test_runtime`, `test_model_set`, `test_claim_retry`, `test_staged_pipeline`, the three new modules, `quality_control`) | Ran 282 tests, OK (skipped=3: `FACECLOUD_SAMPLE_PHOTOS`, the RetinaFace weights, and the CodeFormer checkpoint path, which this run did not pass) |
| `ruff check .` (0.17.1) | clean |
| `manage.py spectacular --validate --fail-on-warn` | exit 0 |
| `manage.py makemigrations --check --dry-run` | no changes (there is no migration) |
| `bandit -ll` | 0 medium, 0 high |
| production-configuration check | exit 0 |

An aside found while doing this: with a developer `.env` that sets `FACE_DETECTOR_PROVIDER=yunet_v1`, five tests in `enhancement/test_staged_pipeline.py` (`FaceRestorationIdentityGateTests`) fail, because they
assume the default detector. They pass with the default environment, and CI is unaffected. Not changed here.

## 7. What remains unverified

* **Real-world images.** Two photographs of two people from one phone, synthetic degradations (the dark case is a x0.25 scaling plus noise, which is not how a dark scene is captured). No real low-light capture,
  real motion blur or real sensor noise, no other skin tones, ages, cameras, poses or groups. Nothing here generalises beyond these photographs.
* **Quality-gate limits.** 25 (luminance) and 12 (contrast) are the existing values; I did not calibrate them, and I have not measured how many real photographs `review` would send to a moderator.
* **Identity thresholds.** The guard limits (0.70 ArcFace, 0.80 SFace) are provisional and uncalibrated for this use; the similarity numbers above are evidence, not proof of identity, and come from one embedding model.
* **GPU/CUDA.** Nothing was run on CUDA; the timings are an Apple M1 (MPS).
* **AWS S3.** Not exercised (filesystem storage was used).
* **Model licensing.** Unchanged and unresolved: see `docs/model-licenses/commercial-inventory-2026-10.md` (CodeFormer, ArcFace and Zero-DCE are non-commercial; the training-data terms of every checkpoint are unchecked).
  Face restoration stayed off throughout.
* **A real worker.** The runs use Celery eager mode (in-process); a Redis worker, concurrency and timeouts were not exercised.
* **Consent wording and flow** do not exist yet (decision D2).
* **An inconclusive identity comparison** never occurred on a real photograph in these runs; it is covered only by unit tests with a stand-in checker.
* **The chosen `fail_closed` policy** means accounts without biometric consent get no enhancement of photographs with faces under `adaptive_v1`; the consent flow and wording, and the frontend behaviour for `failed_closed`, are not built or tested.
* **The chosen settings take effect only under `adaptive_v1`**, which is still opt-in and was not made the default; under `lightweight_v1` the identity policy setting is ignored.
* LPIPS is not installed in either environment, so no LPIPS value is reported.

## 8. Settings actually in effect in runs 1-6, and the chosen configuration (D1 `reject`, D2 `fail_closed`)

### 8.1 Reconciling the report

A reader could take "consent absent produced `partially_enhanced`" and "`fail_closed`" to contradict each other. They do not: **`fail_closed` is not the default and was never described as one.** The
code defaults are `QUALITY_UNDEREXPOSED_FACE_ACTION = reject` and `ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY = limited` (`config/settings.py`; `.env.example` repeats them; the developer `.env` sets neither;
loading Django with only `DJANGO_DEBUG=True` gives `'reject'` and `'limited'`). "default" in the labels of runs 1 and 2 means *those two settings at their code defaults*. The values each run
really used are recorded inside its own evidence file (`settings`), and are, from the saved files:

| # | run | `QUALITY_UNDEREXPOSED_FACE_ACTION` | `ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY` | `BIOMETRIC_CONSENT_REQUIRED` | consent requested / effective | code the run used |
|---|---|---|---|---|---|---|
| 1 | `1-default-consent` | `reject` | `limited` | `True` | granted / True | `b3aad14` |
| 2 | `2-default-no-consent` | `reject` | `limited` | `True` | absent / False | `b3aad14` |
| 3 | `3-review-consent` | `review` | `limited` | `True` | granted / True | `b3aad14` |
| 4 | `4-review-no-consent` | `review` | `limited` | `True` | absent / False | `b3aad14` |
| 5 | `5-review-failclosed-consent` | `review` | `fail_closed` | `True` | granted / True | `b3aad14` |
| 6 | `6-review-failclosed-no-consent` | `review` | `fail_closed` | `True` | absent / False | `b3aad14` |
| 7 | `7-chosen-reject-failclosed-consent` | `reject` | `fail_closed` | `True` | granted / True | `b3aad14` |
| 8 | `8-chosen-reject-failclosed-no-consent` | `reject` | `fail_closed` | `True` | absent / False | `b3aad14` |

So runs 1-4 ran `limited`: with consent absent (run 2) the engine could not verify identity and, under `limited`, applied the operations that do not synthesise a face (3 `partially_enhanced`).
Runs 5 and 6 ran `fail_closed` (all six originals kept in run 6). All eight evidence files were generated on 2026-10-21 from the clean commit `b3aad14` (`git.dirty: false`; the first versions of runs 1-6
were made from an uncommitted tree and were replaced, after the evidence review, by these; every HTTP status, label, identity mode, quality decision and metric is identical between the two versions, only the timings differ).
The environment of every run was only `DJANGO_DEBUG`, a throwaway SQLite database and `LOG_LEVEL`, plus absolute paths of the model files in environment variables; no `.env` was read.

One more default that matters for "consent absent": **`BIOMETRIC_CONSENT_REQUIRED` defaults to `False` in the code** (production refuses to start unless it is `True`). With it `False`, an account
without recorded consent is still allowed biometric processing, so the identity check runs and "consent absent" behaves like "consent given". The script therefore forces it to `True` in every run.

### 8.2 The chosen combination

D1 `reject` (kept) and D2 `fail_closed` (chosen), with consent granted and with consent absent, the same six cases. Settings in effect, from the evidence files: `QUALITY_UNDEREXPOSED_FACE_ACTION=reject`,
`ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY=fail_closed`, `BIOMETRIC_CONSENT_REQUIRED=True`, `ENHANCEMENT_RENDER_POLICY=adaptive_v1` (the engine this setting belongs to; the global default is unchanged),
`CODEFORMER_ENABLED=False`, `GFPGAN_ENABLED=False`.

```
cd FaceCloud_backend
M=/path/to/.runtime/models
export DJANGO_DEBUG=True DB_ENGINE=django.db.backends.sqlite3 DB_NAME=/tmp/verify.sqlite3 LOG_LEVEL=WARNING
export RETINEXFORMER_MODEL_PATH=$M/Retinexformer_NTIRE.pth NAFNET_MODEL_PATH=$M/NAFNet-GoPro-width64.pth NAFNET_SIDD_MODEL_PATH=$M/NAFNet-SIDD-width64.pth
export REALESRGAN_MODEL_PATH=$M/RealESRGAN_x4plus.pth ZERO_DCE_MODEL_PATH=$M/zero_dce_epoch99.onnx YUNET_MODEL_PATH=$M/yunet_s_dynamic.onnx
export IDENTITY_MODEL_PATH=$M/face_recognition_sface_2021dec.onnx ARCFACE_MODEL_PATH=$M/arcface/w600k_r50.onnx IDENTITY_LICENSE_RECORD_PATH=/path/to/.runtime/licenses/identity-models.md
PY=.runtime/venvs/gfpgan/bin/python
$PY scripts/verify_adaptive_api.py --images PHOTOS --out OUT/7-chosen-reject-failclosed-consent --label 7-chosen-reject-failclosed-consent --consent granted --quality-underexposed reject --identity-policy fail_closed
$PY scripts/verify_adaptive_api.py --images PHOTOS --out OUT/8-chosen-reject-failclosed-no-consent --label 8-chosen-reject-failclosed-no-consent --consent absent --quality-underexposed reject --identity-policy fail_closed
```

Expected behaviour, written down before the runs and checked by the script (`policy_*` checks, unit-tested in `enhancement/test_verification_script.py`), then confirmed below:

* a photograph the quality check rejects, in particular a very dark one, is refused: HTTP 400 with the stock sentence, no result row, no output file, label `expected_rejection`, never enhanced;
* with consent and an identity that can be verified, a change is applied only if its identity check held, and then `mode` is `verified`;
* whenever identity is unavailable or inconclusive: no candidate is applied (no operation, no stage, no model run in the `failed_closed` mode), the label is `original_preserved`, the returned file is
  pixel-identical to the upload and the stored upload is byte-identical; `mode` is `failed_closed`, never `limited_unverified`;
* without consent nothing is ever changed.

Result, run 7 (consent granted), all six cases and every check passed (`all_checks_pass: true`, exit status 0):

| case | HTTP | result row | output file | quality decision | outcome label | identity: status / mode | operations applied | models run | stored upload unchanged | returned file |
|---|---|---|---|---|---|---|---|---|---|---|
| dark_portrait | 400 | no | no | rejected | `expected_rejection` | - / - | - | - | yes | - |
| dark_and_defocused | 400 | no | no | rejected | `expected_rejection` | - / - | - | - | yes | - |
| clean_photograph | 202 | yes | yes | accepted | `original_preserved` | verified / not_applicable | none | none | yes | pixel-identical to the upload |
| noisy_portrait | 202 | yes | yes | accepted | `partially_enhanced` | verified / verified | ['denoising'] | nafnet_sidd | yes | changed (verified identity) |
| motion_blur | 202 | yes | yes | accepted | `partially_enhanced` | verified / verified | ['motion_deblurring'] | nafnet_gopro | yes | changed (verified identity) |
| motion_blur_second_photo | 202 | yes | yes | needs_review | `original_preserved` | verified / not_applicable | none | nafnet_gopro | yes | pixel-identical to the upload |

Result, run 8 (consent absent), all six cases and every check passed:

| case | HTTP | result row | output file | quality decision | outcome label | identity: status / mode | operations applied | models run | stored upload unchanged | returned file |
|---|---|---|---|---|---|---|---|---|---|---|
| dark_portrait | 400 | no | no | rejected | `expected_rejection` | - / - | - | - | yes | - |
| dark_and_defocused | 400 | no | no | rejected | `expected_rejection` | - / - | - | - | yes | - |
| clean_photograph | 202 | yes | yes | accepted | `original_preserved` | unavailable / not_applicable | none | none | yes | pixel-identical to the upload |
| noisy_portrait | 202 | yes | yes | accepted | `original_preserved` | unavailable / failed_closed | none | none | yes | pixel-identical to the upload |
| motion_blur | 202 | yes | yes | accepted | `original_preserved` | unavailable / failed_closed | none | none | yes | pixel-identical to the upload |
| motion_blur_second_photo | 202 | yes | yes | needs_review | `original_preserved` | unavailable / failed_closed | none | none | yes | pixel-identical to the upload |

How to read them:

* **Severe underexposure** is refused in both runs, before any consent question: mean luminance 16.6 and 16.5, reasons `severe_underexposure` and `contrast_too_low`, HTTP 400, nothing created, the stored upload unchanged.
* **Consent absent (run 8):** the three photographs that needed work (`noisy_portrait`, `motion_blur`, `motion_blur_second_photo`) came back unchanged with `mode: failed_closed`, no operation applied and no model run
  (the engine stops before any model); `clean_photograph` needed nothing (`not_applicable`). In run 2, the same photographs under `limited`, three of them were changed.
* **Consent granted (run 7):** `noisy_portrait` and `motion_blur` were changed because their identity was verified (`verified`). `motion_blur_second_photo` was kept: both candidates failed the identity
  check (`identity_changed`), so this is a *failed* comparison, not an unavailable one. `clean_photograph` needed nothing.
* **What this does not exercise on real photographs:** an *inconclusive* comparison never occurred in these runs (no photograph here produced one); it is covered only by unit tests with a stand-in checker
  (`test_an_inconclusive_comparison_rejects_the_candidate_and_keeps_the_original` and `test_an_inconclusive_comparison_is_uncertain_never_verified` in `enhancement/test_identity_unverified_policy.py`).
* **"Original byte-identical", precisely:** the stored upload is checked by SHA-256 before and after (identical in every case). For `original_preserved` the *returned* file is pixel-identical to the upload
  (decoded arrays are equal) but its bytes differ, because the service re-encodes its output (`output_file_bytes_equal_stored_original: false` in the JSON).
* **`verified` on an unchanged photograph** (`clean_photograph` and the second photograph in run 7: status `verified`, mode `not_applicable`) means only that the output is the upload; it is not an identity badge.

### 8.3 What the chosen combination means for accounts and for the frontend

With `fail_closed` on the adaptive engine, an account without recorded biometric consent gets **no enhancement at all for a photograph that has a face** (the original comes back with the notice "Identity could not be
verified (Biometric processing is not permitted for this account), so the photograph was left unchanged"), and a face too dark or small to compare is never enhanced. The frontend should show `identity_notice` and, for
`mode: failed_closed`, offer the consent screen. Photographs without a face are unaffected. The consent wording itself is still not written.

To use the combination (nothing here changes a default; these are environment settings):

```
ENHANCEMENT_RENDER_POLICY=adaptive_v1
QUALITY_UNDEREXPOSED_FACE_ACTION=reject
ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY=fail_closed
BIOMETRIC_CONSENT_REQUIRED=True
```

Evidence for runs 7 and 8: `docs/evaluation/evidence/2026-10-21/7-chosen-reject-failclosed-consent.json` and `.../8-chosen-reject-failclosed-no-consent.json` (full copies, with `verification.json`, in
`~/FaceCloud_verification_evidence/2026-10-21/`).
