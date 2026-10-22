# Search by what is in the photograph: model and licence record

Owner approval: pending

> Change the line above to `Owner approval: approved` only after you have read this record and decided that this model may be used. Until then
> `python manage.py check` refuses `SEMANTIC_SEARCH_ENABLED=True` (`search.E005`). This file is the engineering record of what was checked; it is not legal advice
> and it makes no licence decision for the project owner.

Checked on 2026-10-22. Every statement below was read from the source named next to it on that day; what could not be checked is listed at the end.

## The model that is recommended

| | |
|---|---|
| Model | SigLIP ViT-B/16 (224 px), image and text towers, packaged for open_clip as `timm/ViT-B-16-SigLIP` |
| Source | https://huggingface.co/timm/ViT-B-16-SigLIP , revision `41f575766f40e752fdd1383e9565b7f02388c1c4` (last modified 2023-10-25) |
| Licence stated by the source | `apache-2.0` (model metadata: `license:apache-2.0`) |
| Paper / training data named by the source | "Sigmoid Loss for Language Image Pre-Training" (arXiv 2303.15343); dataset WebLI |
| Intended-use / out-of-scope text in the model card | none (the card has usage examples only) |
| Languages | not stated by the card; used here for **English queries only** |
| Parameters | 203.2 million (measured) |

### Files used (downloaded once, then loaded from a local folder only)

| File | Bytes | SHA-256 |
|---|---|---|
| `open_clip_model.safetensors` (the weights; **this is the checksum the application verifies**) | 812,658,408 | `81942ea8c09b9e41963357cca5d2682118bf7eb7491c2ae51a28bc4f4f95b194` |
| `open_clip_config.json` | 881 | `e96434a839915fd38980a009ec47cfebda2422700273c0d455e52817047ebb0f` |
| `tokenizer.json` | 2,422,934 | `83051c8005acc696637fe0c62c711ecee4b59083b4cf07ff9ad5f637eb2a3d2a` |
| `tokenizer_config.json` | 20,608 | `8571e0cf70f7ae095c5c544ab94b7967e94c79262202d61604da10cbe426cecb` |
| `special_tokens_map.json` | 2,200 | `3a60d3bb0808e7e629845031c2d720d33c2aceee1a6c535255de15d45b9f1ac7` |

To install: download exactly that revision into `SEMANTIC_SEARCH_MODEL_DIR` (for example
`huggingface_hub.snapshot_download("timm/ViT-B-16-SigLIP", revision="41f575766f40e752fdd1383e9565b7f02388c1c4", local_dir=...)`), set
`SEMANTIC_SEARCH_MODEL_SHA256` to the weights checksum above, install `requirements-semantic.txt`, set `HF_HUB_OFFLINE=1`. The application loads from the folder only and
refuses weights whose SHA-256 differs.

## The libraries it needs (`requirements-semantic.txt`, optional, not imported while the feature is off)

| Package | Version tested | Licence (from the installed package metadata) |
|---|---|---|
| open_clip_torch | 3.3.0 | MIT |
| transformers | 5.20.2 | Apache-2.0 |
| tokenizers | 0.23.3 | Apache-2.0 (classifier) |
| sentencepiece | 0.2.2 | Apache-2.0 |
| protobuf | 7.36.2 | BSD-3-Clause |
| timm | 1.0.30 | Apache-2.0 |
| ftfy | 6.3.1 | Apache-2.0 |
| regex | 2026.9.29 | Apache-2.0 AND CNRI-Python |
| safetensors | 0.8.0 | Apache-2.0 |
| huggingface_hub | 2.2.0 | Apache-2.0 |

PyTorch and torchvision are already required by the enhancement models. `pip-audit -r requirements-semantic.txt --no-deps` reported no known vulnerabilities on 2026-10-22.

## Models that were looked at and not recommended

| Model | What the source says | Why not |
|---|---|---|
| OpenAI CLIP ViT-B/32 (github.com/openai/CLIP `model-card.md`) | "Any deployed use case of the model - whether commercial or not - is currently out of scope." The card names no licence for the weights. | Its own card excludes deployed use. |
| LAION CLIP ViT-B/32 laion2B (`laion/CLIP-ViT-B-32-laion2B-s34B-b79K`) | Metadata `license: mit`, but the card repeats the same out-of-scope sentence and says the uncurated training data is "not recommended for creating ready-to-go industrial products". | Same exclusion, plus the data warning. |

## What a deployment must keep true

- The model runs locally; nothing is downloaded at run time.
- The vectors describe whole pictures. They are **not face templates**: they are stored apart from the face embeddings, are not built from face crops, are not in the data
  export, and erasing a person's facial data does not touch them (it is not biometric data; they go when the photograph or the account goes).
- A query's text is not logged.
- Queries are English. Other languages return poor results; the screen should say so.
- Results are suggestions ranked by similarity, with a minimum score; the measured quality and its limits are in `docs/evaluation/2026-10-22-semantic-search.md`.
- A web-trained model reflects biases of its training data. It is used here only to rank a person's own photographs for that person.

## What was not verified

- The model repository contains no separate `LICENSE` file; the Apache-2.0 statement is in the model metadata, and the original release by Google (big_vision) was not read.
- Nothing here says WebLI-trained weights are free of third-party claims; Apache-2.0 is the licence the publisher states.
- Legal advice was not taken. The owner decides whether this is enough for their use.

## Review

Revalidate when the model file, its checksum, its source, the libraries, or the way it is used changes.
