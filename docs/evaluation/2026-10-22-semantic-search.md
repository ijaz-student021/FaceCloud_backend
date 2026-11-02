# Search by what is in the photograph: measured on a labelled public set

Measured 2026-10-22 on an Apple M1 (macOS 27.2), with the same loader the server uses (`OpenClipEmbedder`: local folder, weights checked against the recorded SHA-256, no network).
Model: SigLIP ViT-B/16, weights `81942ea8…b194` (record: `docs/model-licenses/semantic-search-model.md`). Script: `scripts/evaluate_semantic_search.py`; raw result:
`2026-10-22-semantic-search.json`.

## What was measured, and on what

- **Set:** the Oxford-IIIT Pet test split, **3,669 photographs** of 37 breeds (25 dog breeds, 12 cat breeds), labelled by the dataset. Each photograph was first shrunk to a JPEG with
  its longest edge at 256 px (what the gallery thumbnail is), then embedded; vectors were kept as float16, as the server stores them.
- **Queries:** the words a person might type, lower case, no template: `dog`, `cat`, and the 37 breed names (39 queries). A photograph is "right" for a breed word if it shows that breed,
  and for `dog`/`cat` if it shows any breed of that animal.
- **What it says and what it does not:** it shows that named things in real photographs are ranked first, in English. It is **not** a personal photo library, not scenes ("beach",
  "sunset"), not attributes or combinations ("red car", "two children on a swing"), not other languages, and not hard negatives such as photographs with no subject. Those are unmeasured.

## Result

| | queries | precision at 10 | precision at 50 | average precision | first result right |
|---|---|---|---|---|---|
| `dog`, `cat` | 2 | 1.000 | 1.000 | 1.000 | 2 of 2 |
| the 37 breed names | 37 | 0.968 | 0.946 | 0.916 | 36 of 37 (97.3%) |

The weakest breed words: `bombay` (average precision 0.13: a black cat breed whose name is also a city and which looks like many other black cats), `ragdoll` (0.49), `american pit bull
terrier` (0.65), `birman` (0.73), `american bulldog` (0.88). Breed names are a harder test than everyday words, so these figures should not be read as the quality of every query.

## The score floor (`SEMANTIC_SEARCH_MIN_SCORE`)

The scores are cosine similarities and are small: pairs that are right have a median of 0.115 (5th to 95th percentile: 0.082 to 0.139); wrong pairs have a median of -0.004 and a 99th
percentile of 0.087. Pooled over the 37 breed queries (every photograph against every query):

| floor | share of shown results that are right | share of right photographs that are shown |
|---|---|---|
| 0.00 | 5.7% | 100% |
| 0.05 | 24.4% | 98.8% |
| 0.08 | 61.2% | 95.5% |
| **0.10 (the default)** | **86.8%** | **84.1%** |
| 0.12 | 97.3% | 34.4% |
| 0.15 | 100% | 0.4% |

The default is 0.10: it favours not showing unrelated photographs for a word the library does not contain, at the cost of dropping the weaker true matches (about one in six). It was
chosen from this same set, so its trade-off is optimistic for other libraries; lower it (`SEMANTIC_SEARCH_MIN_SCORE=0.08`) to show more.

## Speed (this machine)

| | Apple M1 GPU (MPS) | Apple M1 CPU |
|---|---|---|
| Start-up: hash 813 MB of weights, load, first use | 10.3 s | 8.1 s |
| One typed query (median of 20, slowest) | 17 ms (45 ms) | 42 ms (227 ms) |
| One photograph, in batches of 16 | 40 ms | 75 ms |
| One photograph, alone | 112 ms | 114 ms |
| Decode and shrink a photograph before embedding | about 7 ms | about 7 ms |

Indexing a library therefore costs roughly 0.05 s per photograph in a batch job (about 3 minutes for 3,669), once per photograph; a new upload is indexed by a background task after its
checks finish. Ranking a query against a person's vectors in the application (database read of the float16 vectors, decode, matrix product), SQLite, 768 dimensions:
**5,000 photographs: 12 ms; 20,000 photographs: 80 ms** (median of 5; the 20,000 case read 30 MB of vectors). A PostgreSQL deployment, a library of several hundred thousand photographs, and a
server without a GPU were not measured; the search is a straight scan, so it grows in proportion to the library and would need an index beyond that size.

Memory: the model has 203 million parameters (about 0.8 GB of weights on disk, more in memory).

The timings of the evaluation run itself (`2026-10-22-semantic-search.json`, section `seconds`) were taken while a few short unit-test commands ran on the same machine; the table above is from
a separate, quiet run.

## Not measured

Other languages (the model card does not claim any), scene and attribute queries, a personal photograph library (people, indoor scenes, screenshots), a library with no match for the
query (the floor was chosen to reduce this, not tested on it), PostgreSQL ranking speed, and any machine other than this one.
