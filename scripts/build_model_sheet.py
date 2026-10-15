"""Build the model image sheet: every model of the pipeline, on a real photo, against the clean original.

    python scripts/build_model_sheet.py --images folder_of_photos --out sheet_dir [--photos 3]
        [--device auto] [--face-photo 5]

Each real photograph is the ground truth. One controlled defect per model is applied to it, the matching
model repairs it, and the result is scored against the clean photo, so the numbers are measured, not claimed:

* low light: darkened with sensor-like noise -> Retinexformer, Zero-DCE, OpenCV gamma (PSNR, brightness error)
* noise: Gaussian noise -> NAFNet-SIDD, OpenCV non-local means (PSNR)
* blur: horizontal motion blur -> NAFNet-GoPro, OpenCV unsharp mask (PSNR)
* low resolution: 4x down then up -> Real-ESRGAN, bicubic (PSNR)
* face: blurred and JPEG-compressed face -> CodeFormer, GFPGAN (PSNR on the face, identity similarity to the
  original from SFace, so a restoration that changes who the person is shows up)

Then the sliders: the same dark photo at several positions of the low-light slider, and the full pipeline
(automatic and chosen level) on the unmodified photo. Nothing is uploaded; the page and images stay in --out.
The photos are never modified. Models that are not installed are listed as skipped, not hidden.
"""
import argparse
import html
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def psnr(reference, image):
    import cv2

    return float(cv2.PSNR(reference, image))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--photos", type=int, default=3, help="How many photos to evaluate.")
    parser.add_argument("--max-edge", type=int, default=800)
    parser.add_argument("--face-image", type=Path, default=None, help="A close portrait for the face test (default: the last photo).")
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.test.utils import override_settings

    from enhancement import model_controls
    from enhancement.services import _structural_similarity, enhance_bgr

    paths = sorted(p for p in arguments.images.iterdir() if p.suffix.lower() in SUFFIXES)
    if not paths:
        raise SystemExit(f"No images in {arguments.images}")
    arguments.out.mkdir(parents=True, exist_ok=True)

    def load(path):
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        scale = arguments.max_edge / max(image.shape[:2])
        return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image

    def save(name, image):
        cv2.imwrite(str(arguments.out / name), image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return name

    rng = np.random.default_rng(11)
    rows, skipped = [], []

    def run(label, function, *args):
        started = time.perf_counter()
        try:
            output = function(*args)
        except Exception as error:  # a missing or failing model is reported, never hidden
            skipped.append({"model": label, "reason": f"{type(error).__name__}: {str(error)[:140]}"})
            return None, 0.0
        return output, round(time.perf_counter() - started, 2)

    def add(section, photo, defect, degraded, candidates, clean, note=""):
        entry = {"section": section, "photo": photo, "defect": defect, "note": note, "images": [], "scores": []}
        entry["images"].append(("clean original", save(f"{section}_{photo}_clean.jpg", clean)))
        entry["images"].append((defect, save(f"{section}_{photo}_input.jpg", degraded)))
        entry["scores"].append({"name": defect, "psnr": round(psnr(clean, degraded), 2), "ssim": round(_structural_similarity(clean, degraded), 4), "seconds": None})
        for name, (image, seconds) in candidates.items():
            if image is None:
                continue
            entry["images"].append((name, save(f"{section}_{photo}_{name.replace(' ', '_')}.jpg", image)))
            entry["scores"].append({"name": name, "psnr": round(psnr(clean, image), 2), "ssim": round(_structural_similarity(clean, image), 4), "seconds": seconds,
                                    "luminance": round(float(image.mean()), 1)})
        rows.append(entry)

    # Models are called directly on a uniform footing: the adapters, not the pipeline, are under test here.
    from enhancement import codeformer as codeformer_module
    from enhancement import hd_restoration, lowlight
    from enhancement.gfpgan import restore_faces
    from enhancement.zero_dce import enhance_with_zero_dce

    chosen = paths[: arguments.photos]
    for index, path in enumerate(chosen):
        clean = load(path)
        name = f"p{index + 1}"

        dark = np.clip(clean.astype(np.float32) * 0.25 + rng.normal(0, 2.0, clean.shape), 0, 255).astype(np.uint8)
        add("lowlight", name, "dark input (25 % light + noise)", dark, {
            "Retinexformer": run("Retinexformer", lambda: lowlight.enhance_with_retinexformer(dark)[0]),
            "Zero-DCE": run("Zero-DCE", lambda: enhance_with_zero_dce(dark)[0]),
            "OpenCV gamma": run("OpenCV gamma", lambda: model_controls._opencv_gamma(dark)[0]),
        }, clean, "Brightness of the clean photo: %.0f" % clean.mean())

        noisy = np.clip(clean.astype(np.float32) + rng.normal(0, 14.0, clean.shape), 0, 255).astype(np.uint8)
        add("noise", name, "noisy input (sigma 14)", noisy, {
            "NAFNet-SIDD": run("NAFNet-SIDD", lambda: hd_restoration.denoise_with_nafnet(noisy)[0]),
            "OpenCV NLM": run("OpenCV NLM", lambda: cv2.fastNlMeansDenoisingColored(noisy, None, 10, 10, 7, 21)),
        }, clean)

        kernel = np.zeros((17, 17), np.float32)
        kernel[8, :] = 1 / 17
        blurred = cv2.filter2D(clean, -1, kernel)
        add("blur", name, "motion-blurred input (17 px)", blurred, {
            "NAFNet-GoPro": run("NAFNet-GoPro", lambda: hd_restoration.deblur_with_nafnet(blurred)[0]),
            "OpenCV unsharp": run("OpenCV unsharp", lambda: model_controls._opencv_unsharp(blurred)[0]),
        }, clean)

        height, width = clean.shape[:2]
        small = cv2.resize(clean, (width // 4, height // 4), interpolation=cv2.INTER_AREA)
        bicubic = cv2.resize(small, (width, height), interpolation=cv2.INTER_CUBIC)
        with override_settings(HD_RESTORATION_OUTPUT_SCALE=4):
            esrgan, seconds = run("Real-ESRGAN", lambda: hd_restoration.upscale_with_realesrgan(small)[0])
        if esrgan is not None and esrgan.shape != clean.shape:
            esrgan = cv2.resize(esrgan, (width, height), interpolation=cv2.INTER_AREA)
        add("lowres", name, "low-resolution input (4x down, bicubic up)", bicubic, {"Real-ESRGAN": (esrgan, seconds)}, clean)

    # Face restoration on the portrait chosen for it, with identity measured.
    face_path = arguments.face_image or (chosen[-1] if chosen else paths[-1])
    clean = load(face_path)
    small = cv2.resize(clean, None, fx=0.45, fy=0.45, interpolation=cv2.INTER_AREA)
    degraded = cv2.imdecode(cv2.imencode(".jpg", cv2.GaussianBlur(small, (0, 0), 1.8), [cv2.IMWRITE_JPEG_QUALITY, 25])[1], 1)
    reference = cv2.resize(clean, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_AREA)
    identity = {}  # label -> a function giving the similarity of a picture's face to the original's
    try:
        from identities.providers import ArcFaceOnnxProvider, SFaceOnnxProvider
        from validation.services import OpenCVYuNetFaceDetector

        detector = OpenCVYuNetFaceDetector()
        for label, provider in (("SFace", SFaceOnnxProvider()), ("ArcFace", ArcFaceOnnxProvider())):
            try:
                provider.healthcheck()
            except Exception as error:
                skipped.append({"model": f"identity similarity ({label})", "reason": f"{type(error).__name__}: {str(error)[:120]}"})
                continue

            def embed(picture, provider=provider):
                found = detector.detect(picture)
                x, y, w, h = found.boxes[0]
                aligned = provider.prepare_face(picture, {"x": x, "y": y, "width": w, "height": h}, found.landmarks[0])
                return provider.validate_embedding(provider.embed(aligned))

            base = embed(reference)
            identity[label] = lambda picture, base=base, embed=embed, provider=provider: round(float(provider.similarity(base, embed(picture))), 4)
    except Exception as error:
        skipped.append({"model": "identity similarity", "reason": f"{type(error).__name__}: {str(error)[:120]}"})
    candidates = {
        "CodeFormer w=0.7": run("CodeFormer", lambda: codeformer_module.restore_faces_codeformer(degraded, fidelity=0.7)[0]),
        "CodeFormer w=0.3": run("CodeFormer", lambda: codeformer_module.restore_faces_codeformer(degraded, fidelity=0.3)[0]),
        "GFPGAN": run("GFPGAN", lambda: restore_faces(degraded)[0]),
    }
    add("face", "portrait", "degraded face (blur + JPEG q25)", degraded, candidates, reference,
        "Identity similarity to the original (1.0 = same face): " + ", ".join(identity) + ". Higher is better; a restorer that changes who the person is scores lower.")
    for entry in rows[-1]["scores"]:
        image = degraded if entry["name"].startswith("degraded") else next(
            (candidates[key][0] for key in candidates if key == entry["name"]), None)
        if image is None:
            continue
        entry["identity"] = {}
        for label, measure in identity.items():
            try:
                entry["identity"][label] = measure(image)
            except Exception:
                entry["identity"][label] = None

    # The low-light slider on a real photo, then the full pipeline on the untouched photo.
    base_photo = load(chosen[0])
    dark = np.clip(base_photo.astype(np.float32) * 0.25 + rng.normal(0, 2.0, base_photo.shape), 0, 255).astype(np.uint8)
    strip = []
    for value in (0, 25, 50, 75, 100):
        with override_settings(RETINEXFORMER_ENABLED=True):
            image, report, _reasons = model_controls.apply_model_controls(dark, {"lowlight": value})
        strip.append((f"low-light slider {value}", save(f"slider_lowlight_{value}.jpg", image), round(float(image.mean()), 1),
                      report[0]["engine"] if report else "(untouched)"))
    pipeline = []
    for label, flags, level in (
        ("default policy, automatic", {"ENHANCEMENT_RENDER_POLICY": "lightweight_v1"}, "auto"),
        ("learned policy, all models, automatic", {"ENHANCEMENT_RENDER_POLICY": "learned_v1", "RETINEXFORMER_ENABLED": True,
         "NAFNET_DENOISE_ENABLED": True, "NAFNET_DEBLUR_ENABLED": True, "HD_RESTORATION_ENABLED": False, "CODEFORMER_ENABLED": True}, "auto"),
    ):
        with override_settings(**flags):
            started = time.perf_counter()
            output, _applied, parameters = enhance_bgr(dark, level)
            seconds = round(time.perf_counter() - started, 2)
        pipeline.append((label, save(f"pipeline_{len(pipeline)}.jpg", output), parameters["algorithm"], seconds,
                         round(float(output.mean()), 1)))
    save("pipeline_input_dark.jpg", dark)

    (arguments.out / "results.json").write_text(json.dumps({"rows": [
        {key: value for key, value in row.items() if key != "images"} for row in rows], "skipped": skipped,
        "sliders": strip, "pipeline": pipeline}, indent=2, default=str))

    def figure(label, file, extra=""):
        return (f'<figure><img src="{html.escape(file)}" alt=""><figcaption><b>{html.escape(label)}</b>{extra}</figcaption></figure>')

    sections = []
    titles = {"lowlight": "Low light", "noise": "Noise", "blur": "Blur", "lowres": "Low resolution", "face": "Face restoration"}
    for key, title in titles.items():
        block = [f"<h2>{title}</h2>"]
        for row in [r for r in rows if r["section"] == key]:
            score = {item["name"]: item for item in row["scores"]}
            figures = "".join(
                figure(label, file, "<br>PSNR {:.2f} dB · SSIM {:.3f}{}{}".format(
                    score[label]["psnr"], score[label]["ssim"],
                    f" · {score[label]['seconds']} s" if score[label].get("seconds") else "",
                    (" · identity " + ", ".join(f"{k} {v}" for k, v in score[label]["identity"].items() if v is not None))
                    if score[label].get("identity") else ""))
                for label, file in row["images"] if label in score
            ) + figure("clean original", row["images"][0][1])
            block.append(f"<h3>{html.escape(row['photo'])} — {html.escape(row['defect'])}</h3><div class='row'>{figures}</div>"
                         f"<p class='note'>{html.escape(row['note'])}</p>")
        sections.append("".join(block))
    sections.append("<h2>The low-light slider (Retinexformer)</h2><div class='row'>" + "".join(
        figure(label, file, f"<br>brightness {lum} · engine {engine}") for label, file, lum, engine in strip) + "</div>")
    sections.append("<h2>The full pipeline on the dark photo</h2><div class='row'>" + figure("dark input", "pipeline_input_dark.jpg") +
                    "".join(figure(label, file, f"<br>{html.escape(algorithm)} · {seconds} s · brightness {lum}")
                            for label, file, algorithm, seconds, lum in pipeline) + "</div>")
    if skipped:
        sections.append("<h2>Skipped</h2><ul>" + "".join(
            f"<li>{html.escape(item['model'])}: {html.escape(item['reason'])}</li>" for item in skipped) + "</ul>")
    page = f"""<!doctype html><meta charset="utf-8"><title>FaceCloud model sheet</title>
<style>body{{font:15px system-ui;margin:24px;background:#111;color:#eee}}h1,h2,h3{{font-weight:600}}
.row{{display:flex;flex-wrap:wrap;gap:12px}}figure{{margin:0;width:230px}}img{{width:100%;border-radius:6px}}
figcaption{{font-size:12.5px;color:#bbb;margin-top:4px}}.note{{color:#999;font-size:13px}}</style>
<h1>FaceCloud: every model on a real photo, scored against the clean original</h1>
<p>Each defect is applied to the real photograph and repaired by the model built for it. PSNR is against the clean photo
(higher is better; it favours blur, so read it beside the images). Everything ran locally; nothing was uploaded.</p>
{''.join(sections)}"""
    (arguments.out / "index.html").write_text(page)

    # One contact sheet for a slide or a printout: the first photo, one row per model family.
    tiles = []
    for key in titles:
        row = next((r for r in rows if r["section"] == key), None)
        if row:
            line = [cv2.resize(cv2.imread(str(arguments.out / file)), (300, 400)) for _label, file in row["images"]]
            for tile, (label, _file) in zip(line, row["images"]):
                cv2.putText(tile, label[:34], (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)
            tiles.append(line)
    width = max(len(line) for line in tiles) if tiles else 0
    blank = np.zeros((400, 300, 3), np.uint8)
    if tiles:
        cv2.imwrite(str(arguments.out / "sheet.jpg"), np.vstack([np.hstack(line + [blank] * (width - len(line))) for line in tiles]),
                    [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"Wrote {arguments.out / 'index.html'} and sheet.jpg; {len(skipped)} model(s) skipped.")


if __name__ == "__main__":
    main()
