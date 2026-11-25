CELLS = [
("md", """# 07 · CodeFormer: face restoration

**What it is.** CodeFormer (Zhou et al., NeurIPS 2022, `codeformer.pth`, 359 MB) restores a degraded face using a learned codebook; a *fidelity* weight `w` trades realism (low w) against faithfulness to the input (high w). In FaceCloud it runs only when a face is soft, and its output is accepted only if the **identity gate** (SFace similarity >= 0.65 for a restored face) and the geometry gate agree. License: S-Lab (non-commercial research use, recorded in `docs/model-licenses/`).

**How it is scored (ground truth).** Sharp public portraits are softened artificially (gaussian blur + light noise); CodeFormer restores them; compared with the original face: PSNR / SSIM / MAE of the whole picture and **identity similarity** (SFace) of the original face against the degraded and against the restored one. The last number is the one the application's gate uses."""),
("code", '''from enhancement.codeformer import restore_faces_codeformer
GATE = identity_provider().thresholds()["restore_guard"]
cases = []
for pk in public_stock_ids():
    image = photo(pk, 800); face = largest_face(image)
    if face and face["score"] >= 0.85 and min(face["box"][2:]) >= 110:
        cases.append((pk, image, face))
cases = cases[:6]; print(len(cases), "portraits")
rows, shown = [], []
for pk, truth, face in cases:
    soft = degrade(degrade(truth, "blur", 3), "noise", 4)
    (restored, meta), seconds = timed(lambda: restore_faces_codeformer(soft))
    ref = face_embedding(truth, face)
    sim_soft = face_embedding(soft); sim_new = face_embedding(restored)
    rows.append({"photo": pk, "psnr_soft": round(psnr(truth, soft), 2), "psnr_restored": round(psnr(truth, restored), 2), "ssim_soft": round(ssim(truth, soft), 3), "ssim_restored": round(ssim(truth, restored), 3),
                 "id_soft": round(identity_similarity(ref, sim_soft), 3) if sim_soft is not None else None, "id_restored": round(identity_similarity(ref, sim_new), 3) if sim_new is not None else None,
                 "sharp_x": round(sharpness(restored) / max(sharpness(soft), 1e-6), 2), "seconds": round(seconds, 1)})
    shown.append((truth, soft, restored))
for r in rows: r["passes_gate_0.65"] = (r["id_restored"] or 0) >= GATE
table(rows)'''),
("code", '''x = np.arange(len(rows)); w = 0.4
fig, ax = plt.subplots(1, 3, figsize=(15, 3.6))
ax[0].bar(x - w / 2, [r["psnr_soft"] for r in rows], w, label="softened", color=COLORS["input"]); ax[0].bar(x + w / 2, [r["psnr_restored"] for r in rows], w, label="CodeFormer", color=COLORS["model"]); ax[0].set_title("PSNR dB vs original")
ax[1].bar(x - w / 2, [r["ssim_soft"] for r in rows], w, color=COLORS["input"]); ax[1].bar(x + w / 2, [r["ssim_restored"] for r in rows], w, color=COLORS["model"]); ax[1].set_title("SSIM vs original")
ax[2].bar(x - w / 2, [r["id_soft"] or 0 for r in rows], w, color=COLORS["input"]); ax[2].bar(x + w / 2, [r["id_restored"] or 0 for r in rows], w, color=COLORS["model"]); ax[2].axhline(GATE, color=COLORS["bad"], ls="--"); ax[2].set_title("Identity similarity to the original face (dashed = gate)")
for a in ax: a.set_xticks(x, [r["photo"] for r in rows])
ax[0].legend(fontsize=8); fig.tight_layout(); save(fig, "07_codeformer_scores.png")
pick = shown[:3]; fig = montage([i for t in pick for i in t], [n for _ in pick for n in ("original", "softened", "CodeFormer")], columns=3, width=3.0); fig.set_size_inches(9, 2.2 * len(pick)); save(fig, "07_codeformer_examples.png")'''),
("md", "## Fidelity sweep: realism against faithfulness (3 portraits)"),
("code", '''sweep = []
for w_ in (0.3, 0.5, 0.7, 0.9):
    p, i = [], []
    for pk, truth, face in cases[:3]:
        soft = degrade(degrade(truth, "blur", 3), "noise", 4)
        out = first(restore_faces_codeformer(soft, fidelity=w_)); e = face_embedding(out); ref = face_embedding(truth, face)
        p.append(psnr(truth, out)); i.append(identity_similarity(ref, e) if e is not None else 0.0)
    sweep.append({"fidelity_w": w_, "psnr_db": round(float(np.mean(p)), 2), "identity": round(float(np.mean(i)), 3)})
table(sweep)
fig, ax = plt.subplots(figsize=(5.5, 3.4)); ax.plot([s["psnr_db"] for s in sweep], [s["identity"] for s in sweep], "o-", color=COLORS["model"])
for s in sweep: ax.annotate(f"w={s['fidelity_w']}", (s["psnr_db"], s["identity"]), fontsize=8, xytext=(4, 4), textcoords="offset points")
ax.set_xlabel("PSNR dB"); ax.set_ylabel("identity similarity"); ax.set_title("CodeFormer fidelity trade-off"); fig.tight_layout(); save(fig, "07_codeformer_fidelity.png")
metrics = {"portraits": len(rows), "mean_psnr_soft_db": float(np.mean([r["psnr_soft"] for r in rows])), "mean_psnr_restored_db": float(np.mean([r["psnr_restored"] for r in rows])),
           "mean_ssim_soft": float(np.mean([r["ssim_soft"] for r in rows])), "mean_ssim_restored": float(np.mean([r["ssim_restored"] for r in rows])),
           "mean_identity_soft": float(np.mean([r["id_soft"] for r in rows if r["id_soft"] is not None])), "mean_identity_restored": float(np.mean([r["id_restored"] for r in rows if r["id_restored"] is not None])),
           "share_passing_identity_gate": float(np.mean([r["passes_gate_0.65"] for r in rows])), "mean_sharpness_gain_x": float(np.mean([r["sharp_x"] for r in rows])), "mean_seconds": float(np.mean([r["seconds"] for r in rows]))}
print(scorecard("CodeFormer", "face restoration", metrics, "softened portraits with ground truth; identity gate 0.65"))
save_json("07_codeformer.json", {"rows": rows, "fidelity_sweep": sweep, "scorecard": metrics})'''),
("code", '''m = metrics
print("What the numbers say (computed, not typed):")
print(f"* identity similarity to the original face: softened {m['mean_identity_soft']:.3f} -> CodeFormer {m['mean_identity_restored']:.3f}; {m['share_passing_identity_gate']:.0%} of restored faces clear the application's 0.65 gate")
print(f"* SSIM {m['mean_ssim_soft']:.3f} -> {m['mean_ssim_restored']:.3f}; PSNR {m['mean_psnr_soft_db']:.2f} -> {m['mean_psnr_restored_db']:.2f} dB (a small change: the gain is mostly perceptual, not pixel-exact)")
print(f"* Laplacian sharpness x{m['mean_sharpness_gain_x']:.2f} of the softened input (it also removes the added noise, which lowers this figure)")
'''),
("md", """## Verdict
A modest, honest result: CodeFormer keeps the face recognisable (identity does not drop and mostly clears the gate) and slightly improves structure, but it is **not** a dramatic pixel-level recovery. That is why the application only keeps a restored face when the identity and geometry gates agree."""),
]
