# Model licences: the 2026-10 model set

Recorded 2026-10-15. These are the licences stated by each project; they are not legal advice. This project is a
final-year academic project (non-commercial). Check them again before any commercial deployment.

| Model | Licence | What it means here |
|---|---|---|
| Retinexformer (caiyuanhao1998/Retinexformer, ICCV 2023) | MIT | Free to use and vendor with the notice kept (`enhancement/archs/retinexformer.py`). |
| NAFNet GoPro and SIDD (megvii-research/NAFNet) | MIT | Free to use; the network is re-implemented in `enhancement/hd_restoration.py`. |
| Real-ESRGAN (xinntao/Real-ESRGAN) | BSD-3-Clause | Free to use. |
| Zero-DCE (Li-Chongyi/Zero-DCE) | the project owner states an approved licence exists; the document is not in this repository and was not checked here | Keep the reference with the project's records. |
| GFPGAN (TencentARC/GFPGAN) | Apache-2.0 code; used under a private permission recorded outside the repository | Unchanged from before. |
| **CodeFormer** (sczhou/CodeFormer, NeurIPS 2022) | **S-Lab License 1.0: non-commercial use only** | Fine for this academic project. Not for a commercial service. The vendored files `enhancement/archs/{codeformer,vqgan}.py` carry this notice. |
| **ArcFace** (InsightFace `w600k_r50` from the `buffalo_l` pack) | **InsightFace pretrained models: non-commercial research use only** | Fine for this academic project. Not for a commercial service. |
| RetinaFace (facexlib `detection_Resnet50_Final.pth`) | the facexlib licence (MIT code); weights from the RetinaFace project | Used by the optional detector provider. |
| YuNet, SFace (OpenCV Zoo) | Apache-2.0 / MIT | Unchanged: `docs/model-licenses/feature-18-yunet-sface-attestation.md`. |

CodeFormer's licence text (S-Lab License 1.0) permits redistribution and use for non-commercial purposes with the
copyright notice and licence retained. The checkpoints are not in Git; `scripts/fetch_models.py` downloads each from
its public source and checks its pinned SHA-256.
