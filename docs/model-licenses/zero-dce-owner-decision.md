# Zero-DCE: the owner's decision to run it (2026-11-04)

**Decision.** On 2026-11-04 the project owner decided, in the working session, to **run and enable Zero-DCE** in the deployment prepared for the defence (2026-11-10): `ZERO_DCE_ENABLED=True` and
`NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True` in `docs/deployment/vps.env.example`.

**What it is not.** It is not a licence. The official Zero-DCE weights (`Li-Chongyi/Zero-DCE`, `Epoch99.pth`, exported to `zero_dce_epoch99.onnx`, provenance in `zero-dce-provenance.md`) carry
no licence file and the README says academic research, **CC BY-NC 4.0**. The row for Zero-DCE in `commercial-inventory-2026-10.md` is unchanged: for commercial use it is still **not permitted**
without the licence holder's approval. The acknowledgement records that the owner knows this and runs it for a final-year project demonstration.

**Why this model.** On the owner's three dark portraits it brightens in 2-3 s on a CPU-class path and was as good as or better than Retinexformer through the validator gates
(`docs/evaluation/2026-11-03-low-light-engines.md`); without it the low-light step falls to an OpenCV gamma (mean PSNR 13.2 against 18.7, `docs/deployment/ec2-small-cpu.md`).
The Keras re-implementation `keras-io/low-light-image-enhancement` (Apache-2.0 per its card) was downloaded and measured on 2026-11-04: it runs but over-brightens (brightness 168-207 against 104-127),
so it is not a drop-in replacement; the licence of the LOL data it was trained on is not checked.

**Scope.** Only Zero-DCE. CodeFormer, GFPGAN, ArcFace, RetinaFace and 4x-UltraSharp remain separate decisions: the production guard still refuses them unless acknowledged, and the acknowledgement
setting is one switch for all of them, so enabling any of them needs its own look (`config/production_guard.py::model_rule`).

**To revert.** `ZERO_DCE_ENABLED=False` and remove `NONCOMMERCIAL_MODELS_ACKNOWLEDGED`; the low-light step then uses the OpenCV fallback and production start-up passes without the acknowledgement.
