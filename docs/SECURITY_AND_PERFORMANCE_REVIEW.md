# Security and performance review (2026-09-19)

What was checked, what was found, what was changed, and what was deliberately
left. Repeat it before a release; the commands are at the end.

## Dependencies

`pip-audit` reported 14 advisories in installed packages.

| Package | Advisory | Affects this project? | Action |
|---|---|---|---|
| Django 6.0.7 | GeoDjango DoS on nested geometry | No (GeoDjango is not installed) | Updated to 6.0.8 |
| djangorestframework 3.17.1 | `request.data` bypasses `DATA_UPLOAD_MAX_MEMORY_SIZE` for JSON and form bodies | **Yes**: public endpoints (login, register, password reset) accept request bodies | Updated to 3.17.2 |
| djangorestframework 3.17.1 | `AdminRenderer` can disclose data on a 400 | No (`AdminRenderer` is not enabled) | Updated to 3.17.2 |
| sqlparse 0.5.5 (8 advisories) | DoS and escaping bugs when parsing attacker-supplied SQL | No (the app never parses user SQL) | Updated to 0.6.0 |
| pip 26.1.2 | Malicious package index only | Tooling only | Updated to 26.2 |

Only patch releases were taken; Django 6.1 and DRF 3.18 exist but need their own
compatibility pass. `requirements.txt` now carries the safe minimums. After the
update `pip-audit` reports no known vulnerabilities and the full test suite
passes.

## Static analysis (bandit)

17,815 lines of application code, no high-severity findings.

| Finding | Verdict |
|---|---|
| B105 "hardcoded password" (6) | False positives: an error message, documentation examples, and throttle rates such as `"60/hour"` |
| B110 `except: pass` in the batch enhancement task | **Fixed.** A failure was silently swallowed and the image stayed "processing". It is now logged and marked failed, and the batch continues |
| B110 in `operations/services.py` (2) | Left: best-effort cleanup inside health checks |
| B311 `random` in the training script | Left: shuffling training data, not security |
| B614 `torch.load(weights_only=False)` in the training script | Left, with a caveat: it only loads the developer's own checkpoint when resuming training. Do not point `--resume` at a file from an untrusted source |

## Performance

Query counts were measured on every main list endpoint with 2 rows and then 22.

| Endpoint | Result |
|---|---|
| `my-image-list` (the My Images screen) | **N+1: 2 extra queries per image** (47 queries for 22 images). Fixed by prefetching identity groups and edit versions; the count is now constant. A test asserts it stays constant |
| image, batch, enhancement result and job, comparison, share, download history and package lists | Constant already |

While fixing that, a latent bug surfaced: an image with two faces of the same
person listed that person's group twice, because the model's default ordering
leaked into `DISTINCT`. Each group is now listed once, on both the optimised and
the fallback path.

Identity-model verification hashed the whole 38 MB SFace file on every call, and
a face triggers several calls. Successful verification is now cached against the
file's path, size and modification time, so an unchanged file is hashed once and
a cached check costs about 0.4 ms instead of 180 ms. A modified file misses the
cache and is re-hashed; failures are never cached (tests cover both).

The test suite dropped from about 610 s to about 66 s for the everyday run by
using a low-iteration password hasher in tests only (see `config/test_runner.py`).

## Left as is, on purpose

- Django 6.1 and DRF 3.18: newer minor lines, deferred.
- Per-account login lockout (see the authentication review in
  `docs/features/01-authentication-and-authorization.md`).
- Tokens are signed with `SECRET_KEY` (HS256).
- Web-server access logs contain share-link tokens (see the share-link doc).

## How to repeat this

```powershell
python -m venv .runtime\audit-tools
.runtime\audit-tools\Scripts\pip install pip-audit bandit
$env:PIPAPI_PYTHON_LOCATION = (Resolve-Path venv\Scripts\python.exe)
.runtime\audit-tools\Scripts\pip-audit --path venv\Lib\site-packages
.runtime\audit-tools\Scripts\bandit -r accounts uploads enhancement validation quality_control duplicates identities reports downloads notifications operations activity privacy sharing config -x "*/tests.py,*/test_*.py,*/migrations"
python manage.py check --deploy
```
