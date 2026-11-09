# Optional cloud enhancement (Deep Image Auto Enhance)

Status: implemented on branch `feat/cloud-auto-enhance`; tested against a scripted stand-in (`enhancement/fake_deep_image.py`) **and, on 2026-11-09, by three real requests for one
photograph** (section 9: what they showed, and what is still not verified). Off by default.

With the switch on, a photograph the enhancement engine would have worked on is sent to Deep Image and one of its six `auto_enhance*` presets makes the result. Nothing else of
Deep Image is called (no background removal, no upscale-only, no other preset: `DEEP_IMAGE_PRESETS` is the whole list). The local engine stays in place as the fallback and does
everything the cloud engine does not do. With the switch off nothing changes: the code path, the stored record and the network are exactly what they were.

## 1. Off by default

| Setting | Default | Meaning |
|---|---|---|
| `CLOUD_ENHANCEMENT_ENABLED` | `false` | master switch |
| `DEEP_IMAGE_API_KEY` | empty | secret, environment/secret store only; never logged, stored, returned by an endpoint or committed |
| `DEEP_IMAGE_PRESET` | `auto_enhance_pro` | one of `auto_enhance_pro` (best, about 30 s a photograph by the provider's figures), `auto_enhance_qwen` (about 30 s), `auto_enhance_klein9b` (about 15 s), `auto_enhance_generative` (about 15 s), `auto_enhance_tone_correction`, `auto_enhance` (about 3 s) |
| `DEEP_IMAGE_BASE_URL` | `https://deep-image.ai/rest_api` | development only: a local test server. The production guard refuses any other value |
| `DEEP_IMAGE_REQUEST_TIMEOUT_SECONDS` / `DEEP_IMAGE_DEADLINE_SECONDS` | 30 / 150 | one HTTP request / the whole photograph (upload, waiting, download) |
| `DEEP_IMAGE_POLL_SECONDS` | 2 | pause between two status requests |
| `DEEP_IMAGE_UPLOAD_JPEG_QUALITY` | 95 | the JPEG the provider receives |
| `DEEP_IMAGE_MAX_RESULT_BYTES` / `DEEP_IMAGE_MAX_RESULT_PIXELS` | 64 MiB / 40 MP | a larger answer is refused (its size is read from the header before it is decoded) |
| `DEEP_IMAGE_DAILY_REQUEST_LIMIT` | 300 | requests sent per calendar day (0: no limit); a photograph over it is enhanced locally |
| `CLOUD_ENHANCEMENT_MIN_STRUCTURE_SSIM` | 0.5 | see section 4 |

Switching it on needs the key as well: with the key missing a photograph is enhanced locally and its result says the cloud engine is "switched on but not set up". The production
guard (`config/production_guard.py`) refuses to start a production deployment that switches it on without a real key or with another address. Restart the web process **and the worker**
after changing the `.env` (settings are read at start). Switching it off again is one line and a restart.

## 2. Which photographs go to the cloud

Decided in `enhancement/services.py` (`_enhance_photo`), before anything leaves the server. A photograph goes to Deep Image only when **all** hold; otherwise the local engine answers and
`applied_parameters["cloud_enhancement"]` records why (`used: false`, `reason`, `attempted`).

| Reason (`cloud_enhancement.reason`) | When | Shown to the person |
|---|---|---|
| `no_consent` | the owner's face-processing consent is off (the switch face restoration already needs) | nothing |
| `lighting_requested` | the request named a lighting method (Dark photo / Light photo): that is the local engine's method | nothing |
| `transparency` | a PNG with transparency (the provider's result carries none) | nothing |
| `not_configured` | switched on, no key | a sentence in `warnings` |
| `daily_limit` | today's limit was reached | a sentence |
| `identity_unverifiable` | the photograph has a face that could be judged and the identity model cannot verify it, so nothing is sent | a sentence |

Automatic enhancement after an upload and a person's own request both go through the same job, so both use the cloud engine while the switch is on. Every upload then spends a credit:
the daily limit is the backstop. The editor's sliders and the model comparison do not use it.

## 3. What is sent, and what is not

One request per photograph: the pixels the engine was given, re-encoded as a JPEG (`photo.jpg`: **no EXIF, location, colour profile or original file name**) and the preset's name.
Not sent: the user's name, email or id, any account data, the original file, face embeddings or identity data, other images. The key goes only to the provider's own address, in a
header; the finished image is fetched from the address the provider answers with, without the key, and only from an `https` address that is not a private one (a redirect on a request
that carries the key is refused).

What Deep Image says about its handling (its own pages, read 2026-11-09 by an assistant, not by a lawyer: check them yourself before relying on them): files processed through the API
are deleted within 40 days; its servers for customer files are on AWS in the EU/EEA; it does not sell images or use them to train models; "external AI model and inference-processing
providers" may be used for some operations, may process data outside the EEA (the UK and the US are named) and may retain material temporarily; **which presets use them is not stated**,
and no data-processing agreement is published. The people in a photograph are not asked: the provider's terms put the permission of the people shown on the person who uploads.

What the first real run added (section 9): the provider's reply for a finished job carries `nsfw_moderation` with `"model": "omni-moderation-latest"`, an OpenAI model, so the photograph is
also seen by OpenAI for moderation (at least for the `auto_enhance` preset); and the finished image is served from a public S3 address (`https://<bucket>.s3.eu-central-1.amazonaws.com/<date>/<uuid>.jpg`,
no signature): whoever has the address can fetch the enhanced photograph until the provider deletes it. The address is a random UUID and this code never logs or stores it.

**Consent.** The only switch consulted is the existing face-processing consent, and its text (`privacy.services.PURPOSE_COVERS`) does not say that a photograph may leave the server.
There is deliberately no new screen: the interface did not change. So the switch is an operator's decision for a deployment whose users have been told (the privacy notice must say it),
or for a demonstration with the operator's own photographs. It must not be on for strangers' photographs. The advisor's separate, versioned consent (`docs/enhancement/openai-advisor.md`)
is the pattern if this is ever offered to the public.

## 4. How the result is judged

The result is kept only if it is still the same picture. The checks are `enhancement/cloud.py` `judge`; the first that fails sends the photograph to the local engine.

| Check | Reason | Limit |
|---|---|---|
| width / height of the result against the upload | `aspect_changed` | 2 % |
| result narrower than half the upload | `result_too_small` | 0.5 |
| result is a flat fill (the upload was not) | `result_flat` | luminance std under 2 |
| structure similarity of the brightness-normalised luminance with the upload (`validator.structure_ssim`) | `structure_changed` | `CLOUD_ENHANCEMENT_MIN_STRUCTURE_SSIM` (0.5): a backstop against a re-composed image, **not calibrated on the provider's output** |
| a face of the upload is found again where it was (landmark geometry on the upload's own pixel grid) | `face_lost`, `face_geometry_changed` | `Limits.face_landmark_shift_max_upscale` (0.06 of the face width), the limit the local engine holds an enlargement to |
| every face is the same person (SFace guard) | `identity_changed`, `identity_unverifiable` | the guard limit (0.80), not the face-restoration one; a face that cannot be verified is **not accepted** |

Faces under 48 px (`too_small`) are not judged, as in the local engine; a result with only such faces says so (`identity_verification.state` is `not_verifiable`, never `verified`). The
face and identity limits are the local engine's own and are not loosened. A generative preset may change a face: where it does, the photograph keeps the local engine's result, which may
be the unchanged original.

## 5. What the API shows (no interface change)

The cloud result is stored in the shape of an adaptive result with one accepted stage, so every reader works as it did:

* `processing_summary`: `{"ai_applied": true, "classical_fallback": false, "stages": [{"stage": "cloud_enhance", "engine": "deep_image_<preset>", "kind": "ai", "fallback": false, "replaced": []}]}`;
  `applied_stages` has the same stage; `enhancement_outcome` is `enhanced`; `enhancement_summary` says "Enhanced: improved the photograph with Deep Image Auto Enhance Pro, a cloud AI model."
  and, when faces were checked, "The faces were checked against your original.". A frontend that does not know the names shows them as text.
* When the cloud engine was wanted and was not used, the result is the local engine's, its `processing_summary` names the local engines (or none), and `warnings` carries one sentence for the
  reasons above that need one. The summary never names the cloud engine for a photograph it did not make.
* `output_width` / `output_height` are the result's: the provider's size rule makes a photograph 1000-4096 px wide 4096 px wide and a smaller one four times wider, so the output is
  usually much larger than the upload (the API already says to read these fields). The file is re-encoded in the result's output format (the provider's JPEG is decoded and encoded again).
* `applied_parameters["cloud_enhancement"]`: `attempted`, `used`, `provider`, `preset`, and for a used result the provider's job id, seconds, byte counts and the readings of every check.
* One log line per photograph (`cloud_enhancement {...}`): the decision, the timings and the readings, never pixels, a key, an address or user data.

## 6. Failures

Every failure ends with a usable result: the local engine's. The upload is never repeated (every attempt is paid for); a failed status request is tried again up to 3 times, within the deadline.
A cancelled job stops waiting at once and is not turned into a local result.

| Reason | Cause |
|---|---|
| `plan_required` | HTTP 400 with "paid account" in its message: seen when a free account asked for `auto_enhance_pro` |
| `unauthorized`, `no_credits`, `rate_limited`, `provider_error`, `rejected`, `redirected` | the provider answered with HTTP 401/403, 402, 429, 5xx, another 4xx, a redirect. The provider's own words for a refusal go to the log (short, with the key taken out) so the first misconfiguration can be read |
| `timeout`, `connection_error`, `deadline`, `job_failed`, `bad_reply`, `bad_result`, `result_too_large` | no answer in time, no connection, the job did not complete, an answer that is not what the documentation shows, an image that cannot be read or is too large |
| `unexpected_error` | a bug in this path; logged with its traceback, and the local engine takes over |
| the checks of section 4 | the result was delivered and refused |

A job status that is neither known to be in progress nor known to be a failure is **not** a verdict: the client keeps waiting (and names the status once in the log) until the deadline. An
earlier version gave up on `predicting`, a status the real service reports while a job runs, and so abandoned a job that was paid for.

When the result's own address cannot be reached (a reset or a timeout) and is a bucket host, the same object is asked for through the regional endpoint
(`https://s3.<region>.amazonaws.com/<bucket>/<key>`). On the owner's network the bucket host was reset in the TLS handshake, for Python and curl alike, while the regional endpoint answered.

A Celery task that is retried after a database failure runs the whole job again, so the photograph is sent again; the daily limit bounds that.

## 7. Cost

Credits per call are not published for the presets; the provider's terms say operations that use external models "may require more than one credit". Its pages (read 2026-11-09)
say "from $0.04 per image", 10 free test credits per new account (another page says 5), purchases non-refundable, and results of an account that never bought anything for personal,
non-commercial use. Count the credits on the provider's dashboard after the first run: this code does not read them.

## 8. Tests

`enhancement/test_cloud_enhancement.py` (88 tests): the real client against `fake_deep_image.py` (the multipart upload, the key and where it does not go, redirects, every refusal, timeouts, a
closed port, the key never in a log even when the provider repeats it), the checks one by one, the stored record read by `describe_stages` / `processing_summary` / the API,
the job code (claim, publish, API fields, comparison endpoint), the switch off (the network is blocked while it runs), consent, lighting, transparency, the daily limit, cancellation,
the production guard. A first mutation run (17 of 69 mutations, stopped to change the status handling) found two guards no test needed; both are now tested. The full run is repeated on the final code (see the pull request).

## 9. What the first real run showed, and what is still not verified

One photograph (a 1920 x 1080 webcam capture, its standardized copy: the engine's own input), three requests, 2026-11-09, a free account's key.

| Seen | |
|---|---|
| A file upload (`multipart/form-data`, field `image`, field `parameters` = `{"preset": ...}`) is accepted | for `auto_enhance_pro` it reached the plan check; for `auto_enhance` it was processed |
| `auto_enhance_pro` with a free account | HTTP 400, `{"message": "This Auto Enhance preset is available only for paid accounts.", "code": "BAD_REQUEST"}` (reason `plan_required`) |
| Job statuses | `received`/`predicting` (not in the documentation's samples) then `complete`; the reply holds `status`, `job`, `queue`, `result_url`, `nsfw_moderation` |
| Time | about 10 s for `auto_enhance` |
| The result | a 4096 x 2304 JPEG (900 KB) for the 1920 x 1080 upload; sharper (Laplacian variance +47 %), more saturated (+29 %), smoother skin; the live engine's result of the same photograph was nearly identical to the original |
| **A watermark** | the free account's result has the text "watermark" laid across the whole picture, the face included. The engine's checks do not see it (a light overlay): a free key must not be used for real results |
| The engine's checks on it | accepted: structure 0.962, landmark shift 0.010 (limit 0.06), identity 0.915 (limit 0.80) |
| The result's host | reset on this network (see section 6); the regional endpoint worked and the fixed client fetched the same 899,985 bytes |
| Credits | not read; how many the three requests used is not known (check the provider's dashboard) |

| Still not verified | Why it matters |
|---|---|
| That a paid account removes the watermark and unlocks `auto_enhance_pro`, `auto_enhance_qwen`, `auto_enhance_klein9b` | the presets the owner wants; the quality of `pro` has not been seen |
| Anything on a second photograph | one photograph says little about how often the face checks refuse a generative result |
| The HTTP status for a bad key or for "no credits" (a bad key was never sent) | the 401/402/403 mapping is a guess |
| Credits per preset, rate limits, maximum input size, how long a result address stays valid | not stated by the provider |
| That the structure limit (0.5) and the other limits are right for the provider's output | they are backstops, not calibrated on more than one result |
