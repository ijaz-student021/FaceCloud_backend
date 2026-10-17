"""Build a self-contained demo page for a defence: one photo, every way FaceCloud can treat it.

    python scripts/build_demo.py --image portrait.jpg --out demo_dir [--max-edge 1100]

Writes ``demo_dir/index.html`` and the images it shows (open it in a browser; no server needed). Sections:

1. The clean photo through the default policy: nothing is changed because nothing is wrong.
2. A dark copy under each policy and level: the default leaves it dark in automatic mode (on purpose),
   a chosen level lifts it, ``balanced_v1`` lifts it with Zero-DCE on the CPU, ``learned_v1`` shows the
   optional models switched fully on.
3. A noisy dark copy: the effect of the pre-filter, at 1:1 crops.
4. Honest limits: blur, JPEG artefacts and overexposure are not fixed by the lightweight pipeline.
5. The model inventory: every optional model, whether its file is installed and verified, and why it is off.

Nothing is uploaded; the photo stays on this machine. The page states measured numbers, not claims.
"""
import argparse
import html
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def darken(clean, factor, seed=7):
    import numpy as np

    rng = np.random.default_rng(seed)
    dark = np.clip(clean.astype(np.float32) * factor, 0, 255)
    return np.clip(dark + rng.normal(0, 1, dark.shape) * (1.5 + 0.35 * np.sqrt(dark)), 0, 255).astype(np.uint8)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-edge", type=int, default=1100)
    parser.add_argument("--darkness", type=float, default=0.28)
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.test.utils import override_settings

    from enhancement.services import analyze_image_quality, enhance_bgr
    from operations.services import check_models

    clean = cv2.imread(arguments.image, cv2.IMREAD_COLOR)
    if clean is None:
        raise SystemExit(f"Cannot read {arguments.image}")
    longest = max(clean.shape[:2])
    if longest > arguments.max_edge:
        scale = arguments.max_edge / longest
        clean = cv2.resize(clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    arguments.out.mkdir(parents=True, exist_ok=True)
    pictures = []  # (section, filename, caption)

    def save(name, image):
        cv2.imwrite(str(arguments.out / name), image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return name

    def run(image, policy, level, prefilter="chroma", style="natural"):
        with override_settings(ENHANCEMENT_RENDER_POLICY=policy, ENHANCEMENT_PREFILTER_MODE=prefilter):
            output, applied, parameters = enhance_bgr(image, level, style=style)
        before, after = parameters["quality_before"]["mean_luminance"], parameters["quality_after"]["mean_luminance"]
        note = ""
        if parameters["plan"].get("routing"):
            note = " (lift withheld: automatic mode does not guess that a dark photo should be brighter)" \
                if parameters["plan"]["routing"][0]["decision"] == "lift_withheld" else ""
        return output, f"{policy}, level {level}: brightness {before:.0f} to {after:.0f}; {parameters['algorithm']}{note}"

    # 1. clean photo
    output, caption = run(clean, "lightweight_v1", "auto")
    unchanged = bool(np.array_equal(output, clean))
    pictures.append(("1", save("1_clean_input.jpg", clean), "The photo as supplied"))
    pictures.append(("1", save("1_clean_default.jpg", output),
                     f"Default pipeline, automatic: {'pixel-identical, nothing needed fixing' if unchanged else caption}"))

    # 2. dark copy under each policy
    dark = darken(clean, arguments.darkness)
    pictures.append(("2", save("2_dark_input.jpg", dark), f"Dark copy ({arguments.darkness:.0%} brightness)"))
    for index, (policy, level) in enumerate((
        ("lightweight_v1", "auto"), ("lightweight_v1", "high"), ("balanced_v1", "auto"), ("balanced_v1", "high"),
        ("learned_v1", "auto"),
    )):
        output, caption = run(dark, policy, level)
        pictures.append(("2", save(f"2_dark_{index}.jpg", output), caption))
    pictures.append(("2", save("2_dark_clean.jpg", clean), "For reference: the clean photo"))

    # 3. pre-filter at 1:1
    height, width = dark.shape[:2]
    top, left = height // 3, width // 3
    window = (slice(top, top + 260), slice(left, left + 360))
    plain, _ = run(dark, "lightweight_v1", "high", prefilter="off")
    filtered, _ = run(dark, "lightweight_v1", "high", prefilter="chroma")
    pictures.append(("3", save("3_prefilter_off.jpg", plain[window]), "Lift without the pre-filter (1:1 crop)"))
    pictures.append(("3", save("3_prefilter_on.jpg", filtered[window]), "Lift with the pre-filter (same crop)"))

    # 4. limits
    blurred = cv2.GaussianBlur(clean, (0, 0), 4.0)
    ok, encoded = cv2.imencode(".jpg", clean, [cv2.IMWRITE_JPEG_QUALITY, 12])
    compressed = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    over = np.clip(clean.astype(np.float32) * 1.9 + 40, 0, 255).astype(np.uint8)
    for key, label, image in (("blurred", "Blurred", blurred), ("compressed", "JPEG quality 12", compressed), ("over", "Overexposed", over)):
        output, caption = run(image, "lightweight_v1", "auto")
        pictures.append(("4", save(f"4_{key}_in.jpg", image), f"{label}: input"))
        same = "unchanged" if np.array_equal(output, image) else "lightly changed"
        pictures.append(("4", save(f"4_{key}_out.jpg", output), f"{label}: default pipeline, {same}. Detail that is gone is not invented."))

    with override_settings(ENHANCEMENT_RENDER_POLICY="lightweight_v1"):
        health = check_models()
    rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{html.escape(str(entry.get('status')))}</td>"
        f"<td>{'yes' if entry.get('installed', entry.get('status') == 'healthy') else 'no'}</td>"
        f"<td>{'yes' if entry.get('checksum_verified', entry.get('status') == 'healthy') else 'no'}</td>"
        f"<td>{html.escape(entry.get('reason', 'always used (face detection / identity)' if entry.get('status') == 'healthy' else ''))}</td></tr>"
        for name, entry in health["models"].items()
    )
    quality = analyze_image_quality(clean)
    titles = {
        "1": "1. A good photo is left alone",
        "2": "2. A dark photo: what each policy does",
        "3": "3. Noise before a lift (the pre-filter)",
        "4": "4. What the lightweight pipeline does not fix",
    }
    sections = []
    for key, title in titles.items():
        figures = "".join(
            f'<figure><img src="{name}" alt=""><figcaption>{html.escape(caption)}</figcaption></figure>'
            for section, name, caption in pictures if section == key
        )
        sections.append(f"<h2>{title}</h2><div class='row'>{figures}</div>")
    page = f"""<!doctype html><meta charset="utf-8"><title>FaceCloud enhancement demo</title>
<style>body{{font:15px system-ui;margin:24px;background:#111;color:#eee}}h1,h2{{font-weight:600}}
.row{{display:flex;flex-wrap:wrap;gap:12px}}figure{{margin:0;max-width:340px}}img{{width:100%;border-radius:6px}}
figcaption{{font-size:12.5px;color:#bbb;margin-top:4px}}table{{border-collapse:collapse}}td,th{{border:1px solid #444;padding:4px 10px}}
</style><h1>FaceCloud: lightweight enhancement and the optional models</h1>
<p>Default policy <b>lightweight_v1</b> (OpenCV and Pillow only). Source photo: {clean.shape[1]}x{clean.shape[0]}, mean brightness
{quality['mean_luminance']:.0f}. Everything below ran on this CPU, on this machine, with nothing uploaded.</p>
{''.join(sections)}
<h2>5. The models: installed, verified, and off by default</h2>
<table><tr><th>Model</th><th>Status</th><th>Installed</th><th>Checksum verified</th><th>Reason</th></tr>{rows}</table>
<p>Switch policy with <code>ENHANCEMENT_RENDER_POLICY</code> = <code>lightweight_v1</code> | <code>balanced_v1</code> (adds Zero-DCE on the CPU
when a level is chosen) | <code>learned_v1</code> (every model whose setting is on).</p>"""
    (arguments.out / "index.html").write_text(page)
    print(f"Wrote {arguments.out / 'index.html'}")


if __name__ == "__main__":
    main()
