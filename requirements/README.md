# Requirements

Every pip requirements file of the project is in this folder. Install what you need, nothing more.

| File | What it is | Install |
|---|---|---|
| [`base.txt`](base.txt) | The runtime dependencies: what the application imports (Django, DRF, Celery, OpenCV, Pillow, storage, ...). CI audits this file with `pip-audit`. | `pip install -r requirements/base.txt` |
| [`test.txt`](test.txt) | Test-only dependencies (`moto`, so the S3 storage tests need no network and no AWS account). | `pip install -r requirements/base.txt -r requirements/test.txt` |
| [`gfpgan.txt`](gfpgan.txt), [`gfpgan-constraints.txt`](gfpgan-constraints.txt) | An isolated worker environment that adds PyTorch and the GFPGAN stack on top of `base.txt`. Read the header of the file first: it has an OpenCV step. | `.runtime/venvs/gfpgan/bin/pip install -r requirements/gfpgan.txt` |
| [`semantic.txt`](semantic.txt) | Optional libraries for search by what is in a photograph (`SEMANTIC_SEARCH_ENABLED`). Not imported while the switch is off. | `pip install -r requirements/semantic.txt` |
| [`loadtest.txt`](loadtest.txt) | Locust, in its own environment (`loadtest/locustfile.py`). | `.runtime/venvs/loadtest/bin/pip install -r requirements/loadtest.txt` |
| [`model-export.txt`](model-export.txt) | Conversion-only dependencies (PyTorch to ONNX). Inference does not need them. | `pip install -r requirements/model-export.txt` |
| [`training.txt`](training.txt) | Model training (a CUDA-enabled PyTorch in Colab is preferable). | `pip install -r requirements/training.txt` |

The file `requirements.txt` in the project root is a single line, `-r requirements/base.txt`. It exists so that `pip install -r requirements.txt` (hosting
panels, runbooks, habits) keeps working; it holds nothing of its own.

Inside this folder `-r` and `-c` lines are relative to the file that contains them (`gfpgan.txt` says `-r base.txt`). Why a version is pinned or floored is
written next to it in the file. Older reports under `docs/` may name these files by their former paths (`requirements-gfpgan.txt` is now `requirements/gfpgan.txt`).
