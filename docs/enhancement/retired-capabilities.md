# Retired enhancement capabilities

Retired on **2026-10-15**. FaceCloud enhances with OpenCV and Pillow only
(`lightweight_v1`); the optional OpenAI advisor (docs/enhancement/openai-advisor.md)
is a remote service and needs no local checkpoint. This page records what was
retired, why it was safe, what was kept, and how to bring any of it back.

Last commit that still contained all of it: **`1074c48`**
(`git checkout 1074c48 -- <path>` restores any file below).

## Retired capabilities

| Capability | What it did | Files (SHA-256, bytes) | Upstream source (the checkpoint was public unless noted) |
|---|---|---|---|
| Zero-DCE | learned exposure curves for dark photos | `zero_dce_epoch99.onnx` `e5cb6f921c7997f89105f6aa86242f30ff3f5379f5c6cd5282eb2aada14dae14`, 320,301 (exported from the official `Epoch99.pth` `a4395acb874f320375d9704997cef874eaaaaa26a1777ceb29a92b70f74c3612`, 320,017; both archived, see below) | github.com/Li-Chongyi/Zero-DCE, `Zero-DCE_code/snapshots/Epoch99.pth`; export with PyTorch 2.13.0, TorchScript exporter, opset 17 (script in git history: `scripts/export_zero_dce_onnx.py`) |
| EDSR x2 | super-resolution of low-resolution photos | `EDSR_x2.pb` `585623221baa070279a0d1e7e113a4c3faba0f318ca7fdd9a65d9afc0763d9b4`, 38,490,162 | github.com/Saafke/EDSR_Tensorflow, `models/` |
| NAFNet (GoPro, width 64) | deblurring | `NAFNet-GoPro-width64.pth` `329d3ab4077b8d6b7ff61de376e483714667960bf85be027bf4335cda701196f`, 271,778,961 | the Google Drive link in github.com/megvii-research/NAFNet `docs/GoPro.md` |
| Real-ESRGAN x4plus | HD upscaling | `RealESRGAN_x4plus.pth` `4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1`, 67,040,989 | github.com/xinntao/Real-ESRGAN, release v0.1.0 |
| GFPGAN v1.4 | generative face restoration (with its identity gate) | `GFPGANv1.4.pth` `e2cd4703ab14f4d01fd1383a8a8b266f9a5833dacee8e6a79d3bf21a1b6be5ad`, 348,632,874; exclusive helpers `detection_Resnet50_Final.pth` `6d1de9c2944f2ccddca5f5e010ea5ae64a39845a86311af6fdf30841b0a5a16d`, 109,497,761 and `parsing_parsenet.pth` `3d558d8d0e42c20224f13cf5a29c79eba2d59913419f945545d8cf7b72920de2`, 85,331,193 | github.com/TencentARC/GFPGAN, release v1.3.0; the two helpers: github.com/xinntao/facexlib releases v0.1.0 and v0.2.2 |
| PureVision 1x | compression-artefact removal | `PureVision_1x.pth` `3c3bec2111d0f7c3926f3171f37ce28e72502b744e084c566d8960c06a6a06a3`, 67,120,607 | github.com/limitlesslab/AI-upscaling-models, release `PureVision` (`1x_PureVision.pth`); CC BY-NC-SA 4.0 |
| PureScale 2x | super-resolution | `PureScale_2x.pth` `c8fb698766f502be6409cf3cee1a69d63381c6eebf3ff5de7df6ed87f6c9a5cb`, 22,328,007 | github.com/limitlesslab/AI-upscaling-models, release `PureScale2` (`2x_SAFMN_PureScale.pth`); CC BY-NC-SA 4.0 |
| Colourisation (SIGGRAPH17) | automatic colourisation of near-grayscale photos | `colorization_siggraph17.pth` `df00044c0a4d7c3edcecf6f75437ce346a66e7a42612d9b968e1a7e17dbc6f66`, 136,787,426 | the S3 bucket named in github.com/richzhang/colorization (`siggraph17-df00044c.pth`); BSD-2-Clause |

A file restored from upstream must hash to the value above; never change a
pinned hash to accept a different file. (The earlier manifest is
`git show 1074c48:docs/gpu/model_manifest.json`.) `facecloud_curve_best_opencv.onnx` (the private curve
model) had no public source and was already absent from this checkout.

## What was removed

* **Code:** `enhancement/{zero_dce,gfpgan,hd_restoration,purescale,purevision,colorization,runtime}.py`, the stages
  and helpers that served them in `enhancement/services.py` (EDSR, the
  face-restoration gate/withdraw/reconcile logic, learned-strength guards,
  chroma-gain after Zero-DCE), `enhancement/management/commands/check_gfpgan.py`,
  and the `learned_v1` rendering policy.
* **Settings:** every `ZERO_DCE_*`, `AI_RESTORATION_*`, `HD_RESTORATION_*`,
  `NAFNET_*`, `REALESRGAN_*`, `GFPGAN_*`, `PUREVISION_*`, `PURESCALE_*`,
  `COLORIZATION_*` setting, `ENHANCEMENT_RENDER_POLICY`, `ENHANCEMENT_MIN_TILE_SIZE`,
  `ENHANCEMENT_OOM_MAX_RETRIES`, `ENHANCEMENT_DARK_COLOUR_GAIN_MAX`,
  `EXTREME_LOW_LIGHT_MODEL_STRENGTH`, `ENHANCEMENT_MODEL_LUMINANCE_*`, and the
  `gfpgan` Celery queue routing. A leftover one in the environment is reported by
  `manage.py check` as `enhancement.W001`.
* **Deployment:** the `gfpgan` Docker target and compose worker,
  `requirements-gfpgan.txt`, `requirements-gfpgan-constraints.txt`,
  `scripts/start_gfpgan_worker.*`, and the isolated Python 3.12 worker environment
  (`.runtime/venvs/gfpgan`, 1.3 GB). Enhancement runs on the default queue.
* **Scripts:** `export_zero_dce_onnx.py` (and `requirements-model-export.txt`, its conversion-only dependencies), `validate_models.py`, `gpu_preflight.py`,
  `measure_upscaler_memory.py`, `evaluate_gpu_stages.py` (its still-useful helpers
  moved to `scripts/evaluation_common.py`).
* **Tests:** the adapters' and stages' tests (about 160 tests; the suite went from 1760 to 1620 after the new retirement tests were added), `test_runtime.py`,
  `test_comparison_real_image.py` (it asserted that Zero-DCE rescued a dark photo).
* **Docs:** the feature and provenance pages exclusive to these models, and the
  GPU session runbook. Their facts that still matter are on this page.
* **Untracked files:** the checkpoints above (950 MB), `gfpgan/weights`
  (190 MB), the worker environment (1.3 GB) and disposable evaluation runs.

## What was kept, and why

| Kept | Because |
|---|---|
| YuNet (`yunet_s_dynamic.onnx`) and SFace (`face_recognition_sface_2021dec.onnx`), their licence records (`docs/model-licenses/feature-18-yunet-sface-attestation.md`, `.runtime/licenses/identity-models.md`), and `docs/models/model_manifest.json` (now just these two) | face detection, the face-fidelity check and consented identity features still use them; neither used PyTorch |
| `identity_guard`, `face_fidelity`, `denoise_guard`, the quality guards, decoding, edit versions, previews, the advisor, Celery processing | active features |
| PyTorch is **not** in `requirements.txt` and no retained code imports it | it was only ever the worker environment's dependency for the removed models |
| `scripts/train_facecloud_curve_model.py`, `requirements-training.txt`, `notebooks/FaceCloud_Private_Model_Training_Colab.ipynb`, `docs/features/47-private-model-training.md` | **ambiguous, retained:** the private curve-model *training* work is a project deliverable and runs in Colab, not in the application. The model it produces is no longer loaded by anything. Remove them when that work is no longer needed |
| `audit-output/`, `fix-validation/`, `docs/evaluation/*` | dated evidence of earlier runs; they describe the pipeline as it was and are not edited (some now carry a note) |
| `scripts/evaluate_pipeline_cases.py`, `evaluate_enhancement_samples.py`, `evaluate_reference_quality.py`, `evaluate_advisor.py`, `check_queue_integration.py`, `check_advisor_queue.py`, `model_manifest.py` | the evaluation and operational harness of the retained pipeline |
| Historical results, saved edits and downloads | untouched; see below |

## How clients and old data are affected

* **Requests that name a retired thing are refused with the reason, never run as
  something else.** `POST …/images/{id}/compare/` with the profile
  `opencv_baseline` (it differed from `natural` only by switching Zero-DCE off) is
  a `400` that says it was retired. The capability list
  (`applied_parameters.render_policy.retired_capabilities`, `GET
  /api/system/health/` → `models.retired`) names all eight.
* **New results** no longer carry the blocks `zero_dce`, `restoration`,
  `hd_restoration`, `purevision`, `purescale`, `face_restoration`, `colorization`,
  `discarded_stage_reports`, and `feature_flags` lists only
  `adaptive_denoise_enabled`, `identity_guard_enabled`, `face_fidelity_enabled`.
  `pipeline_version` is `staged_candidate_pipeline_v5`. The stage list is
  `analyze_original, exposure_enhancement, output_guard_a,
  measure_post_exposure_noise, adaptive_denoise, sharpen, output_guard_b,
  face_fidelity, dynamic_style, identity_guard, validate_output`.
  `identity_verification` is `{status, verified, reason, notice}`;
  `identity_check_bypassed` and `identity_notice` remain in the API and are
  false/empty for every new result.
* **Existing results keep exactly what they stored**: `applied_parameters`, the
  algorithm string (e.g. `zero_dce_hybrid_v1+private_gfpgan_v1_4`), downloads,
  saved edits, and earlier comparisons (a stored `opencv_baseline` output is still
  labelled "OpenCV adaptive baseline (retired)"). Admin analytics keeps counting
  the retired model names that older results contain.
* Under the lightweight pipeline severe blur and low resolution are *reported as
  beyond it*; nothing invents detail.

## Bringing something back

Not recommended without a new decision on licensing (PureScale/PureVision are
non-commercial), identity safety (GFPGAN synthesises faces) and the lightweight
goal. If needed: restore the code from `1074c48`, download each checkpoint from the
source above and check it against the hash, recreate the isolated Python 3.12
environment from `git show 1074c48:requirements-gfpgan.txt`, and re-add the
settings from `git show 1074c48:config/settings.py`.

Archived outside the repository before deletion (not reproducible from upstream
without re-export): `~/FaceCloud_retired_2026-10-15/` holds the official
`Epoch99.pth`, both Zero-DCE ONNX exports and a copy of the previous local `.env`.
