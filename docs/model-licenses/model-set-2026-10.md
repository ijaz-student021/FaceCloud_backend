# Model licences: the 2026-10 model set

Recorded 2026-10-15. These are the licences stated by each project; they are not legal advice. This project is a
final-year academic project (non-commercial). Check them again before any commercial deployment.

| Model | Licence (checked against the upstream project on 2026-10-15 unless noted) | What it means here |
|---|---|---|
| Retinexformer (caiyuanhao1998/Retinexformer, ICCV 2023) | MIT (GitHub licence metadata) | Free to use and vendor with the notice kept (`enhancement/archs/retinexformer.py`). |
| NAFNet GoPro and SIDD (megvii-research/NAFNet) | MIT (LICENSE file read) | Free to use; the network is re-implemented in `enhancement/hd_restoration.py`. |
| Real-ESRGAN (xinntao/Real-ESRGAN) | BSD-3-Clause (GitHub licence metadata) | Free to use. |
| **Zero-DCE** (Li-Chongyi/Zero-DCE) | **No licence file; the README states the code is for academic research only, CC BY-NC 4.0 (non-commercial).** The project owner states an approved licence covers its use; that approval is not in this repository and was not seen here | Fine for this academic project. For anything else, keep the approval document with the project's records and check it covers the use. |
| GFPGAN (TencentARC/GFPGAN) | Repository LICENSE (not classified by GitHub); used under a private permission recorded outside the repository | Unchanged from before; confirm the permission before any non-academic use. |
| **CodeFormer** (sczhou/CodeFormer, NeurIPS 2022) | **S-Lab License 1.0: non-commercial use only** (LICENSE file read) | Fine for this academic project. Not for a commercial service. The vendored files `enhancement/archs/{codeformer,vqgan}.py` carry this notice. |
| **ArcFace** (InsightFace `w600k_r50` from the `buffalo_l` pack) | **Non-commercial research purposes only** (InsightFace README: "available for non-commercial research purposes only"; the code itself is MIT) | Fine for this academic project. Not for a commercial service. |
| RetinaFace (facexlib network, `detection_Resnet50_Final.pth`) | facexlib MIT (LICENSE file read); the weights come from the RetinaFace project and their own terms were not checked | The vendored network files carry the MIT notice. Confirm the weights' terms before any non-academic use. |
| YuNet, SFace (OpenCV Zoo) | Apache-2.0 / MIT | Unchanged: `docs/model-licenses/feature-18-yunet-sface-attestation.md`. |

**Unresolved questions** (for the project owner): the Zero-DCE permission document; the GFPGAN permission; the RetinaFace weights' terms;
and that three models in the active set (CodeFormer, ArcFace, Zero-DCE) are non-commercial, so the whole system is academic-use only
as configured.

CodeFormer's licence text (S-Lab License 1.0) permits redistribution and use for non-commercial purposes with the
copyright notice and licence retained. The checkpoints are not in Git; `scripts/fetch_models.py` downloads each from
its public source and checks its pinned SHA-256.
