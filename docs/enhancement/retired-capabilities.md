# Retired enhancement capabilities

Retired on **2026-10-15** at the project owner's request, to keep the model set focused (low light, denoise,
deblur, face restoration, super-resolution, identity). Four capabilities were removed; nothing replaced them
silently. Results stored before the change keep exactly what they recorded, including algorithm strings such as
`zero_dce_hybrid_v1+purescale_safmn_2x`.

The last commit that still contained all four is **`fd988cd`** (`git checkout fd988cd -- <path>` restores a file).

| Capability | What it did | File (SHA-256, bytes) | Public source | Licence |
|---|---|---|---|---|
| EDSR x2 | super-resolution of low-resolution photos (OpenCV DNN) | `EDSR_x2.pb` `585623221baa070279a0d1e7e113a4c3faba0f318ca7fdd9a65d9afc0763d9b4`, 38,490,162 | github.com/Saafke/EDSR_Tensorflow, `models/EDSR_x2.pb` | see upstream |
| PureVision 1x | compression-artefact removal | `PureVision_1x.pth` `3c3bec2111d0f7c3926f3171f37ce28e72502b744e084c566d8960c06a6a06a3`, 67,120,607 | github.com/limitlesslab/AI-upscaling-models, release `PureVision` (`1x_PureVision.pth`) | CC BY-NC-SA 4.0 |
| PureScale 2x | super-resolution | `PureScale_2x.pth` `c8fb698766f502be6409cf3cee1a69d63381c6eebf3ff5de7df6ed87f6c9a5cb`, 22,328,007 | github.com/limitlesslab/AI-upscaling-models, release `PureScale2` (`2x_SAFMN_PureScale.pth`) | CC BY-NC-SA 4.0 |
| Colourisation (SIGGRAPH17) | colourised near-grayscale photos | `colorization_siggraph17.pth` `df00044c0a4d7c3edcecf6f75437ce346a66e7a42612d9b968e1a7e17dbc6f66`, 136,787,426 | the S3 bucket named in github.com/richzhang/colorization (`siggraph17-df00044c.pth`) | BSD-2-Clause |

A restored file must hash to the value above; never change a pinned hash to accept a different file.

## What was removed

* **Code:** `enhancement/{colorization,purevision,purescale}.py`, the EDSR loader and `restore_low_resolution`, and
  the stages that served them (`purevision_restoration`, `low_resolution_restoration`,
  `purescale_super_resolution`, `colorization`) with their context fields.
* **Settings:** every `AI_RESTORATION_*`, `PUREVISION_*`, `PURESCALE_*` and `COLORIZATION_*` setting.
* **Tooling:** their rows in the model manifest, the fetch table, the health report, the preflight and validation
  scripts.
* **Tests:** the adapters' and stages' tests, rewritten onto the remaining models where the test was about pipeline
  behaviour (upscaler routing, guards, cancellation, stage reports).
* **Files:** the four checkpoints, deleted from `.runtime/models` (all public and re-downloadable).

## What new results look like

The stage list is `analyze_original, exposure_enhancement, output_guard_a, measure_post_exposure_noise,
adaptive_denoise, hd_restoration, face_restoration, sharpen, output_guard_b, face_fidelity, dynamic_style,
identity_guard, validate_output`. Only Real-ESRGAN changes the pixel grid. The `restoration`, `purevision`,
`purescale` and `colorization` blocks are no longer written.

## Kept

NAFNet (GoPro and SIDD), Real-ESRGAN, Retinexformer, Zero-DCE, GFPGAN, CodeFormer, YuNet, SFace, ArcFace and the
guards: see `docs/enhancement/optional-models.md`.
