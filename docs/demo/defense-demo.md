# Defence demo script (about 8 minutes)

Everything runs on this laptop's CPU. Nothing is uploaded anywhere.

## Before the session
1. Start the stack (once): Redis, worker, Beat, web server.
   `redis-server --port 6379 --save "" --daemonize yes`, then in `.runtime/dev/env.sh` terms:
   `. .runtime/dev/env.sh` and run `celery -A config worker -l info --pool=solo`,
   `celery -A config beat -l info`, `python manage.py runserver 127.0.0.1:8001`.
2. Make a login: `python manage.py createsuperuser` (or the `demo` user in the dev database).
3. Build the gallery for your photo: `python scripts/build_demo.py --image your_photo.jpg --out demo_page`
   and open `demo_page/index.html`.

## The story (what to say and show)
1. **Gallery, section 1:** "A good photo is left alone: the output is pixel-identical." (Honest design:
   no beautifying, only measured faults are corrected.)
2. **Gallery, section 2:** the dark copy (`scripts/seed_demo.py --dark` makes the same kind of copy for the live demo: **x0.40** of the light, brightness about 28 for a normal portrait; the quality check lifts such a photograph instead of refusing it, and refuses only a practically black one). "By default automatic mode does not brighten it, because
   pixels cannot tell a dark photo from a deliberately dark one. When the user chooses a level it lifts;
   with `balanced_v1` the Zero-DCE model lifts it on the CPU (27 to 56)."
3. **Gallery, section 3:** the pre-filter crops. "A lift amplifies noise, so a gentle filter runs first,
   only for a requested lift of a dark photo."
4. **Gallery, section 4:** blur, heavy JPEG, overexposure. "The pipeline does not invent detail that is
   not there; it reports it as beyond the lightweight pipeline."
5. **Swagger (http://127.0.0.1:8001/api/docs/), live:** log in, upload a photo, enhance it (watch the
   worker log), open the editor controls and a preview, save, download. Show that a second preview request
   with the same revision and different settings is refused (409).
6. **The models:** `GET /api/system/health/` as an administrator: every optional model is
   `installed: true`, `checksum_verified: true`, `status: disabled`, reason "disabled by the lightweight_v1
   rendering policy". Also `python scripts/model_manifest.py verify`.
7. **Switching a policy live (one command):**
   `python scripts/compare_policies.py --image your_photo.jpg --darken 0.28 --out compare.jpg`
   shows the default and the learned policy side by side.
8. **The advisor:** `GET /api/enhancements/advisor/configuration/` says it is off until a key and a model
   are configured, consent is separate, only a small metadata-free preview would ever be sent.

## Questions to expect, and the honest answers
* *Why are the models off?* Automatic mode must not guess intent; generative face restoration cannot be shown to
  keep the person; two checkpoints are non-commercial; no GPU path is validated. They are installed and can be enabled.
* *Is it as good as a deep model on dark photos?* No. On the benchmark, Zero-DCE (CPU) beats the default by a
  clear margin on exposure and colour (`docs/evaluation/2026-10-15-lowlight-benchmark.md`), and neither reaches the
  clean photo's brightness because of the safety guards.
* *Is the OpenAI advisor validated?* Tested with a mocked provider and the real SDK against a local stand-in;
  not yet against the real service (needs a key, a model name and a cost cap).
* *Is the backend complete for the frontend?* Every API path the frontend calls exists with matching fields;
  the rest is frontend integration (`docs/alignment/frontend-alignment-2026-10-15.md`).
