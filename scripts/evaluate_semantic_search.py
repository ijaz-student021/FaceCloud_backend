"""Measure search by what is in the photograph on a labelled public set, with the same loader the server uses.

    python scripts/evaluate_semantic_search.py MODEL_DIR SHA256 DATASET_ROOT OUT_JSON [--limit N] [--edge 256]

* ``MODEL_DIR`` / ``SHA256``: the folder and the recorded checksum of the weights (``OpenClipEmbedder`` refuses a file that does not match).
* ``DATASET_ROOT``: where torchvision keeps (or downloads) the Oxford-IIIT Pet test split, 3,669 photographs of 37 breeds (25 dogs, 12 cats).
* Each photograph is first shrunk to a JPEG with its longest edge at ``--edge`` (what ``thumbnails.render_thumbnail`` does on the server), then embedded.
* The queries are the words a person would type: ``dog``, ``cat`` and the 37 breed names, in lower case, no prompt template.

It reports precision at 10 and 50, average precision, whether the first result is right, how scores of right and wrong pairs are distributed (to choose
``SEMANTIC_SEARCH_MIN_SCORE``), and the time per photograph, per query and per ranking. Nothing here is imported by the application.
"""
import argparse
import io
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search.semantic.embedders import OpenClipEmbedder  # noqa: E402

CATS = {"Abyssinian", "Bengal", "Birman", "Bombay", "British Shorthair", "Egyptian Mau", "Maine Coon", "Persian", "Ragdoll", "Russian Blue", "Siamese", "Sphynx"}


def shrink(image, edge):
    """The picture as the server hands it to the model: longest edge ``edge``, re-encoded as JPEG."""
    image = image.convert("RGB")
    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85, optimize=True)
    return Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")


def average_precision(relevant_in_rank_order):
    hits, total = 0, 0.0
    for position, relevant in enumerate(relevant_in_rank_order, start=1):
        if relevant:
            hits += 1
            total += hits / position
    return total / hits if hits else 0.0


def evaluate(scores, labels, positives_of):
    """``scores`` is ``(queries, images)``; ``positives_of[q]`` is a boolean mask of the images that show what query ``q`` asks for."""
    rows = []
    for q in range(scores.shape[0]):
        order = np.argsort(-scores[q])
        relevant = positives_of[q][order]
        rows.append({
            "precision_at_10": float(relevant[:10].mean()), "precision_at_50": float(relevant[:50].mean()),
            "average_precision": average_precision(relevant), "first_is_right": bool(relevant[0]), "positives": int(positives_of[q].sum()),
        })
    return rows


def mean(rows, key):
    return round(float(np.mean([row[key] for row in rows])), 4)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("model_dir")
    parser.add_argument("sha256")
    parser.add_argument("dataset_root")
    parser.add_argument("out")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--edge", type=int, default=256)
    parser.add_argument("--batch", type=int, default=32)
    arguments = parser.parse_args()

    from torchvision.datasets import OxfordIIITPet

    dataset = OxfordIIITPet(root=arguments.dataset_root, split="test", target_types="category", download=False)
    total = len(dataset) if not arguments.limit else min(arguments.limit, len(dataset))
    labels = np.array(dataset._labels[:total])
    names = dataset.classes

    started = time.perf_counter()
    embedder = OpenClipEmbedder(arguments.model_dir, arguments.sha256, "ViT-B-16-SigLIP")
    embedder.embed_texts(["warm up"])
    load_seconds = time.perf_counter() - started

    pictures_seconds, decode_seconds, vectors = 0.0, 0.0, []
    for start in range(0, total, arguments.batch):
        t0 = time.perf_counter()
        batch = [shrink(Image.open(dataset._images[i]), arguments.edge) for i in range(start, min(start + arguments.batch, total))]
        t1 = time.perf_counter()
        vectors.append(embedder.embed_images(batch))
        t2 = time.perf_counter()
        decode_seconds += t1 - t0
        pictures_seconds += t2 - t1
        if (start // arguments.batch) % 20 == 0:
            print(f"  {min(start + arguments.batch, total)}/{total}", flush=True)
    images = np.concatenate(vectors)
    # float16 storage, as the server keeps it
    images = np.asarray(images.astype("<f2"), dtype=np.float32)

    queries = ["dog", "cat"] + [name.lower() for name in names]
    is_cat = np.array([names[label] in CATS for label in labels])
    positives = [~is_cat, is_cat] + [labels == index for index in range(len(names))]
    t0 = time.perf_counter()
    text = embedder.embed_texts(queries)
    text_seconds = (time.perf_counter() - t0) / len(queries)
    t0 = time.perf_counter()
    scores = text @ images.T
    for q in range(len(queries)):
        np.argsort(-scores[q])[:24]
    rank_seconds = (time.perf_counter() - t0) / len(queries)

    rows = evaluate(scores, labels, positives)
    species, breeds = rows[:2], rows[2:]
    positive_scores = np.concatenate([scores[q][positives[q]] for q in range(2, len(queries))])
    negative_scores = np.concatenate([scores[q][~positives[q]] for q in range(2, len(queries))])
    by_floor = {}
    for floor in (0.0, 0.03, 0.05, 0.08, 0.10, 0.12, 0.15):
        kept_positive = float((positive_scores >= floor).sum())
        kept_negative = float((negative_scores >= floor).sum())
        by_floor[str(floor)] = {
            "precision": round(kept_positive / max(kept_positive + kept_negative, 1), 4), "recall": round(kept_positive / len(positive_scores), 4),
        }
    result = {
        "model_dir": Path(arguments.model_dir).name, "weights_sha256": arguments.sha256, "photographs": total, "breeds": len(names), "edge": arguments.edge,
        "device": embedder._device, "machine": platform.platform(), "queries": len(queries),
        "species_queries": {"n": 2, **{key: mean(species, key) for key in ("precision_at_10", "precision_at_50", "average_precision")}, "first_is_right": mean(species, "first_is_right")},
        "breed_queries": {"n": len(breeds), **{key: mean(breeds, key) for key in ("precision_at_10", "precision_at_50", "average_precision")}, "first_is_right": mean(breeds, "first_is_right")},
        "worst_breeds_by_average_precision": sorted(
            [(queries[2 + i], round(row["average_precision"], 3)) for i, row in enumerate(breeds)], key=lambda item: item[1])[:5],
        "score_percentiles_right_pairs": {str(p): round(float(np.percentile(positive_scores, p)), 4) for p in (5, 25, 50, 75, 95)},
        "score_percentiles_wrong_pairs": {str(p): round(float(np.percentile(negative_scores, p)), 4) for p in (50, 90, 99, 99.9)},
        "pooled_by_score_floor_breed_queries": by_floor,
        "seconds": {
            "model_load_and_first_text": round(load_seconds, 1), "decode_and_shrink_per_photo_ms": round(1000 * decode_seconds / total, 1),
            "embed_per_photo_ms": round(1000 * pictures_seconds / total, 1), "embed_one_query_ms": round(1000 * text_seconds, 1),
            "rank_query_over_all_photos_ms": round(1000 * rank_seconds, 2),
        },
    }
    Path(arguments.out).write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
