"""Blur-measure study: which measure separates sharp from blurred photos regardless of
resolution and exposure? Photos are split into calibration and evaluation halves by a
hash of the file name. Labels: an original photo is 'sharp' unless reviewed otherwise;
synthetic blur is applied with a known size at the proxy scale."""
import glob, hashlib, json, os, sys, time
import cv2
import numpy as np

PROXY = 1024  # long edge the measures are computed at


def proxy(gray):
    h, w = gray.shape
    scale = PROXY / max(h, w)
    if scale < 1:
        gray = cv2.resize(gray, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    return gray


def laplacian_variance(gray):
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def reblur_ratio(g):
    """Crete-Roffet: how much a 9-px box re-blur reduces neighbour differences (0 sharp .. 1 blurred)."""
    g = g.astype(np.float64)
    out = []
    for axis in (0, 1):
        k = np.ones((9, 1)) / 9 if axis == 0 else np.ones((1, 9)) / 9
        b = cv2.filter2D(g, -1, k, borderType=cv2.BORDER_REFLECT)
        d = np.abs(np.diff(g, axis=axis)); db = np.abs(np.diff(b, axis=axis))
        v = np.maximum(0, d - db)
        s = d.sum()
        out.append(1 - v.sum() / s if s > 0 else 1.0)
    return float(max(out))


def edge_sharpness(g):
    """At the strongest edges: |second derivative| / |gradient|, roughly 1/edge width.
    Gain-invariant; computed only where gradients are in the top 2 %."""
    g = cv2.GaussianBlur(g.astype(np.float64), (0, 0), 0.5)
    gx = cv2.Sobel(g, cv2.CV_64F, 1, 0, ksize=3); gy = cv2.Sobel(g, cv2.CV_64F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    lap = np.abs(cv2.Laplacian(g, cv2.CV_64F, ksize=3))
    thr = np.percentile(mag, 98)
    sel = mag >= max(thr, 1e-6)
    # Laplacian is zero at the edge centre; take the max in a 3x3 neighbourhood instead.
    lapmax = cv2.dilate(lap, np.ones((3, 3)))
    return float(np.median(lapmax[sel] / mag[sel]))


def coverage(g):
    """Share of the proxy with gradients above 10 % of the robust luminance range (texture/edges)."""
    g = g.astype(np.float64)
    p5, p95 = np.percentile(g, [5, 95])
    rng = max(p95 - p5, 1.0)
    mag = np.hypot(cv2.Sobel(g, cv2.CV_64F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_64F, 0, 1, ksize=3)) / 8
    return float((mag > 0.1 * rng).mean()), float(rng)


def measures(bgr):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    g = proxy(gray)
    cov, rng = coverage(g)
    gf = g.astype(np.float64)
    strength = float(np.percentile(np.hypot(cv2.Sobel(gf, cv2.CV_64F, 1, 0, ksize=3), cv2.Sobel(gf, cv2.CV_64F, 0, 1, ksize=3)) / 8, 98))
    return {"edge_strength": strength,
        "lap_native": laplacian_variance(gray),
        "lap_proxy_norm": laplacian_variance(g) / max(float(g.astype(np.float64).var()), 1e-6),
        "reblur": reblur_ratio(g),
        "edge": edge_sharpness(g),
        "coverage": cov, "range": rng,
    }


def motion_kernel(length, angle):
    k = np.zeros((length, length)); k[length // 2, :] = 1
    rot = cv2.getRotationMatrix2D((length / 2 - 0.5, length / 2 - 0.5), angle, 1)
    k = cv2.warpAffine(k, rot, (length, length)); return k / k.sum()


def variants(img):
    """(name, label, image). Blur sizes are in proxy pixels, scaled to the image's own grid."""
    h, w = img.shape[:2]
    f = max(h, w) / PROXY if max(h, w) > PROXY else 1.0
    out = [("original", "sharp", img)]
    for long_edge in (1600, 800):
        if max(h, w) > long_edge * 1.2:
            s = long_edge / max(h, w)
            out.append((f"resized_{long_edge}", "sharp", cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)))
    for gain in (0.5, 0.25):
        out.append((f"dark_x{gain}", "sharp", np.clip(np.rint(img.astype(np.float32) * gain), 0, 255).astype(np.uint8)))
    noisy = np.clip(img.astype(np.float32) + np.random.default_rng(1).normal(0, 8, img.shape), 0, 255).astype(np.uint8)
    out.append(("noise8", "sharp", noisy))
    out.append(("jpeg30", "sharp", cv2.imdecode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 30])[1], 1)))
    for sigma, label in ((0.7, "borderline"), (1.2, "mild"), (2.0, "severe"), (3.5, "severe")):
        out.append((f"gauss{sigma}", label, cv2.GaussianBlur(img, (0, 0), sigma * f)))
    for length, label in ((5, "mild"), (11, "severe")):
        L = max(3, int(round(length * f)) | 1)
        out.append((f"motion{length}", label, cv2.filter2D(img, -1, motion_kernel(L, 30))))
    blurred = cv2.GaussianBlur(img, (0, 0), 2.0 * f)
    out.append(("gauss2.0_dark_x0.25", "severe", np.clip(np.rint(blurred.astype(np.float32) * 0.25), 0, 255).astype(np.uint8)))
    out.append(("gauss2.0_noise8", "severe", np.clip(blurred.astype(np.float32) + np.random.default_rng(2).normal(0, 8, img.shape), 0, 255).astype(np.uint8)))
    out.append(("gauss1.2_jpeg30", "mild", cv2.imdecode(cv2.imencode(".jpg", cv2.GaussianBlur(img, (0, 0), 1.2 * f), [cv2.IMWRITE_JPEG_QUALITY, 30])[1], 1)))
    return out


def split(path):
    return "calibration" if int(hashlib.sha256(os.path.basename(path).encode()).hexdigest(), 16) % 2 == 0 else "evaluation"


if __name__ == "__main__":
    files = sorted(glob.glob("/Users/Ali/Downloads/*.jp*g") + glob.glob("/Users/Ali/Downloads/*.png"))
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(files)
    rows = []
    t0 = time.time()
    for path in files[:limit]:
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None or min(img.shape[:2]) < 300:
            continue
        if max(img.shape[:2]) > 2400:
            rows.append({"file": os.path.basename(path), "split": split(path), "variant": "native", "label": "sharp",
                         "size": list(img.shape[:2]), **measures(img)})
            s = 2400 / max(img.shape[:2])
            img = cv2.resize(img, (round(img.shape[1] * s), round(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
        for name, label, var in variants(img):
            rows.append({"file": os.path.basename(path), "split": split(path), "variant": name, "label": label,
                         "size": list(var.shape[:2]), **measures(var)})
    json.dump(rows, open(sys.argv[2] if len(sys.argv) > 2 else "blur_rows.json", "w"))
    print(len(rows), "rows", round(time.time() - t0), "s")
