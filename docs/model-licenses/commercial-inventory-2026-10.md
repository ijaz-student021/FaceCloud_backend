# Commercial licence inventory (2026-10)

Prepared 2026-10-15 from the upstream licence files and READMEs recorded in `model-set-2026-10.md`. **This is an engineering inventory,
not legal advice.** Nothing was purchased, no licensor was contacted, and no permission is assumed. "Unresolved" means exactly that.
Three things are separate for each model and must not be conflated: the **source code** licence, the **pretrained weights**
licence, and the **training-data** terms the weights inherit. A permissive code licence does not by itself make a checkpoint
commercially usable.

| Model | Repository | Code licence | Weights / checkpoint licence | Training-data terms | Commercial status | Permission needed | Recommendation |
|---|---|---|---|---|---|---|---|
| Retinexformer (low light) | caiyuanhao1998/Retinexformer | MIT (vendored with notice) | The released NTIRE checkpoint: no separate licence found in the repository; presumed to follow the repository | Trained on low-light benchmark data (LOL-family / challenge data): terms **not checked** | **Unresolved** for the weights and data; code is fine | Confirm checkpoint and dataset terms | Keep as the preferred low-light engine; resolve before charging customers |
| NAFNet-SIDD (denoise) | megvii-research/NAFNet | MIT | Released with the repository: no separate licence found | SIDD dataset terms **not checked** | **Unresolved** for weights/data | Confirm checkpoint and dataset terms | As above |
| NAFNet-GoPro (deblur) | megvii-research/NAFNet | MIT | As above | GoPro dataset terms **not checked** | **Unresolved** for weights/data | Confirm checkpoint and dataset terms | As above |
| Real-ESRGAN x4plus | xinntao/Real-ESRGAN | BSD-3-Clause | Released under the repository's licence | Trained on DIV2K/Flickr2K/OST: terms **not checked** | **Unresolved** for data; code is fine | Confirm dataset terms | Keep optional; only runs for genuinely small photographs |
| CodeFormer (face restoration) | sczhou/CodeFormer | **S-Lab License 1.0: non-commercial only** | Same non-commercial terms | FFHQ and others: not checked | **Not permitted** commercially | A commercial licence from the owner | **Keep disabled** (default). Blocked in production by `config/production_guard.py` |
| GFPGAN v1.4 | TencentARC/GFPGAN | Repository LICENSE (not classified); used under a private permission **not in this repository** | Same | FFHQ: not checked | **Unresolved** | The written permission, checked against the intended use | **Keep disabled** (default). Blocked in production |
| Zero-DCE | Li-Chongyi/Zero-DCE | No licence file; README says academic research, CC BY-NC 4.0 | Same | SICE: not checked | **Not permitted** unless the owner's stated approval is on file and covers the use (it is not in this repository) | The approval document | Only a fallback under non-default policies; blocked in production unless acknowledged. Do not call it licensed |
| ArcFace `w600k_r50` (InsightFace buffalo_l) | deepinsight/insightface | MIT (code) | **Non-commercial research purposes only** | WebFace600K: terms not checked | **Not permitted** commercially | A commercial licence from InsightFace | Optional identity provider only for research; production default is SFace. Blocked in production |
| RetinaFace (`detection_Resnet50_Final.pth`, via facexlib) | xinntao/facexlib | MIT | Weights come from the RetinaFace project; **their terms were not checked** | WIDER FACE: terms not checked | **Unresolved** | Confirm the weights' terms | Optional detector only; YuNet is the default. Blocked in production unless acknowledged |
| YuNet | opencv/opencv_zoo | Apache-2.0 | Apache-2.0 | Not checked | Likely fine; see `feature-18-yunet-sface-attestation.md` | None identified | Default detector |
| SFace | opencv/opencv_zoo | Apache-2.0 | Apache-2.0 | Trained on face-recognition datasets whose terms are **not checked** | Likely fine for code; data unresolved | Confirm data terms if it matters | Default identity provider |
| LPIPS (evaluation only) | richzhang/PerceptualSimilarity | BSD-2-Clause | AlexNet/VGG backbones from torchvision (BSD-3) | ImageNet-derived | Used only in benchmarks, never in the serving path | None for serving | Not a runtime dependency |

## What the code now enforces

* `config/production_guard.py::model_rule` refuses to start with `DJANGO_DEBUG=False` when CodeFormer, GFPGAN, the ArcFace provider, the
  RetinaFace detector, or (under a non-default render policy) Zero-DCE is enabled, unless `NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True` is set.
  The setting records a deliberate decision; it is not a licence. It also refuses `ADAPTIVE_DEBUG_ARTIFACTS=True`.
* Defaults keep all of these off (CodeFormer, GFPGAN) or non-routing (ArcFace, RetinaFace, Zero-DCE under `lightweight_v1`).
* **Owner's decision, 2026-11-04:** Zero-DCE is run in the deployment prepared for the defence, with `NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True` (`zero-dce-owner-decision.md`). The Zero-DCE row above is unchanged: it is still not licensed for commercial use. The acknowledgement is ONE switch for every model in the list, so enabling another of them later is a separate decision, not covered by this one.

## What a commercial release needs (owner decisions, not done here)

1. Decide the production model set. The smallest set with no known non-commercial component is: YuNet + SFace + Retinexformer + NAFNet-SIDD
   + NAFNet-GoPro + Real-ESRGAN, with face restoration off. Its remaining open questions are the *training-data* terms in the
   table, which only the owner can resolve.
2. Obtain and file written confirmation for each "Unresolved" row, and a commercial licence for any non-commercial component that the
   product would actually use. Keep the documents in the project's records (not in Git if they are confidential).
3. Until then the system is academic-use only, as `model-set-2026-10.md` states.
