# Frontend API guide

For the developer building the FaceCloud frontend. This is the practical
"how do I use the API" companion to two things that stay authoritative:

- **The live schema** at `/api/docs/` (Swagger), `/api/redoc/` and
  `/api/schema/` (raw OpenAPI). Every endpoint, field and enum is there and is
  checked in CI, so if this guide and the schema ever disagree, the schema wins.
- [`API_ERRORS.md`](API_ERRORS.md) for every error shape and status code.

This guide covers what the schema cannot say well: the order to call things in,
what to poll, and which rules the UI must respect.

## 1. Basics

**Base URL:** the backend origin, e.g. `http://127.0.0.1:8000`. All paths below
start with `/api/`.

**Format:** send and receive JSON (`Content-Type: application/json`). File
uploads use `multipart/form-data`.

**Authentication:** send `Authorization: Bearer <access token>` on every request
except the public endpoints (registration, email verification, login, token
refresh, password reset, public share links).

**CORS:** open only while the backend runs with `DJANGO_DEBUG=True`. In
production the backend allows only the origins in `CORS_ALLOWED_ORIGINS`, so
your frontend's origin must be listed there.
Browser scripts can read the `Content-Disposition` (download filename) and
`Retry-After` (429 wait time) response headers; the backend exposes both.

**Dates** are ISO 8601 strings in UTC.

**Lists:** some list endpoints are paginated (`{count, next, previous, results}`,
e.g. `GET /api/images/my/`, default 24 per page, `?page=` and `?page_size=` up
to 100) and some return a plain array. Check the endpoint in Swagger, and write
one helper that accepts both shapes.

**Ownership:** you only ever see your own data. Someone else's record returns
**404, not 403**, so do not build UI that expects a 403 for "not yours".

## 2. Sign up, log in, stay logged in

Registration is a two-step flow. No account exists until the link in the
verification email is opened.

**Length limits on the public forms.** A login identifier (`identifier`, `username` or `email`) and any email on a public form (sign-up, password reset) may be at most **254** characters, a password at most **1024**, a reset or invitation token at most 256. A longer value is a `400` with the field named and a plain sentence (for example `{"identifier": ["This is too long: use at most 254 characters."]}`), never a 500. Use `maxlength` on those inputs.

```text
POST /api/auth/register/            {full_name, email, password, password_confirm?, username?}
   -> 202 {registration_id, ...}    (an email with a link is sent)
        link: {FRONTEND_URL}/verify-email?registration_id=<uuid>&token=<token>
POST /api/auth/verify-email/        {registration_id, token}      (both come from the link)
   -> 201                           (account now exists; the link works once)
POST /api/auth/verify-email/resend/ {registration_id}     (new link, the old one stops working; has a cooldown)
GET  /api/auth/registrations/{registration_id}/status/    (masked status, for a "check your email" screen)
```

**Registering from an organization invitation (one step).** The invitation link is `{FRONTEND_URL}/invitations/{token}`. If the person has no account, let them register on
that page and add `invitation_token` (the token from the link) to the same `POST /api/auth/register/` body:

```text
POST /api/auth/register/   {full_name, email, password, password_confirm?, username?, invitation_token}
   -> 201 {access, refresh, user, role, dashboard, organization_roles}     (the same shape as login)
```

The invitation was sent to that mailbox, so the address counts as verified: the account is created verified, the invitation is accepted and the person is signed in, all in one
transaction (a moderator invitation lands in the moderator portal: `dashboard: "moderator"`). `email` must be the invited address (case does not matter); use the
invitation preview (section 7) to show it read-only. A token that is unknown, used, cancelled, expired, for a suspended organization, **or for another email address** answers the same
`404 {"detail": "This invitation is invalid, used or expired."}` as the preview and creates nothing: show that sentence only. Otherwise the usual rules hold: password rules
(400), the address already has an account (400 on `email`: ask them to sign in, then accept the invitation), the registration rate limit (429), and `403` `plan_limit_reached` when the
organization has no seat left (nothing is created and the invitation stays usable). Without `invitation_token` nothing changes (202 and the verification email).

**Login**

```text
POST /api/auth/login/  {identifier, password}     (identifier = username or email)
   -> 200 {access, refresh, role, dashboard, user, organization_roles}   (organization_roles: section 7b)
```

- **Too many failed attempts.** Sign-in is limited by *failed* attempts only, so people sharing one address (a classroom) do not
  lock each other out and a correct password is never refused for that reason. Two things can answer `429`: the account's own
  backoff after 5 wrong passwords in a row (`"Too many failed sign-in attempts. Try again in N seconds."`, whatever the address),
  and the failed-attempt limit per address and account name (`"Request was throttled. Expected available in N seconds."`). Both carry a
  `Retry-After` header with the seconds: show "try again in N seconds" (or a countdown) instead of a generic error. Token refresh is
  limited per refresh token, so a flood of refreshes from one person does not affect anyone else on the address. The values and the
  demo-day settings are in `docs/deployment/rate-limits.md`.
- `role` is `user`, `moderator` or `admin`. `dashboard` is **always the same
  value** (`"user"`, `"moderator"`, `"admin"`), meant for choosing which
  dashboard to open after login. A person who is an *active moderator of an
  active, non-personal organization* gets `moderator` even though their stored
  account role is `user` (`user.role` is the stored value; organization owners
  stay `user`). This chooses the portal only: what each endpoint allows is
  unchanged. **A moderator works for an organization**: an account whose stored
  role is `moderator` but that has no active moderator or owner membership in an
  active organization is refused at sign-in (and at token refresh) with
  `403 {"detail": "Your moderator account is not assigned to an organization yet.", "code": "no_organization"}`;
  show that sentence and ask them to contact their administrator (see section 7b).
- Every user object (`user` here, `GET/PATCH /api/user/profile/`, the admin user
  list) carries `full_name` (the username when no name is set). `PATCH
  /api/user/profile/ {full_name}` changes it.
- Wrong credentials, an unverified account and a suspended account all return
  **400** with the same kind of message (not 401), so accounts cannot be
  enumerated. Show the message as is.
- After repeated wrong passwords the account is refused with **429** and a
  `Retry-After` header. Show the message and offer "Forgot password".

**Forgot password**

```text
POST /api/auth/password-reset/          {email}                           -> 200 (always the same message)
POST /api/auth/password-reset/confirm/  {uidb64, token, new_password}     -> 200, or 400 for an invalid/expired link or a weak password
```

- The email contains `{FRONTEND_URL}/reset-password/{uidb64}/{token}`. The frontend needs a page at that route that reads the two
  values and posts them with the new password. The link works once; a successful reset signs out every session.
- The first call never reveals whether the address has an account.

(Two-factor authentication was removed on 2026-10-16: login is always one step and there are no `/api/auth/2fa/*` endpoints.)

**Tokens**

- The access token lasts **15 minutes**. The refresh token lasts 7 days and
  **rotates**: each `POST /api/auth/refresh/ {refresh}` returns a new refresh
  token that replaces the old one. Always store the newest.
- On a 401 with `code: "token_not_valid"`, call refresh once and retry the
  original request. If refresh fails, send the user to login.
- A 401 saying "This token has been revoked" means the password, email, role or
  suspension changed, or the user logged out everywhere. Refreshing will not
  help; go to login.
- `POST /api/user/logout/ {refresh}` logs out this device.
  `POST /api/user/logout-all/` revokes every session.

**Forgot password**

```text
POST /api/auth/password-reset/          {email}
   -> 200 always the same message ("If an eligible account exists...")
```

The email contains a link to **your frontend**:
`{FRONTEND_URL}/reset-password/{uid}/{token}`. Build a page at that route that
reads `uid` and `token` from the URL, asks for a new password, then:

```text
POST /api/auth/password-reset/confirm/  {uidb64: uid, token, new_password}
   -> 200 "Password reset successfully. Please log in."
   -> 400 {"detail": ["Invalid or expired reset link."]}   or password-rule errors
```

Every session is revoked after a reset, so send the user to login. Because the
first endpoint always answers the same way, do not tell the user whether the
email exists; say "If an account exists, we sent an email."

**Profile and account:** `GET/PUT/PATCH /api/user/profile/`,
`POST /api/user/change-password/`, `DELETE /api/user/delete-account/`,
`GET /api/user/dashboard/`, `GET /api/user/activity/` (the activity timeline).

**Changing the password** (`POST /api/user/change-password/ {current_password, new_password}`): a wrong `current_password` is a `400` on that field; a
`new_password` identical to the current one is a `400` `{"new_password": ["Choose a password you have not just used."]}` (show it under the new-password
field). A refused request changes nothing and signs nobody out. Only a change that goes through ends every session, on every device, including the one
that made it: send the person to the sign-in page.

## 3. Upload and processing

```text
POST /api/images/upload/single/    multipart, field: file
POST /api/images/upload/multiple/  multipart, field: files (repeat the field)
POST /api/images/upload/zip/       multipart, field: file
   -> 201 a batch: {id, status, total_files, accepted_files, rejected_files,
                    processing_status, processing_mode, auto_enhance, images: [...], ...}
```

**Automatic enhancement (new).** A photograph that passes the checks is now **enhanced without anyone asking**: `accepted` at once, `rejected` never, and
`needs_review` at once in a personal workspace or after a moderator approves it in an organization. Every upload request takes an optional form field
**`auto_enhance`** (`true` or `false`; anything else is a 400 `{"auto_enhance": ...}`); absent means the account setting, which is on unless the person turned it off
(section 8: `GET/PATCH /api/notifications/preferences/` carries `auto_enhance`). The answer is stored on the batch and returned as `auto_enhance`. Offer it as a checkbox
on the upload page ("Enhance my photos automatically"), pre-set from the preference. It adds nothing to what a person can still do by hand: `POST /api/enhancements/...`
works as before, and a photograph that already has a result is never enhanced a second time. Every photograph in the answer carries `enhancement` (below), so **in
`inline` mode the finished results are already in the upload answer**; with a worker they are `queued` and move as you poll.

**`processing_mode`** (new, upload answers only): `"background"` when a Celery worker does the work, `"inline"` when the background jobs run inside this very request
(`CELERY_TASK_ALWAYS_EAGER` is on, normally a development server without a worker). In `inline` mode the answer already holds the finished batch and **there is no
progress to show**: do not draw a progress bar or poll, show the result, and (in development) a one-line hint "background jobs run inside the request: start the
worker to see progress". `GET /api/system/health/` tells an administrator the same (section 10).

**Submitting to an organization.** All three upload requests take an optional `organization_id` (a form field next to the
file). Without it the upload stays in the person's own workspace, exactly as before. With the id of an organization the
person is an active member of (see `GET /api/organizations/`: the ones with `is_personal: false`), the upload is filed
under it, and **that organization's moderators can then see and review it**. Anyone who is not an active member gets a
400 `{"organization_id": ["You are not an active member of that organization."]}` and nothing is stored; a value that is not
a number is a 400 too. The upload page should offer "Submit to: My workspace / <team name>" only to people who belong
to a team, and say that the team's moderators will see the photos. The owner always still sees their own uploads.

Every upload creates a **batch** (a single image is a batch of one). Files that
fail validation are rejected individually; check `rejected_files` and each
image's status rather than assuming all succeeded. Accepted formats are JPG/JPEG,
PNG, WebP, BMP, TIFF and static GIF (an animated GIF is rejected). The file's
extension must match its real content, and the size and pixel limits in
`.env.example` apply.

**Following progress.** Processing runs in the background. Poll:

```text
GET /api/images/batches/{batch_id}/progress/
   -> {batch_id, status, stage, progress_percentage, counts,
       estimated_completion_at, cancellation_requested, retry_count,
       auto_enhance, enhancement_finished,
       images: [{image_id, filename, stage, status, display_status,
                 progress_percentage, failure_stage, failure_reason,
                 enhancement: {status, outcome, summary, waiting_for, skipped_reason}, ...}]}
```

- **Enhancement is a stage of the batch.** Each image (in the progress and in every batch response) has `enhancement`:
  - `status`: `queued`, `processing`, `completed`, `skipped`, `failed` or `not_applicable` (the upload turned it off, or the photograph was rejected by the checks).
  - `waiting_for` (only while `queued` and not yet started): `quality_verdict` (its checks have not decided), `moderator_approval` (an organization's moderator has to
    approve it first) or `capacity` (the person already has the most enhancements running; it starts as they finish). Draw those as "waiting", never as done.
  - `outcome` and `summary` (once it ended): `enhanced`, `partially_enhanced` or `original_preserved` (a clean photograph: nothing safe would help, so the
    original is kept; **this is a success**, say "no change needed", not "failed"), or `failed`; `summary` is one plain sentence safe to show.
  - `skipped_reason` (when `skipped`): `capacity` (no free slot for too long), `cancelled`, `already_requested`, `not_possible` or `error`. A skipped photograph is
    never failed and its upload is untouched.
  The batch is **not `completed` before its enhancements finished or were skipped** (`enhancement_finished` says so); a photograph that waits for a moderator does not
  hold the batch back, because that can take days. A failed enhancement is that photograph's `failure_stage: "enhancement"` (status `failed`), the original stays,
  and `POST /api/images/batches/{id}/retry/` runs it again. The person gets **one** notification per batch, `batch_enhanced` ("3 photos enhanced"), none per photograph.

- **Person folders come after the quality verdict.** A photograph is filed into a person folder only once its quality check has decided: an `accepted` photo
  at once, a `rejected` one never, and one that `needs_review` only after a moderator approves it (at once when it is in a personal workspace, where no
  moderator can decide). So a photograph that is going to be rejected never appears in a folder, not even for a moment. Each image in the progress carries
  `identity_grouping: {status, waiting_for}`: `status` is `queued`, `processing`, `completed`, `needs_review`, `failed`, `cancelled`, `skipped` (rejected: it is
  never grouped) or `not_applicable` (grouping is off for this photo); a photo that is still waiting is `queued` and `waiting_for` says what for,
  `quality_verdict` or `moderator_approval` (otherwise it is empty). Draw the grouping step as "waiting" for those, never as done. The batch also has
  `identity_grouping_finished` (every photo that will be grouped has been) and `suggested_merge_count` (see section 5; `0` until the batch is fully grouped).
- Poll every 2 to 3 seconds while the batch is active and stop when
  `status` is terminal: `completed`, `partial`, `failed` or `cancelled`.
- `stage` moves through: `uploaded, queued, preprocessing, face_validation,
  quality_analysis, identity_grouping, enhancement, finalizing, completed`.
- A batch can be `partial`: some images succeeded and some did not. Show both.
- `display_status` is the photo's final verdict word, identical to the
  `display_status` of `GET /api/images/my/` (`rejected`, `needs_review`,
  `accepted`, `completed`, `ready`, `quality_processing`, ...). It is also on every
  photo in the batch responses (`POST /api/images/upload/*`,
  `GET /api/images/batches/` and `/{id}/`), so no second request is needed.
- `POST /api/images/batches/{id}/cancel/` stops a batch that is still working (on a batch that
  already finished it changes nothing and returns its real state);
  `POST /api/images/batches/{id}/retry/` requeues its failed images (there is a
  retry cap, and a 400 explains when it is reached or nothing failed).
- The notifications endpoints (section 8) can replace polling for "it's done"
  toasts, but progress bars still need the progress endpoint.

**Listing.** `GET /api/images/my/` (paginated) is the gallery view; both it and
`GET /api/images/` accept `?batch=<id>` to show one upload's photos (another
user's id gives an empty list, a non-number a 400),
`GET /api/images/batches/` lists batches, `GET /api/images/{id}/` and
`GET /api/images/{id}/download/` fetch one image. The single-image response is
the **same shape** as a row in `my/` — including `display_status`, the quality
decision and its reasons — so one screen's type works for both. Downloads are protected
endpoints, so fetch them with the Authorization header (not a bare `<img src>`;
use fetch with a blob, or a short-lived approach agreed with the backend).

**Deleting photos.** `DELETE /api/images/{id}/` (204) deletes one; `POST /api/images/bulk-delete/ {image_ids: [..]}`
(1 to 200 ids) deletes several and answers `{deleted_ids, not_found_ids, folders_updated, packages_expired, exports_expired}`:
ids that are not yours or do not exist are listed in `not_found_ids` and nothing is done for them. A delete also
removes the photo's files, thumbnails, enhanced copies and the share links to those copies; it **recounts every
person folder the photo was in** (`member_count`, and the folder no longer matches faces against it); and it
**expires the ready ZIP of the same upload and the ready folder exports of those folders**, because they hold a copy
(their secure links then answer 410; the owner can build new ones). A folder left empty stays until its owner
deletes it, and its `cover_images` is empty. Deleting an upload batch (`DELETE /api/images/batches/{id}/`) does the same.

**Favourites (new).** `PUT /api/images/{id}/favorite/` marks one of the person's own photographs (answer `{id, is_favorite: true, favorited_at}`), `DELETE` takes the mark off
(`is_favorite: false`); both are idempotent and a photograph that is not theirs, does not exist or was refused at upload is a `404`. `POST /api/images/favorites/
{image_ids: [..up to 200], favorite: true|false}` does many and answers `{favorite, updated_ids, not_found_ids}` (nothing is done for the ids in `not_found_ids`).
`GET /api/images/my/?favorite=true` lists only the favourites (`false` the others; it combines with every other filter), and every photograph row and batch image carries `is_favorite`.
Marking a favourite never changes the photograph's other timestamps.

**Thumbnails.** Never draw a gallery from `download_url`: originals can be
hundreds of megabytes. Each image in the list carries `thumbnail_url`, which
serves a small JPEG (`GET /api/images/{id}/thumbnail/`, optional `?size=256`
default or `?size=1024`; any other size is a 400). The 256 size is normally
already made in the background right after upload; if not, the first request
generates it (about 0.1 to 0.8 seconds for a large photo) and every later one
is served from the cache. The 1024 size is made the first time it is asked for.
It is a normal protected endpoint: fetch it with the Authorization header and
use a blob URL, exactly like a download. Use 256 for tiles and 1024
for a lightbox, then `download_url` only for the real file. The response carries
`Cache-Control: private, max-age=3600`. A file refused at upload (wrong type,
corrupt, unreadable) has no thumbnail (`thumbnail_url` is `null`); a photograph
that was stored but rejected by the quality or face checks does have one, so a
rejected photo can still be shown in the list with its reason.

**Quality, faces, duplicates.** Each image has read endpoints:
`/api/quality/images/{image_id}/`, `/api/validation/images/{image_id}/`,
`/api/duplicates/images/{image_id}/`. A decision is `accepted`, `rejected` or
`needs_review`, with human-readable reasons to show the user.

**"In review" only where someone can review (changed).** `needs_review` means a person has to look at the photograph. In an **organization** that is a moderator, and nothing changes
there. In the person's **own workspace** nobody can ever decide it, so it is no longer shown as a state: such a photograph is **`accepted`, with tips**. Concretely, for a photograph in a
personal workspace whose checks doubted it:

- `display_status` (everywhere: `GET /api/images/my/`, batch responses, progress) is `accepted`, the batch progress `counts` count it as `accepted`, the batch is `completed` (not `partial`
  because of it), `GET /api/images/my/?status=needs_review` does not list it and `?status=accepted` does, the dashboard counts it as accepted, the `accepted_results` ZIP includes it, the
  notification is "Photo accepted" with a tip, and no moderator review is opened for it.
- The quality decision (`GET /api/quality/images/{id}/`, and `quality_decision` on every photograph row) keeps **`decision` exactly as the checks found it** (it is the evidence) and gains
  **`effective_decision`** (what it means for the person: `accepted`), **`review_required`** (true only for a `needs_review` in an organization) and **`tips`** (plain sentences such as
  "This photo is a little dark; more light gives better results."). Show `tips`, never the word "review", when `review_required` is false.
- A **rejected** photograph is never softened (very dark, blown out, corrupt). The same photograph filed under an organization still shows `needs_review` and `review_required: true`.

## 4. Enhancement

```text
POST /api/enhancements/images/{image_id}/   {level, style}     -> 202
POST /api/enhancements/batches/{batch_id}/  {level, style}     -> 201
```

- `level`: `auto` (default), `low`, `medium`, `high`.
- `style`: `natural` (default), `dynamic`, `ai_max`.
- **What `style` and `level` do in the adaptive engine** (the default policy; `applied_parameters.adaptive.profile` echoes what was used). They change *when* an operation is
  tried and how strongly; the safety checks are the same for every choice, so a stronger choice can still end in "nothing needed fixing" or "kept the original":
  - `natural` (and what an automatic enhancement uses): defect-driven; a black-and-white photograph **stays** black and white.
  - `dynamic`: a stronger polish, also for a deliberately dark photograph, and **a black-and-white photograph is colourized** (`operations_applied` contains `colourization`).
  - `ai_max`: all of `dynamic`, a stronger polish, a dark scene with a visible subject may be lifted, light noise is treated, and a photograph under 1280 px is enlarged.
  - `level`: `low` = gentler (blends 0.65 and 0.4, polish x0.6); `auto`/`medium` = the style as it is; `high` = polish x1.4, a dark scene may be lifted, light noise is treated,
    a photograph under 960 px is enlarged.
  - **`detail_restoration`** (in `adaptive.operations_applied`, summary "restored fine detail and reduced compression blocking"): a soft or heavily compressed photograph (a WhatsApp copy)
    comes out clearer at the **same size**. It is tried for every style when the photograph shows heavy compression or softness (not for a sharp face in front of a blurred
    background), and AI Enhance / Strong prefer the slowest, best-looking model for it. A photograph over 4 megapixels is left alone. Say "clearer", not "enlarged".
  Suggested wording for the style picker: "Natural: keeps the photo as it is. Dynamic: more punch, and colours a black-and-white photo. AI Enhance: strongest; also brightens dark
  scenes and enlarges small photos." A colourized photograph is a guess at the original colours (the model predicts them; they are damped to look natural): say "colourized",
  never "restored". The upload and the original download are never changed.
- **Automatic results** (section 3) are ordinary results: `GET /api/enhancements/results/{id}/` and the lists, with a new field `trigger`: `auto` (started after the upload
  passed the checks) or `manual` (the person asked). An automatic one runs level `auto`, style `natural`. Its `applied_parameters.render_policy.name` is the policy it
  used (the adaptive engine by default), which can differ from a manual request's. Automatic ones count towards the same limit of enhancements in progress, so
  `POST /api/enhancements/images/{id}/` can answer 429 while a large upload is still being enhanced: say "wait for the running enhancements" and try again.
- `applied_parameters.no_correction_needed` is `true` when the photo had nothing to
  correct and was left as it is; say that rather than showing an identical
  before/after as an enhancement.
- **`ai_max` and identity.** `natural` and `dynamic` restore faces only when the
  result can be checked against the original. `ai_max` skips that requirement.
  Say so where the user picks it, and on every result read these three fields:
  - `identity_status`: `verified` (faces were compared with the original and
    matched), `inconclusive`, `unavailable` (no check ran) or `failed`. Only
    `verified` means a check passed; never present the others as "verified".
  - `identity_check_bypassed`: `true` when faces were generatively restored
    without a verified check. Show a visible notice on that result.
  - `identity_notice`: the sentence to show; empty when there is nothing to say.
  The same three fields are on every entry of a model comparison's `outputs`
  (`GET /api/enhancements/comparisons/{id}/`) and at the end of its CSV report,
  with the same meaning, so a profile reads there as it would on the result it
  produces. They are null / false / empty for the untouched `original` entry.
- The job is asynchronous. Poll `GET /api/enhancements/results/{id}/` until
  `status` is `completed` (or `failed`, with `failure_reason`). Other statuses:
  `pending, queued, processing, partial, cancelled`.
- A result carries `download_url`, `original_download_url`,
  `standardized_download_url` and an `editor_url`. `applied_level` may differ
  from the requested level (the backend adapts to the image), so show what was
  actually applied.
- **What was done (restoration).** A blurred face or a low-resolution photograph goes through the restoration chain when the server can
  run it (deblur, face restoration, upscale to HD; `docs/deployment/restoration-chain.md`). Every result carries three fields that
  say so, so a barely changed photograph is never presented as restored:
  - `applied_stages`: what changed the pixels, in order: `[{"stage": "deblur", "engine": "nafnet_gopro", "variant": "blend 1.0"},
    {"stage": "upscale", "engine": "realesrgan", "variant": "x2", "scale": 2, "input_size": [240, 320], "output_size": [480, 640]}]`.
    Stages: `low_light`, `denoise`, `deblur`, `face_restore`, `upscale`, and `polish` (a gentle contrast and sharpness polish of a
    photograph with nothing to fix, only when the operator turned `ADAPTIVE_POLISH_ENABLED` on: `{"stage": "polish", "engine":
    "opencv_polish", "variant": "blend 0.65"}`, summary "Polished: balanced the contrast and sharpened lightly.", outcome
    `enhanced`; give it a label such as "Polish"). `[]` when nothing changed.
  - `skipped_stages`: restoration the photograph called for that did not run (and, with polish on, `{"stage": "polish", "reason":
    "Already balanced and sharp: nothing to improve."}` when a polish was tried and the photograph was left as it was; that is not a
    failure and never feeds `hd_unavailable_reason`): `[{"stage": "face_restore", "reason": "codeformer: switched
    off (CODEFORMER_ENABLED=False), ..."}]`.
  - `hd_unavailable_reason`: one sentence when restoration was needed and not done (`"Restoration was needed but not available on this
    server: deblur (nafnet_gopro: checkpoint not found at ...)"`), else `null`. Show it where you would show "HD", for example "HD
    restoration is not available on this server", and do not claim the photograph was improved when `applied_stages` is empty.
  Because the output can be larger than the upload, read `output_width` / `output_height` (and `applied_stages[].output_size`) rather
  than assuming the original size.
- **Blur and low resolution do not reject a photograph that has a face.** The quality decision (`GET /api/quality/images/{id}/` and
  `quality_decision` in `GET /api/images/my/`) measures sharpness on the face: `measurements.sharpness_basis` is `face` or `frame`,
  `measurements.face_blur_warning` is `true` for a blurred face (show a "will be sharpened" hint, not an error), and
  `rejection_reasons` no longer contains `severe_blur`, `borderline_blur` or `borderline_resolution` for such a photograph. A photograph
  is still rejected as `resolution_too_low` when its shorter edge is under 240 px, and a photograph with no face and a severely blurred
  frame is still rejected.
- **Very small photographs are enlarged (new, on unless the operator sets `SMALL_IMAGE_ENLARGE_ENABLED=False`).** A photograph whose shorter
  edge is under 240 px (`SMALL_IMAGE_ENLARGE_BELOW_EDGE`) and at least 60 px (a smaller picture would stay under the line even at 4 times, so it is left
  alone and still refused as too small) is enlarged 2 or 4 times while it is standardized, so it is no longer refused for its size and
  face validation, grouping and enhancement see a usable image. The uploaded original is never changed. `GET /api/images/{id}/preprocessing/` (and the
  preprocessing result wherever it is embedded) has a new field **`enlargement`**: `{}` when nothing was asked of it, else
  `{"applied": true, "from": [150, 200], "to": [600, 800], "scale": 4, "engine": "realesrgan_x4plus" | "opencv_lanczos", "fallback_reason": "", "seconds": 1.7}`
  (`{"applied": false, "reason": ...}` when enlarging failed; the upload still succeeded). The quality decision then measures the *enlarged* copy and says so:
  `measurements.enlarged_from` is `[width, height]` of the photograph as uploaded (absent otherwise). Show a note such as "This photo was small, so it was
  enlarged from 150 x 200; enlarging cannot add real detail", and do not present its resolution score as the size it was taken at. A no-face photograph
  can still be sent to review for blur afterwards (`borderline_blur`): an enlargement is soft, and that is reported, not hidden.
- **Rejected photos are never enhanced.** `POST /api/enhancements/images/{id}/` answers 400
  ("This photo was rejected by the checks, so it is not enhanced.") for a photo refused at upload or
  rejected by its quality decision or face validation, and `POST /api/enhancements/batches/{id}/`
  silently leaves such photos out of the job (`total_images` counts only the ones enhanced; 400 when
  none is left). Photos that `need_review` can still be enhanced on request. The rule and its table
  are in `enhancement/eligibility.py`.
- **429 on start** means the user already has the maximum number of active
  enhancement jobs. The message states the limit; retry after some finish.
- Compare before/after: `GET /api/enhancements/results/{id}/comparison/`.
- **Model comparison** (one image through several profiles side by side):
  `POST /api/enhancements/images/{image_id}/compare/`, then
  `GET /api/enhancements/comparisons/{id}/`, previews at
  `.../outputs/{profile}/preview/`, CSV at `.../report/`.

**Editor (sliders).** Get the slider ranges from
`GET /api/enhancements/editor/configuration/`. Create an edit with
`POST /api/enhancements/results/{result_id}/edits/`, then poll
`GET /api/enhancements/edits/{id}/`. Edits are immutable versions:
`POST .../edits/{id}/activate/` picks the current one and
`GET .../edits/{id}/download/` fetches it. The original is never overwritten.
While you poll (every 1.5 s is fine), the edit says how far it is: `stage` is `queued` (waiting for a worker), `reading`, `enhancing` or `finishing`, and
`progress_percent` is a coarse 0 to 100 that never goes backwards (0 queued, about 5 reading, 15 to 80 enhancing, 85 to 95 finishing, 100 completed). Both are
`null` once the edit has `failed` or been `cancelled`, and `stage` is `null` when it has `completed`. Use them for a progress bar; keep deciding "done" by `status`.
`quality_metadata.timings_ms` (milliseconds per stage, with `total`), `image_pixels` and `guard_attempts` (`{count, ms}`) say where a save spent its time.
Show client-side previews while the user drags sliders and submit the final
values for the full-resolution server render.

## 5. People (identity groups)

Faces are grouped into "person folders". A photo with no detectable face is
analysed as `completed` with `face_count` 0 (not `failed`), so do not show an
error for it.

**Group photographs (changed).** A photograph with several people is accepted when at least one face is big enough to use, however many faces it has (it used to go to review above five
faces or when one face was small), and it appears in **each** person's folder. Faces too small to place in a folder are skipped, and the photograph's quality `tips` say how many
("2 faces are too small to place in a person folder..."); at most `IDENTITY_MAX_FACES_PER_IMAGE` (20) faces of one photograph are placed, the largest first. A photograph whose only
face is small is no longer **rejected**: it is kept (`needs_review` in the face check, so "accepted with a tip" in a personal workspace) and simply not placed in a folder.

**People you can tap (new): avatars, chips, search.**

- `GET /api/identities/people/?q=&limit=&include_unnamed=` returns the person folders as **chips**, most photographs first: `[{id, display_name, is_named, photo_count, avatar_url}]`. Folders with
  no photograph and inactive ones are not listed. `q` filters by name, `limit` defaults to 12 (at most 50, larger is capped), `include_unnamed=false` hides folders that still have their automatic
  name. `is_named: false` means the folder is still "Person 001": show an "Add a name" prompt (`POST /api/identities/groups/{id}/rename/`). Call it with no `q` for what a search bar offers before
  anything is typed.
- `GET /api/identities/groups/{id}/avatar/?size=96|192|384` is a **square JPEG crop of the person's best face** (bright enough first, then the detector's confidence, then size), with a margin so the
  head shows. Fetch it with the Authorization header and a blob URL, like a thumbnail (`Cache-Control: private, max-age=3600`). `404` while the folder has no face that can be cropped, `400` for another size.
  The folder objects (`/api/identities/groups/`, `/{id}/`) gained `avatar_url` (null while there is no face). Nothing new is analysed: the crop uses the face box and the standardized picture the
  analysis already stored, under the person's consent; withdrawing consent with erasure removes the folders and so their avatars.
- `GET /api/search/?q=` gained **`people`** (the same chip objects, for folders whose name contains `q`) next to the existing `groups` (unchanged).
- `GET /api/images/my/?person=<folder id>` lists the photographs that contain that person; **repeat the parameter for photographs that contain all of those people**
  (`?person=3&person=7`). Only faces placed in the folder count (a suggested face waits for a decision and does not); another person's folder id gives an empty list; a non-number is a `400`.
  It combines with every other filter (`favorite`, `status`, `batch`, ...). Measured cost of one avatar: 6 to 52 ms from a JPEG standardized picture (1 to 12 megapixels), up to 270 ms from a 12 MP PNG.

- **Consent first.** Face grouping only runs for users who granted biometric
  consent. `GET /api/privacy/consents/` returns the current state and policy
  version; `POST /api/privacy/consents/ {granted, policy_version}` grants or
  withdraws. Withdrawing with `erase_facial_data: true` also deletes the data.
  Show a consent screen before the first upload and disable the People page
  until consent exists.
- `GET /api/identities/groups/` and `/{id}/` list and show folders
  (`display_name`, `member_count`, `confirmed_count`, `suggested_count`, `preview_image`). `confirmed_count` is the faces placed in the folder (the same number
  as `member_count`); `suggested_count` is the faces that look like this person but wait for a decision (`membership_state: "needs_review"` on a photo of
  `GET .../groups/{id}/images/`): they are listed with the folder's photos but are **not** in `member_count`, so show "5 photos, 3 confirmed, 2 suggested"
  from these two numbers instead of counting tiles. A photograph the checks **reject** is never in a folder: it is taken out when it is rejected and a folder left
  empty is archived (state `archived`, the id still resolves; a folder the person named is kept). `GET /api/identities/images/{image_id}/` (the photo's identity analysis) adds `evidence`: the faces
  the detector found that were not grouped (`skipped_faces`, with `reason` `face_too_small` or `detector_score_below_floor`) and the limits used. **Embeddings and centroids
  are never returned**; do not expect them. Each folder also has
  `cover_images` (up to four distinct photos, `[{id, thumbnail_url}]`, empty for
  an empty folder) and `matching_confidence` (the average of its members'
  similarity scores **as a percentage, 0-100 with one decimal**, so `98.4` can be
  shown as `98.4%`; `null` when no member has a score, never a made-up number).
  The detail response also keeps `memberships` (raw 0-1 `similarity_score` per
  face).
- **Photos in a folder:** `GET /api/identities/groups/{id}/images/` (paginated:
  `{count, next, previous, results}`, `?page=`, `?page_size=` up to 100, newest
  first). Each row: `id`, `original_name`, `thumbnail_url`, `file_size`,
  `width`, `height`, `display_status` (the same value `GET /api/images/my/`
  reports), `matching_confidence` (this photo's best score in the folder, same
  percentage scale; `null` when unscored), `membership_id` and `membership_state`
  (`confirmed`, `needs_review`, ...): the membership that puts this photo in this
  folder, which is what the actions take: move to another folder
  `PATCH /api/identities/memberships/{membership_id}/move/ {target_group_id}`, take it out
  `PATCH .../memberships/{membership_id}/remove/`, make it the cover
  `PATCH /api/identities/groups/{id}/profile/ {membership_id}`. A photo with two faces of the
  same person appears once, with the better-matching face's membership. Another user's folder
  is a 404.
- **Why a new folder.** A face that started its own folder carries `evidence.best_score` (how close the nearest
  existing folder was, always below the new-folder threshold; `null` when it was the owner's first folder) and
  `evidence.closest_group_id` on its membership. Its `similarity_score` stays `null`: the face is not compared with
  the folder it starts. For matched and review faces `best_score` is the score against the suggested folder, as before.
- **Folders that may be the same person.** Photographs are grouped one after the other, so the first ones of a batch can open separate folders before later
  ones show they match. `GET /api/identities/groups/suggested-merges/` (the signed-in owner's own folders only, never anyone else's) answers a plain array
  `[{source_group, target_group, similarity, source: {id, display_name, member_count}, target: {...}}]`, most alike first (up to 50): `similarity` is 0 to 1
  (cosine of the folders' average faces; a pair is listed from the configured limit, `0.40` for the SFace model), `source_group` is the folder to merge away (the
  one with fewer photos) and `target_group` the one to keep. Show it as "Person 003 and Person 001 may be the same person - Merge?" and merge with
  `POST /api/identities/groups/{target_group}/merge/ {"source_group_id": source_group}`. **Nothing is merged for the person.** The list is worked out when asked
  (nothing is stored), so after a merge the pair is gone. When a batch has been fully grouped and some of its folders may match, the owner also gets one
  notification, `event_type` `identity_merge_suggested` ("N of your person folders may be the same person", `action_path` `/dashboard/groups`, once per batch,
  only when N > 0); it follows the `person_group_created` switch (default on). `suggested_merge_count` in the batch progress is how many pairs involve a folder
  that batch put photographs in.
- **A photo the checks reject leaves no trace.** If a photo is rejected after it was grouped (a manual analysis, or a moderator rejecting it), it leaves its
  folders, a folder only that photo made and that is now empty is archived (`state: "archived"`, the id still resolves; a folder the owner named is kept), and any
  moderator review of its identity that was still open is closed as `rejected` with the reason "Photo was rejected" and no deciding moderator (`decided_by` is
  `null`, which is how a closing by the system differs from a decision).
- Manage folders: `PATCH .../groups/{id}/rename/ {display_name}`,
  `POST .../groups/{id}/merge/ {source_group_id, reason}` (merges the source
  into this folder), `PATCH .../groups/{id}/profile/`,
  `DELETE .../groups/{id}/delete/` (only when empty), and per face
  `PATCH /api/identities/memberships/{id}/move/` and `/remove/`.
- **Review queue.** `GET /api/identities/review/` lists uncertain assignments
  (`state: needs_review`). Resolve one with
  `PATCH /api/identities/assignments/{id}/review/ {action, group_id?}` where
  `action` is `confirm`, `select_group` (needs `group_id`), `new_group` or
  `reject`.
- Change history (renames, merges, moves, reviews): `GET /api/identities/history/`.
  There is no one-click undo endpoint; to reverse a change, use the opposite
  operation (for example move a face back).
- **Search by photo.** `POST /api/identities/search/` (multipart, field
  `query_image`) finds the caller's own photos that contain the face in the
  uploaded photo. Needs biometric consent, same as the rest of this section.
  Response: `{faces_detected, matches: [{group_id, display_name, similarity,
  photo_count, photos: [{id, original_name, thumbnail_url, download_url}]}]}`.
  `faces_detected: false` means no face was found in the uploaded photo at all
  (show "no face found"); `faces_detected: true` with an empty `matches` list
  means a face was found but it doesn't match anyone in the account yet (show
  "no matches"). The query photo and its embedding are never stored anywhere;
  do not expect a way to look up a past search.
- Per-folder ZIP export is async: `POST .../groups/{group_id}/exports/`, poll
  `GET /api/identities/exports/{id}/`, then `.../download/`. The request body
  accepts an optional `image_ids: [int, ...]` to export only those photos;
  leave it out to export the whole folder. Every id must be a photo currently
  in that folder, otherwise the answer is **400** `{"image_ids": ["These photos
  are not in this folder: 12, 40."]}` and nothing is queued. An empty list is a
  400 too (omit the field, or send `null`, for "everything"). Two requests for
  the same set of photos share one queued job; a different set is its own job.
  The export object reports the `image_ids` it was made for (`[]` = all) and
  regenerating keeps them.
- **Share a finished folder ZIP:** `POST /api/identities/exports/{id}/share/
  {ttl_hours?, label?}` -> **201** with the same body as an image share link
  (`id`, `state`, `expires_at`, `token`, `public_url`, `info_url`, ...,
  `identity_export: <export id>`, `result: null`). The `token` appears **only in
  this response**; copy `public_url` straight away. Anyone with `public_url`
  (no login) gets the ZIP as a download. The link never outlives the ZIP (its
  `expires_at` is capped at the export's), is revoked with
  `POST /api/shares/{link id}/revoke/`, and answers **410** once revoked or
  expired. Only a `ready`, unexpired export can be shared: a queued/generating
  one is **409**, an expired one **410** (regenerate it first). Share links
  count toward the same per-user limit as image links.

## 5-albums. Albums (new)

An album is a collection the person makes by hand ("Eid 2026"): a title, a description, a cover and the photographs they put in. A photograph can be in any number of albums; deleting a
photograph takes it out of its albums; **deleting an album never deletes photographs**. Everything is owner-scoped (another person's album is a `404`).

```text
GET    /api/albums/?q=&image=&page=&page_size=      list, most recently changed first (image=<id>: only albums that contain it: "already in 2 albums")
POST   /api/albums/   {title, description?, image_ids?: [..up to 200]}  -> 201 album + added_ids, not_found_ids
GET    /api/albums/{id}/                            PATCH /api/albums/{id}/ {title?, description?}        DELETE /api/albums/{id}/  -> 204
GET    /api/albums/{id}/images/?page=&page_size=    the photographs, newest added first, as the same rows the gallery lists (status, thumbnail, is_favorite, ...)
POST   /api/albums/{id}/images/        {image_ids: [..up to 200]}  -> {added_ids, already_in_album_ids, not_found_ids, photo_count}   (idempotent)
POST   /api/albums/{id}/images/remove/ {image_ids: [..]}           -> {removed_ids, not_in_album_ids, photo_count}
PUT    /api/albums/{id}/cover/  {image_id}       DELETE -> automatic cover again
GET    /api/images/my/?album=<id>               the gallery filtered to one album (combines with the other filters)
```

An album is `{id, title, description, photo_count, cover_image, cover_thumbnail_url, created_at, updated_at}`. The cover is the one chosen, else the first photograph added (`null` for an empty album);
it goes back to automatic when its photograph leaves the album. `photo_count` counts photographs that are still stored. The title is trimmed (1 to 120 characters). Limits: `ALBUM_MAX_PER_USER`
(500) albums per person and `ALBUM_MAX_IMAGES` (10,000) photographs per album, each a `400` with a plain sentence. `GET /api/search/?q=` gained **`albums`** (albums whose title contains `q`).
A photograph id that is not the person's, does not exist or was refused at upload is never added; it comes back in `not_found_ids`.

## 5-memories. Memories and capture dates (new)

A **memory** is a few of the best photographs of a moment, chosen for the person: "2 years ago today", "Moments from 12 Oct", "With Ayesha". Nothing is stored: it is worked out from the
person's own photographs when it is asked for (so it is always current and never shows a deleted or rejected photograph), in tens of milliseconds even for 20,000 photographs.

```text
GET /api/memories/?limit=10        -> [{id, kind, title, subtitle, start, end, photo_count, cover_thumbnail_url, preview_thumbnail_urls: [..4], filter: {..}}]
GET /api/memories/{id}/            -> the card + images: [..gallery rows of its best photographs (up to 24), oldest first]      (404: the id no longer exists)
```

- `kind` is `on_this_day` (id `otd:<year>`: at least 3 photographs taken on today's date in an earlier year, at most 3 such years), `event` (id `event:<first photograph id>`: at least 5 photographs
  in the last 180 days with gaps under 6 hours; newest first, at most 6) or `person` (id `person:<folder id>:<YYYY-MM>`: a **named** person with at least 10 photographs, at most 3; unnamed
  folders have no memory). Ids stay the same for the same photographs; an event id changes if an earlier photograph joins the event, so **take ids from the list**, and treat a 404 as "gone".
- The **cover** is the best photograph and `preview_thumbnail_urls` the best four (favourites first, then the higher quality score, then one with a face, then the earlier one). A photograph that
  duplicates one already chosen, or was taken within a minute of one, is skipped, so a burst of ten shots gives one. `limit` defaults to 10 (at most 30).
- `photo_count` is every photograph of the moment; to show them all, call `GET /api/images/my/` with the card's **`filter`** (`taken_from`/`taken_to` dates, or `person`).
- **Capture dates.** Photographs now carry `taken_at` (the EXIF capture time, read at upload; `null` when the file has none, then the upload time is used on timelines). `GET /api/images/my/` accepts
  `taken_from=YYYY-MM-DD` and `taken_to=YYYY-MM-DD` (inclusive, on the capture date, or the upload date when there is no EXIF time; anything else is a 400). Photographs uploaded before this change
  have no `taken_at` until the operator runs `python manage.py backfill_taken_at` (it reads the stored originals, changes nothing else, and is safe to run again). EXIF has no time zone unless
  `OffsetTime` is present, so the stored time is the wall-clock time taken as UTC: it is right to the day, which is what a memory needs.

## 5-trash. Trash (new)

Deleting with an undo. A photograph moved to the trash is gone from **every** other screen at once (My Images, search, albums, person folders and their counts, memories, events, the
editor, reports, duplicates, shares) and can be restored for `retention_days` (30 by default); a daily job then deletes it for good. The existing permanent deletes
(`DELETE /api/images/{id}/`, `POST /api/images/bulk-delete/`) are **unchanged and immediate**: offer both ("Move to trash" as the normal action, "Delete forever" in the trash).

```text
POST   /api/images/trash/             {image_ids: [..up to 200]}  -> {trashed_ids, skipped: [{id, reason}], not_found_ids, retention_days}
GET    /api/images/trash/?search=&page=&page_size=                -> paged rows {id, original_name, thumbnail_url, width, height, file_size, is_favorite, taken_at, created_at, trashed_at, purge_at, days_left}
POST   /api/images/trash/restore/     {image_ids: [..]}           -> {restored_ids, not_found_ids, folders_restored}
POST   /api/images/trash/empty/       {confirm: true}             -> {deleted, remaining}        (500 per call: repeat while remaining > 0)
DELETE /api/images/trash/{id}/                                    -> 204 (permanent); 404 when it is not in your trash
GET    /api/images/trash/{id}/thumbnail/                          -> image/jpeg (fetch with the Authorization header, like any thumbnail)
```

- **`skipped`** are photographs that cannot be moved right now and nothing was done for them: `still_processing` (their checks or an enhancement are still running: say "try again in a
  moment") and `in_review` (an organization moderator has not decided it yet: offer "Delete forever" or wait). The others in the same request **are** moved. A photograph in a personal
  workspace is never `in_review` (see section 3).
- **`not_found_ids`**: not yours, not a stored photograph (a refused upload), unknown, or already in the trash.
- A trashed photograph answers **404** at every ordinary address (`/api/images/{id}/`, `/thumbnail/`, `/download/`, its checks, its enhancement results, its share link answers 410 until it is restored). Use the
  trash's own thumbnail address for the trash screen; `thumbnail_url` in the trash rows already points to it.
- **Restore** puts back the favourite mark, the album places and, when the folder still exists and is not a different one now, the person-folder membership (a folder the trash left empty and archived
  becomes active again). A face that was placed in another folder meanwhile stays there. Nothing about the checks' results changes.
- **Counts.** Every figure elsewhere drops by the trashed photographs (album `photo_count` and cover, person `photo_count`, dashboard totals). The dashboard summary also has `trash_images` and
  `trash_bytes`, and `storage_bytes` **still includes** the trash (the files are still stored). The data export (`GET /api/privacy/export/`) lists trashed photographs with their `trashed_at`.
- **Not in the trash view:** photographs that were refused at upload, and ones permanently deleted.
- The retention is the server setting `TRASH_RETENTION_DAYS`; show `days_left` from each row, not a fixed 30.

## 5-places. Places: where a photograph was taken (new)

"Places" groups the person's own photographs by where they were taken ("Lahore", "Murree", "Paris"). The position comes from the photograph's own EXIF GPS block when it has one, is read on the
server, and **no coordinate ever leaves it**. Names come from an offline file; until the server has installed and approved it, photographs keep a position but get no named place.

```text
GET    /api/places/?q=&country=PK&limit=24   -> {attribution, names_available, places: [{id, name, region, country, country_code, photo_count, cover_thumbnail_url, latitude, longitude}]}   most photographs first
GET    /api/places/{id}/                      -> one of those (404 if you have no photograph there)
GET    /api/images/my/?place=<id>             the gallery filtered to one place;  ?country=PK  to one country (two letters)
GET    /api/images/{id}/location/             -> {latitude, longitude, place: {id, name, region, country, country_code} | null}   (404 if it has none); the photograph's owner only
DELETE /api/images/{id}/location/             -> 204   take the location off this photograph
POST   /api/images/location/remove/  {image_ids: [..up to 200]} -> {removed_ids, without_location_ids, not_found_ids}
```

- **Photograph rows** (`GET /api/images/my/`, albums, memories) gained `place` (`{id, name, region, country, country_code}` or `null`) and `has_location` (true when it has a position). They never carry
  coordinates; only `GET /api/images/{id}/location/` does, and only for the owner. Moderators, administrators, share links, ZIPs and organization exports carry none.
- **`latitude` and `longitude` on a place are the place's own centre**, not any photograph's position. Draw a map pin for a photograph from its own `/location/` call, never from the places list.
- **You must show the credit** in `attribution` ("Place names: GeoNames ... CC BY 4.0") wherever place names are shown, for example under the Places screen.
- `names_available: false` means this server has no approved place-name file: hide the Places strip (the list is empty) rather than show an empty screen. Photographs still have `has_location`.
- **Remove a location** with `DELETE` or the bulk `POST`. It is gone from FaceCloud's records and views and is never read again, but it is **not** removed from the stored original file: say
  so in the confirmation ("removes it from your library; the downloaded original may still contain it").
- A photograph in the trash is in no place count and has no location endpoint until it is restored. `GET /api/search/?q=` gained `places` (places whose name contains `q`).
- Naming is by the nearest listed town within 50 km (a suburb or village next to a city is named after the city; in very large cities a district may be named: "Paris 16 Passy"). A position far from
  any settlement (open sea, desert) has coordinates but no place. Photographs uploaded before this feature appear after the operator runs `python manage.py backfill_location`.
- Rate limit: 1200 requests per hour per user.

## 5a. Duplicates

`GET /api/duplicates/results/` and `/images/{image_id}/matches/` list suspected
duplicates. Confirm or dismiss with
`PATCH /api/duplicates/matches/{id}/review/`. An exact checksum match cannot be
dismissed (the API returns a 400).

## 5b. Events (moments timeline)

Photos are grouped into events by upload time. No face data is used.

- `GET /api/events/` lists events, newest first (paginated). Each has `id`,
  `start`, `end`, `photo_count`, `cover_image_id`, `cover_download_url` and
  `cover_thumbnail_url`.
- `GET /api/events/{id}/` shows one event; `GET /api/events/{id}/photos/` lists
  its photos (paginated, oldest first) with `thumbnail_url` and `download_url` for each.
- The optional `gap_hours` query parameter (0.25-168) sets how long a silence
  starts a new event. Send the same value on every call, because event ids
  depend on it. Omit it to use the server default (6 hours).
- Use `cover_thumbnail_url` for the tile; `cover_download_url` is the full original.
- Event ids are not permanent: a photo that is not the first of its event is
  not a valid id. Always take ids from the list response.

## 5c. Collages (removed)

Photo collages no longer exist. `/api/collages/` (list, create, detail, delete, download) answers `404`, the `collage` kind is gone from the failed-jobs
list (`GET /api/system/failed-jobs/`), and a person collage can no longer be refused for missing consent. Remove the collage screens and buttons; nothing
replaces them, and events, thumbnails and downloads are unchanged.

## 6. Reports, packages, sharing

**Reports** (`json`, `csv`, `pdf`) and **download packages** (ZIP) are
asynchronous jobs with the same lifecycle:

```text
POST /api/reports/batches/{batch_id}/            {format}
POST /api/reports/images/{image_id}/             {format}
POST /api/downloads/batches/{batch_id}/packages/ {package_type, report_format}
   -> 200 (an identical job already exists) or 202 (queued)
GET  /api/reports/{id}/  |  /api/downloads/packages/{id}/    poll status
GET  .../{id}/download/                                       once status is "ready"
POST .../{id}/regenerate/                                     after expiry or failure
```

Statuses: `queued, generating, ready, failed` (packages also `expired`).
Asking for a package that cannot be made yet is a **409** whose body is
`{"detail": "<sentence>", "code": "<code>", ...}`; branch on `code`:
`enhancement_required` (photos passed the checks, the upload did not ask for automatic enhancement and nobody enhanced any; also
`accepted_images: n`; call `POST /api/enhancements/batches/{id}/`, wait, then ask
again; an upload that did ask for it never answers this), `no_accepted_images` (nothing passed, nothing to enhance),
`batch_still_processing`, `batch_empty`. Downloading before `ready` returns
**409**; after expiry **410**. Packages
(`package_type`: `accepted_results` or `complete_batch`) expire after a
configurable time, so show an expiry and offer "regenerate".

**What `accepted_results` holds (changed).** For each accepted photograph, its **enhanced** version (folder `enhanced/`) when it has one, and its **original** (folder
`original/`) when it does not (an automatic enhancement that was skipped or failed). `manifest.json` says which, file by file: each entry of `files` has `version`
(`enhanced` or `original`), and an enhanced one has `enhancement_outcome` (`enhanced`, `partially_enhanced`, `original_preserved`) while an original has a `reason`;
`summary` has `enhanced_files` and `original_files`. Earlier the package held only the enhanced photographs and listed the others under `skipped_files`.

**What a processing report contains** (`payload`, and the JSON file). A **batch** report's `summary` is
`{total_images, accepted_uploads, rejected_uploads, processing_status, status_counts, total_input_size}` plus three fields:
`images_deleted` (`true` when any photo of the upload no longer exists), `images_remaining` (how many are still stored, which is also the length of `images`) and
`enhanced_images` (how many of the remaining photos were enhanced; `null` when none remain, because it is then unknown). The totals always come from the batch itself, so
**after photos are deleted a report no longer shows `total_images: 0` next to `accepted_uploads: 2`**: `total_images` stays the size of the upload, `images` shrinks to what
remains, and `status_counts` has a `deleted` entry for the photos that are gone, so the counts always add up to `total_images` (`{"deleted": 3}` when everything is gone).
Show a line such as "The photos of this upload have been deleted; only totals are shown" when `images_deleted` is `true`. Nothing was renamed or removed.
The **PDF** is written for people: the date as `19 Oct 2026, 09:29 UTC`, sizes in KB / MB, results as separate rows (Accepted, Rejected, Needs review, plus any other that occurred),
statuses, reasons and duplicate decisions in words, long file names wrapped at `_ - .`, the reason for each rejected photo, scores labelled "out of 100", and a footer with the report type,
the source and "Page n of N". JSON and CSV keep the raw codes, raw numbers and ISO dates (the CSV is unchanged; for a batch whose photos are all deleted it is just the header).

**Share links.** `POST /api/shares/ {result, ttl_hours?, label?, watermark?,
watermark_position?, watermark_opacity?, watermark_text?}` returns the link
**including the raw `token` exactly once**, plus `public_url` and `info_url`.
The token is never shown again, so display and copy it immediately.
`watermark`: `none, small, facecloud, username, custom`; `watermark_position`:
`bottom_right, bottom_left, top_right, top_left, center, tile`. Revoke with
`POST /api/shares/{id}/revoke/`. Preview the watermark before sharing with
`GET /api/enhancements/results/{id}/watermark-preview/`.

**Share link shape.** `GET /api/shares/` and `/{id}/` never return a token or a
public address. A link made from a person-folder ZIP has `identity_export` set
and `result` null; an image link has `result` set and `identity_export` null.

**Downloads screen.** `GET /api/downloads/` (paginated history, newest first)
keeps its original fields and adds, per row:

| Field | Meaning |
|---|---|
| `kind` | `single` (one photo: original, standardized, enhanced, edited), `batch` (a ZIP: batch package or person-folder ZIP) or `report` |
| `expires_at` | When the file ends (batch package / folder ZIP expiry); for an enhanced or edited photo with a live share link, when that link ends; otherwise `null` |
| `image_count` | Photos in the ZIP or batch report; `null` for a single photo and for an image report |
| `has_active_link` | `true` when a live (not revoked, not expired) share link exists for it. The link itself is never listed |

To give the user a **fresh secure link** from a row, use the existing endpoints
(`resource_type` and `resource_id` say what the row is):

| `resource_type` | Mint a link with |
|---|---|
| `enhanced_image` (`resource_id` = result id) | `POST /api/shares/ {result: resource_id}` |
| `edited_image` (`resource_id` = edit id) | `POST /api/shares/ {result: <its result>, edit: resource_id}` |
| `identity_group_package` (`resource_id` = export id) | `POST /api/identities/exports/{resource_id}/share/`; if expired, `POST .../exports/{id}/regenerate/` first |
| `batch_package` (`resource_id` = package id) | no public link; while `ready` use the authenticated `/api/downloads/packages/{id}/download/`, and `POST .../packages/{id}/regenerate/` after expiry |
| `original_image`, `standardized_image`, `processing_report` | no share link; use the authenticated download endpoints |

**Public share page** (no login, no Authorization header):
`GET /api/shares/public/{token}/info/` for metadata (size, expiry, whether it is
watermarked) and `GET /api/shares/public/{token}/` for the image. 404 means the
token is unknown, **410** means expired, revoked or the owner is inactive. For a
person-folder link `info` reports `content_type: "application/zip"` and the
public address answers with the ZIP as an attachment (`Content-Disposition:
attachment`), never watermarked.

## 7. Organizations and the moderator area

Every user has a **personal organization** created at signup. Users can also
belong to shared organizations (owner, moderator or member roles) by accepting
an invitation.

**Every organization endpoint has the organization id in the URL**
(`/api/organizations/{organization_id}/...`). The backend re-checks membership
on every request. An organization you do not belong to is **404**; an
organization you belong to but lack the role for is **403**. When the user
belongs to several organizations, keep the selected organization id in your app
state and put it in the URL; changing it client-side grants nothing.

- `GET /api/organizations/` lists the caller's organizations (use it for the
  organization switcher). `GET /api/organizations/{id}/` shows one.
- Members and invitations (owner or moderator only):
  `GET .../members/`, `PATCH/DELETE .../members/{membership_id}/`,
  `GET/POST .../invitations/ {email, role}` (role `moderator` or `member`;
  only an owner can invite a moderator), `POST .../invitations/{id}/resend/`,
  `DELETE .../invitations/{id}/`. "Remove" only removes organization access, not
  the person's account.
- **Accepting an invitation:** the emailed link is
  `{FRONTEND_URL}/invitations/{token}`. Build that route. **First show who is inviting**, without signing in:
  `GET /api/organizations/invitations/{token}/` (public; send no Authorization header) answers
  `{"organization_name": "Preview Co", "email": "guest@example.com", "role": "moderator", "expires_at": "2026-10-21T19:47:53Z"}`
  (`role` is `member` or `moderator`). An unknown, used, cancelled or expired token all answer the same
  `404 {"detail": "This invitation is invalid, used or expired."}`: show that sentence and no other hint. The answer
  is `Cache-Control: no-store` and rate limited (scope `invitation`, 30/hour per address). Then the person signs in
  (or registers) with **that email**, and the logged-in user calls
  `POST /api/organizations/invitations/accept/ {token}` (unchanged).
- **Audit trail (Moderation Logs and Audit screens):** `GET .../audit/` (owner or moderator; a plain array, or paginated with
  `?page=&page_size=`), newest first. Each event keeps its stored fields (`id`, `actor{id,username,email}`, `action`,
  `target_type`, `target_id`, `previous_value`, `new_value`, `reason`, `created_at`) and adds `actor_name`, `message` (a sentence
  without the actor, for example "Approved face.jpg."), `target_label` (the photo, upload or folder name; `null` when none or gone)
  and `from_status` / `to_status` (the review's status before and after a decision, `null` for other events and for decisions
  recorded before this was kept). Filters: `?action=` (for example `moderator_review_decided`), `?actor=<user id>`, `?q=` (case-insensitive, up to
  100 characters; finds the actor's name or email, the reason, the action's name or the fixed words of its sentence ("escalated"), a decision's
  wording ("approved", "rejected", "corrected": a decision's `message` is "Approved face.jpg.", so `q=approved` finds exactly the approvals),
  and the **name of the photo, upload or person folder** the event was about ("face.jpg", "staff_q3.zip")), `?date_from=`, `?date_to=` (a bare date includes
  that whole day), `?target_type=` and `?target_id=`; a bad value is a 400. The history of one review is
  `?target_type=ModeratorReview&target_id={review id}`. A "user notified" column is
  the event `member_notified_of_review` ("Told the member about the decision on face.jpg.") on the same review: the member is told about an approve, reject or correct decision
  (section 8), and that is recorded here. The older `Audit trail` description follows.
- Audit trail (stored fields): `GET .../audit/`. Organization-wide data export (async):
  `POST .../export/`, poll `GET .../export/{id}/`, download
  `.../export/{id}/download/`.

**Moderator review queue** (owner or moderator):

```text
GET  .../moderator/reviews/?status=&source=&overdue=&assignment=&ordering=
GET  .../moderator/reviews/{id}/
POST .../moderator/reviews/{id}/decision/   {action, reason?, notes?, message_to_member?, group_id?}
POST .../moderator/reviews/{id}/assign/     {moderator_id}
POST .../moderator/reviews/{id}/escalate/   {reason?}
GET  .../moderator/reviews/{id}/image/?size=  the photo, as a thumbnail; size is 256 (default) or 1024, anything else is a 400
POST .../moderator/reviews/{id}/claim/      take the review (soft lock)
POST .../moderator/reviews/{id}/release/    give it back to the queue
POST .../moderator/reviews/bulk-decision/   {review_ids[], action, reason?, notes?, message_to_member?}
POST .../moderator/reviews/{id}/merge-identity-groups/  {source_group_id?, target_group_id?, reason?}
GET  .../moderator/dashboard/               counts for the overview cards
GET  .../moderator/metrics/?days=30         per-moderator performance
```

Queue filters: `source` is `quality | duplicate | identity`, `overdue=true`
narrows to reviews past their deadline, `assignment` is `mine` or `unassigned`,
and `ordering=due_at` works to the deadline first (reviews without one last).

- `ai_confidence` is a **percentage, 0 to 100**, whatever raised the review.
  The three sources do not agree among themselves underneath — a quality score
  and a duplicate similarity are already 0-100, an identity similarity is 0-1 —
  so the backend normalises on the way in. Render it directly; do not multiply.
- `action` is `approve`, `reject`, `correct` or `keep_in_review`. For identity
  reviews `correct` with a `group_id` reassigns to that folder, without one it
  starts a new folder. A decided review returns **409** on a second decision.
- Merging groups is only for duplicate reviews. If either side maps to several
  groups the API returns 400 listing `candidates`; let the moderator pick and
  resend with explicit group ids.

**Deadlines.** Every review carries `due_at` and a derived `is_overdue`. The
dashboard adds `overdue`, `due_soon`, `unassigned`, `claimed_now`,
`oldest_open_age_hours` and the configured `sla_hours`, so the overview cards
can show the service level without a second request. A review that passes its
deadline while still undecided produces one `moderator_review_overdue`
notification -- once, not on every check -- to its assigned moderator, or to the
owners and moderators when nobody has picked it up.

**Seeing the photo.** `image/` serves the photo behind a review as a JPEG
thumbnail (`size` 256 by default, 1024 also configured; a photo is never
enlarged). It is keyed by **review**, not by image, so a moderator can only see
photos they have actually been asked to judge, and it serves thumbnails only --
there is no way to pull the original through a moderation role. The same
organization scope and owner/moderator role as the review apply, so losing the
role closes it at the same moment. It needs the `Authorization` header like any
other call, so load it with `fetch` and `URL.createObjectURL` rather than
putting the path straight in `<img src>`; remember to revoke the object URL.
Each review also carries `image_name`, the photo's own filename, so a queue row
is recognisable before its thumbnail arrives.

**Claiming.** `claim/` holds a review for the moderator so two people do not
work the same photo: the response carries `claimed_by`, `is_claimed` and
`claim_expires_at`. While someone holds a review, another moderator's
`decision/` or `escalate/` answers **409** with code `review_claimed`. The hold
expires on its own, so nothing is ever stranded; claiming your own review again
simply extends it. `release/` returns it to the queue -- the holder can always
release, and an organization owner can release somebody else's hold.

**Telling the member what was decided.** `approve`, `reject` and `correct` (a correction is an approval the moderator adjusted) create an in-app
notification for the member who uploaded the photograph, and an email copy when they turned emails on; `keep_in_review` and `escalate` do not. Never the
deciding moderator, and nothing when the photograph is the moderator's own. `message_to_member` (up to 500 characters, optional, on the single and the
bulk decision) is **the only free text the member is shown**: put the field in the decision form as "Message to the member" and make clear that it is seen
by them. `reason` and `notes` are internal and are never sent. If `message_to_member` is empty a standard plain sentence for the reason code is used
(for example "Your photo is too blurry. Take it again holding the camera steady."). The moderator review detail returns `message_to_member`. A bulk decision
tells each member once per photograph (the same message for all). A `correct` arrives as the event `photo_review_approved` ("Your photo was approved")
with the moderator's `message_to_member` as its text, and follows the member's `photo_accepted` switch; `reject` arrives as `photo_review_rejected` and
follows `photo_rejected`. The notification respects the member's `photo_accepted` / `photo_rejected`
preferences, and the organization's audit trail records that the member was told (`action: member_notified_of_review`, target: the review; it is not
recorded when a preference switched the notification off).

**Bulk decisions.** `bulk-decision/` takes up to 50 `review_ids` with `action`
`approve`, `reject` or `escalate` (`correct` is not available in bulk because it
needs a per-photo choice of person folder). The response is **200** with
`succeeded`, `succeeded_count`, `failed` (`[{review_id, detail}]`) and
`failed_count`: rows that were already decided, held by another moderator, or
not in this organization are reported individually and do **not** fail the rest,
so show per-row outcomes rather than one success or error.

**Performance.** `metrics/?days=30` returns `moderators[]` with
`decided_count`, `approved`, `rejected`, `corrected`, `on_time`, `late`,
`on_time_rate`, `average_decision_hours` and `slowest_decision_hours`, plus
`totals` including `open_now` and `overdue_now`. Decision time is measured from
when the review arrived, not from when it was assigned. A review with no
deadline counts as neither on time nor late, so `on_time_rate` is `null` when
nothing measurable was decided.
- **Batches screen.** `GET .../moderator/batches/?processing_status=` is a plain array, newest activity first. Each row:
  `id`, `owner_username`, `upload_type` (`single` / `multiple` / `zip`), `name` (the label, else the ZIP's file name, else
  `Upload <id>`), `processing_status`, `current_stage`, `progress_percentage` (mean of its photos, 0 to 100),
  `counts` (`accepted`, `rejected`, `needs_review`, `failed`: photos per outcome), `total_files`, `accepted_files`,
  `processed_files`, `processing_failed_files`, `retry_count`, `created_at`, and the rest as before. Example:
  `{"id": 2, "owner_username": "mem1", "upload_type": "zip", "name": "staff_portraits.zip", "processing_status": "partial",
  "progress_percentage": 90.0, "counts": {"accepted": 2, "rejected": 1, "needs_review": 1, "failed": 1}, "total_files": 5, ...}`.
  `GET .../moderator/batches/{id}/` has the same fields under `batch` and, for each photo in `images[]`, `file_size`
  (bytes), `outcome` (`accepted`, `rejected`, `needs_review`, `failed`, `cancelled` or `processing`) and `preview_url`
  (the moderator thumbnail, fetch it with the Authorization header; `null` for a file refused at upload). Thumbnails come in the
  configured sizes only (`THUMBNAIL_SIZES`, 256 and 1024): `?size=` with any other value, for example 800, is a 400
  `{"size": "Choose one of: 256, 1024."}`; the same holds for `.../moderator/images/{id}/preview/` and `.../reviews/{id}/image/`.
  Actions: `POST .../moderator/batches/{id}/retry/`, `/cancel/` and `/reprocess/`.
- **Identity Review screen.** Only person folders that hold photographs **submitted to this organization** are listed, and a moderator
  can only merge two folders of the **same member**. `GET .../moderator/identity-groups/?q=&owner=` (a plain array, or paged) returns
  `[{"id": 7, "display_name": "Person 001", "owner": {"id": 3, "username": "mem1"}, "member_count": 4, "photos_in_organization": 3,
  "matching_confidence": 91.5, "cover_images": [{"id": 14, "preview_url": ".../moderator/images/14/preview/"}], "updated_at": "..."}]`
  (up to three covers; fetch the previews with the Authorization header). `GET .../moderator/identity-groups/suggested-merges/` returns
  at most 20 pairs, most alike first: `[{"similarity": 99.6, "source": <folder>, "target": <folder>}]` where `source` is the folder to
  merge away (fewer photos, newer on a tie) and `target` the one to keep; the threshold is the identity model's own (`SFACE_MERGE_SUGGESTION_MIN_SIMILARITY`, 0.40: it
  suggests about 93 % of one person's split folders and fewer than 1 in 10,000 pairs of different people's, measured on a public benchmark), or
  `MODERATOR_MERGE_SUGGESTION_MIN_SIMILARITY` when that is set. `POST .../moderator/identity-groups/merge/ {source_group_id, target_group_id, reason?}` merges and is audited as
  `identity_groups_merged_by_moderator`; 400 when a folder is not this organization's or the two belong to different members.
  Suggestions are recomputed each time: **"Dismiss" is a browser-side choice** (nothing is stored). To move one photo to another folder,
  decide an identity review item with `select_group` (the existing review decision); there is no free-form move for a moderator.
- **Rejected Images screen.** `GET .../moderator/images/?decision=rejected` (also `needs_review`, `accepted`; `?q=` photo name or
  uploader, `?batch=<id>`; a plain array, or `?page=&page_size=`), newest first, only photos submitted to this organization (never a
  personal workspace). Each row: `id`, `original_name`, `batch_id`, `uploader{id,username}`, `created_at`, `width`, `height`,
  `file_size`, `decision` (`rejected` includes files refused at upload), `rejection_reasons` (reason codes; for a refused file its
  one sentence), `overall_score`, `preview_url` (`null` for a refused file) and `review_id` (the newest review of that photo here, for
  the "Manual Review" button: open `GET .../moderator/reviews/{review_id}/`; `null` when no review exists).
- **Image Review screen.** `GET .../moderator/reviews/{id}/` is the queue row plus an `image` object: `id`, `name`, `width`, `height`,
  `file_size`, `uploader{id,username}`, `face_count` (`null` before the face check ran), `quality{decision, overall_score,
  rejection_reasons, warnings}` and `preview_url` (the review-keyed thumbnail; fetch with the Authorization header). The queue list
  does not carry `image`. The history of the review is the audit trail filtered with `?target_type=ModeratorReview&target_id={id}`.
- **Photos only reach a team's moderators if the uploader files them there**: see "Submitting to an organization" in
  section 3 (`organization_id` on the upload requests).
- **Reports & Exports screen.** Each report in `GET .../moderator/reports/` (and the create / detail responses) keeps its fields and
  adds `requested_by_name` (the username; `null` if that account is gone) and `download_url` (an absolute URL to fetch the file with
  the Authorization header; `null` until `status` is `ready`). The history table's "Generated by" is `requested_by_name`; its date
  range is `date_from` / `date_to` (`null` means no window); its type reads from `report_type` (`review_summary`,
  `moderator_activity`, `rejection_reasons`, `quality_scores`, `batches`, `duplicate_decisions`, `general_activity`).
- Reports: `POST .../moderator/reports/ {report_type, format}` with
  `report_type` one of `review_summary`, `moderator_activity`, `rejection_reasons`,
  `quality_scores`, `batches` or `duplicate_decisions` (and `format` `json`, `csv` or
  `pdf`); then the same poll-and-download lifecycle as section 6. Every report has a
  `summary` (totals and breakdowns, good for a chart) and up to 200 `rows`. None of
  them contains facial data.

## 7b. Organizations and tenancy

FaceCloud is **multi-tenant**: each customer is an *organization*. Three kinds of person, never mixed:

| Who | Belongs to | Sees |
|---|---|---|
| **Platform administrator** (FaceCloud staff, `role: admin`) | no organization | the whole platform: `/api/system/*`, `/api/user/admin/*`, `/api/organizations/admin/` (list, create, read, rename, plan, suspend) |
| **Organization owner** | their organization (membership role `owner`) | everything in their organization; manages it (members, moderators, invitations, export) |
| **Moderator** | **one** organization (membership role `moderator`) | only what belongs to that organization (see below) |
| **Member / user** | their personal workspace, and optionally organizations they were invited to | their own photos; photos filed under an organization (`organization_id` on upload) are visible to that organization's moderators |

Isolation between organizations is enforced on the server: a record of another organization answers **404** (never 403, so its existence is not
revealed); an administrator-only endpoint answers 403 to everyone else.

**A moderator always has an organization.**
* Sign-in and refresh refuse a moderator account with none: `403` with `"code": "no_organization"` (see section 2). Show the sentence; do not show a
  generic error. Administrators and ordinary users are never affected.
* `POST /api/user/admin/invitations/ {email, role, organization_id}`: for `role: "moderator"` the `organization_id` is **required** (400
  `{"organization_id": [...]}` without it; 400 when it is not an active, shared organization); for any other role it is refused. The invitation
  and its list row carry `organization: {id, name}` (null for other roles); `POST /api/auth/invitations/inspect/` adds `organization_name`. Accepting the
  invitation creates the account **and** its active moderator membership in one step (invited by the administrator who sent it); if the
  organization cannot take the moderator any more the acceptance fails with 400 and nothing is created, and the link stays usable.
* `PATCH /api/user/admin/users/{id}/ {role: "moderator", organization_id}`: the same organization choice as creating a moderator account (the only
  active organization is chosen for you; with several, `organization_id` is required; with none, 400 "Create an organization first, then try
  again."). Not needed when the account already moderates an organization. The membership is created with the role change, or neither happens.
* Django admin (`/admin/`): saving an account as a moderator needs an organization chosen on the form (or an existing moderator membership).
* Operations: `python manage.py report_moderators_without_organization` lists (username, email) of stranded moderator accounts; it changes nothing.

**A moderator sees their own members** (`moderator_data_scope`, on every organization row of `GET /api/organizations/` and `GET /api/organizations/{id}/`,
read-only for moderators):

| Value | Meaning |
|---|---|
| `own_members` (the default for **new** organizations) | A moderator sees the members **they invited**, plus every member **no current moderator invited** (invited by the platform administrator, by an owner, by nobody, or by someone who is no longer a moderator), so no photograph is ever left without a reviewer. Never the members another current moderator invited. |
| `organization` (what every organization that existed before this setting keeps) | Every moderator sees the whole organization, as before. |

The **owner always sees everything.** Enforced on the server; the frontend only needs to show less. What is limited, for a moderator on `own_members`:
reviews (list, detail, image, claim, release, decide, escalate, assign, bulk decide, merge), batches (list, detail, retry, cancel, reprocess), photographs and
previews, person folders (list, suggested merges, merge), the dashboard and performance counts (visible members only), reports (built from what the requester
sees, and a moderator lists and downloads **only the reports they asked for**), the member list and the invitation list (staff rows stay; a member another moderator
invited does not appear, and invitations another moderator sent do not), the audit trail (their own actions, plus events about visible members and
objects, but not events about a hidden member and not another moderator's bulk decisions), and the review notifications (a failed-batch or overdue-review notice goes
only to the owner and the moderators who may see that member).

* **404, never 403, for something outside the visible set**, exactly as for another organization (a 403 would say it exists). A bulk decision treats a hidden review id
  like a missing one (`"No such review in this organization."`); ids in a request *body* (merging two folders) are validated, so they are a 400.
* Assigning a review to a moderator who may not see that member's photographs is a 400 on `moderator_id`.
* The organization-wide export (`GET/POST /api/organizations/{id}/export/...`) covers everyone's data, so on `own_members` it is for the **owner only** (403 for a
  moderator) and `capabilities.export_organization` is `false` for that moderator: hide the button.
* When the moderator who invited someone is removed or demoted, those members become visible to every moderator immediately and that moderator's open reviews go back to the queue.
* The setting is changed by the owner (`PATCH /api/organizations/{id}/`) or the platform administrator (`PATCH /api/organizations/admin/{id}/`), see below.

**The owner console** (what an organization's owner needs):

* **Which organizations does this person own?** The login response (and the response that accepting an account invitation returns, which also signs the person in) has `organization_roles`:
  `[{organization_id, name, role, status}]`, the shared organizations (never the personal workspace) the person is an *active* member of, oldest first; `role` is `owner`,
  `moderator` or `member`, `status` is the organization's (`active` or `suspended`). An empty list means none. It is read once at sign-in: after the person accepts an
  invitation or is promoted, re-read `GET /api/organizations/` (every row has `membership.role`, `is_personal`, `status` and `capabilities`).
* `GET /api/organizations/{id}/` adds, **for the owner and the moderators only**, `owner` (`{id, username}` or `null`), `member_count` (every active membership, owner and
  moderators included) and `moderator_count`; a plain member does not get those three keys. `moderator_data_scope` and `plan` are on every row. `capabilities.edit_organization` is
  `true` only for an owner of a shared organization: show the edit form for that.
* `PATCH /api/organizations/{id}/ {name?, moderator_data_scope?, brand_name?, brand_accent?, reason?}` (owner only, 403 for a moderator or member; branding below): `name` 1 to 150 characters, `moderator_data_scope`
  `own_members` or `organization` (takes effect on the next request). The answer is the updated detail above. Sending `plan` or `status` is **403** ("The plan and the status are
  changed by the FaceCloud administrator"), even alongside other fields; a personal workspace and a suspended organization also answer 403. Audited as `organization_updated`
  with the old and new values (the owner and moderators read it in the audit trail).
* The owner already could, and still can, through the existing endpoints: deactivate (`PATCH members/{id}/ {status: "suspended"}`), reactivate (`{status: "active"}`), remove
  (`DELETE members/{id}/`), promote or demote (`{role: "moderator" | "owner" | "member"}`), invite moderators (`POST invitations/ {role: "moderator"}`; a moderator may only invite
  members), and request the organization export (`POST export/`, even when moderators see only their own members). The last owner cannot be removed, demoted or deactivated (409).

**Branding** (owner only to change, every member sees it). Three things, on every organization row (`GET /api/organizations/` and `GET /api/organizations/{id}/`, so on the switcher too):
`brand_name` (text shown to members instead of `name`; `""` when not set, so show `brand_name || name`), `brand_accent` (`#rrggbb`, or `""`) and `logo_url` (or `null`).

* `PATCH /api/organizations/{id}/ {brand_name?, brand_accent?}` (the owner's PATCH above; a blank value clears it). `brand_name`: trimmed, at most 100 characters, no line breaks or control characters (400).
  `brand_accent`: `#rgb` or `#rrggbb` in any case, stored as lower-case `#rrggbb` (400 otherwise: "Use a colour like #1a73e8."). The administrator cannot set branding. Audited in the same `organization_updated` event.
* **Logo**: `PUT /api/organizations/{id}/logo/` (multipart, field `logo`; owner only). PNG or JPEG only, **at most 1 MB** (400 `{"logo": ["The logo must be at most 1 MB."]}`; 400 for another format,
  for a file that is not an image, or for one with too many pixels). It is decoded and **re-encoded as a PNG of at most 512 px** (transparency kept, metadata and anything hidden in the file dropped), replacing the old
  one, which is deleted. The answer is the organization (with the new `logo_url`). `DELETE .../logo/` removes it (owner only; nothing to remove is not an error). Both are audited
  (`organization_logo_changed`, `organization_logo_removed`) and limited to 30 an hour (429).
* **Showing it**: `logo_url` is `/api/organizations/{id}/logo/?v=<milliseconds>`; `v` changes whenever the logo is replaced, so the browser cache is never stale. The file is **never public**: `GET` it **with the
  Authorization header** (an `<img src>` cannot send it: fetch it as a blob and use an object URL). Members of the organization get the PNG (`Cache-Control: private, max-age=3600`); anyone else 404, no sign-in 401; 404 when
  there is no logo (check `logo_url` first); 403 while the organization is suspended. `capabilities.edit_organization` says whether to show the edit form and the upload button.

**Plans, limits and usage.** `plan` (`free`, `team`, `enterprise`) decides how many **moderators** and plain **members** an organization may have (the owner takes no seat). Defaults: free 1 moderator
and 10 members, team 5 and 100, enterprise unlimited; set in `settings.ORGANIZATION_PLAN_LIMITS` (environment `ORG_PLAN_<PLAN>_<MODERATORS|MEMBERS>`, a number or `unlimited`). There is no billing:
the administrator sets the plan.

* **Where it is shown.** `limits` (`{moderators, members}`, `null` = unlimited) and `usage` (`{moderators, members}`) are on `GET /api/organizations/{id}/` (owner and moderators; not a plain
  member) and on every row of `GET /api/organizations/admin/` (and the administrator's detail / PATCH / POST answers). `usage` counts **seats**: the active people of that kind **plus the invitations
  still waiting** (they hold a seat, so an invitation is never sent that could not be accepted; cancelling or letting one expire frees it). `member_count` / `moderator_count` stay "active people".
  Disable "Invite" when `usage` has reached `limits`.
* **When it is refused.** `403 {"detail": "Your plan allows 1 moderator.", "code": "plan_limit_reached", "kind": "moderators" | "members", "limit": 1}` (`limit` is a number; "Your plan allows 10 members." for
  several) on: inviting (`POST /api/organizations/{id}/invitations/`), bringing an expired invitation back (`.../resend/`), accepting an invitation (checked again), changing a role
  (`PATCH members/{id}/`: promoting a member to moderator, demoting a moderator to member) or reactivating a deactivated member, and the administrator's ways in: creating a moderator account
  (`POST /api/user/admin/users/`), changing a user's role to moderator, and an account invitation for a moderator (`POST /api/user/admin/invitations/`, and accepting it). The Django admin form shows the
  same sentence. Nothing is created when it is refused. Making someone an owner, and anything for a plain user account, takes no seat. Show the sentence; offer to contact the administrator.
* An organization that is already above a limit (it existed before the limits) **keeps everyone** and simply cannot add more until it is under.
* Order of refusals: a duplicate (409) or the last-owner rule (409) answers before the plan limit.

**The platform administrator manages organizations** (`role: admin` only; everyone else 403):

* `GET /api/organizations/admin/` (list) and `GET /api/organizations/admin/{id}/`: each row keeps its old fields and adds `owner` (`{id, username}`: the oldest *active* owner,
  or `null`), `member_count` (every **active** membership, the owner and the moderators included) and `moderator_count` (active moderators). Invited, suspended and removed
  memberships are not counted. `POST /api/organizations/admin/` answers with the same shape.
* `PATCH /api/organizations/admin/{id}/ {name?, plan?, status?, moderator_data_scope?, reason?}`: send any of `name` (1 to 150 characters), `plan` (`free`, `team`, `enterprise`), `status`
  (`active` or `suspended`) and `moderator_data_scope`; at least one of them. `reason` (up to 500 characters) is stored in the audit trail only. The answer is the updated row. Every change
  is written to the organization's audit trail (`organization_updated` with the old and new `name`/`plan`, `organization_suspended`, `organization_reactivated`), which the owner can
  read there. A **personal workspace** cannot be suspended (400 on `status`): suspend the account instead.
* **A suspended organization.** Nothing is deleted. While `status` is `suspended`, every organization endpoint (`/api/organizations/{id}/...`, including the moderator and export
  ones) answers its **members, moderators and owner** `403 {"detail": "This organization is suspended.", "code": "organization_suspended"}`; someone who is not a member still gets
  404. Uploading with `organization_id` of a suspended organization is the same 403 and nothing is stored; an invitation to it cannot be previewed (the usual 404) or accepted
  (403); a moderator whose **only** organization is suspended is refused at sign-in with the same sentence and code. The person's **personal workspace is unaffected**: uploads without
  `organization_id` still go there. `GET /api/organizations/` still lists the organization, with `status: "suspended"` and every `capabilities` value `false`, so show a banner and hide
  the buttons. Reactivating (`status: "active"`) restores everything at once.

## 8. Notifications

`GET /api/notifications/` (list), `GET /api/notifications/unread-count/` (badge),
`POST /api/notifications/{id}/read/`, `POST /api/notifications/mark-all-read/`,
`DELETE /api/notifications/{id}`, and `GET/PATCH /api/notifications/preferences/`
for the settings screen. Each notification has a `title`, `message`, `severity`,
`resource_type`/`resource_id` and an `action_path` you can route to (always a real
frontend route: `/dashboard/jobs`, `/dashboard/images`, `/dashboard/downloads`,
`/dashboard/groups`, or `/moderator/...`; older rows were repaired by a migration). Poll the
unread count (every 30 to 60 seconds is plenty); there is no websocket.

**Severity -> toast/colour.** `severity` is already one of `info`, `success`,
`warning`, `error`, so use it directly as the notification type (there is no
separate `type` field to map). Typical sources: `success` (batch finished, report
or package ready), `warning` (a photo needs review, or a job finished only partly), `error` (a job
failed or a photo was rejected), `info` (everything else). Note the notification
`event_type` is the specific event name, not the colour.

A member whose photograph a moderator decided on receives `photo_review_approved` (severity `success`, title "Your photo was approved") or
`photo_review_rejected` (severity `warning`, title "Your photo was not accepted"), `message` = the moderator's message to them or a standard sentence,
`resource_type` `image`, `resource_id` the photo, `action_path` `/dashboard/images/{image_id}` (open that photograph's page), `metadata.review_id`,
`metadata.image_id`, `metadata.organization_id`. They follow the `photo_accepted` / `photo_rejected` preferences and `email_notifications`.

Moderators and owners of an organization also receive `event_type`
`moderator_batch_failed` when one of the organization's batches fails entirely
(`resource_type` `batch`, `metadata.batch_id` and `metadata.organization_id`,
`action_path` `/moderator/batches`). It is in-app only, follows the
`moderator_review_updates` preference, and never goes to the person who uploaded
the batch (they get their own `batch_completed`).

When a batch has been fully grouped and some of its person folders may be the same person the owner receives `identity_merge_suggested` (severity `info`,
title "Some of your person folders may be the same person", `message` "N of your person folders may be the same person. Check them and merge the ones that are.",
`resource_type` `batch`, `resource_id` the batch, `action_path` `/dashboard/groups`, `metadata.batch_id`, `metadata.folder_count`, `metadata.suggested_merge_count`).
It is in-app only, sent once per batch and only when N is more than 0, and it follows the `person_group_created` preference.

When a batch's automatic enhancements are all finished the owner receives `batch_enhanced` (severity `success`, or `warning` when some failed, title "Photos enhanced",
`message` "3 photos enhanced." with, when there is something to add, "(1 kept as uploaded because enhancing them would not have helped; 1 could not be enhanced)",
`resource_type` `batch`, `resource_id` the batch, `action_path` `/dashboard/jobs`, `metadata.batch_id`, `enhanced`, `kept`, `failed`, `skipped`, `total`). It is in-app only, sent
**once per batch** (never per photograph), and follows the `batch_upload_finished` preference.

**Preferences** (`GET/PATCH /api/notifications/preferences/`) are eight switches, all booleans, a PATCH may send any of them: `photo_accepted`,
`photo_rejected`, `batch_upload_finished`, `person_group_created`, `email_notifications` (off by default), `live_session_alerts` (off by default),
`moderator_review_updates` (on by default; show it on the settings screen of owners and moderators only) and **`auto_enhance`** (on by default; **new**: the account's
setting for automatic enhancement after upload, which a form field on one upload can override; it is the only switch here that is not about notifications, kept in
this endpoint because it is the account's existing preferences endpoint, so show it as "Enhance my photos automatically" on the account's settings screen). With
`moderator_review_updates` off a moderator or owner is no longer told that a review was assigned to them, escalated or overdue, or that a batch failed;
the reviews themselves are unaffected. Everything else in the response is unchanged.

## 8a. Activity (history button and Activity page)

`GET /api/user/activity/?category=&page=&page_size=` (default 20 per page, max
100) answers `{page, page_size, has_next, results: [...]}`. Each entry has the
display fields

| Field | Values |
|---|---|
| `id` | stable string, `activity-<n>` or `auth-<n>` |
| `type` | `upload`, `enhance`, `validate`, `download`, `live`, `group`, `reject`, `login`, `email`, `admin` (`live` is reserved: nothing emits it yet) |
| `message` | a ready-to-show sentence |
| `timestamp` | ISO 8601, same instant as `created_at` |
| `status` | `success`, `warning`, `error`, `info` |

and the original record fields (`source`, `category`, `event`, `object_type`,
`object_id`, `identifier`, `ip_address`, `details`, `created_at`), unchanged.
`category` filters to one of `upload, processing, moderation, download, group,
share, admin, privacy, auth`.
`type` filters to one of the `type` values above, **exactly as each entry reads** (done in the database,
so paging and `has_next` stay right however old the events are; an unknown value is a 400 listing the
choices; it combines with `category`). A type with no entries is an empty page.

How stored events map to `type` / `status`:

| Stored event | type | status |
|---|---|---|
| `upload_received` | upload | info |
| `processing_started` | enhance | info |
| `batch_completed` / `batch_partial` / `batch_failed` / `batch_cancelled` | enhance | success / warning / error / warning |
| `enhancement_completed` / `enhancement_failed` / `enhancement_cancelled` | enhance | success / error / warning |
| `quality_accepted` / `quality_failed` | validate | success / error |
| `quality_rejected` / `quality_needs_review` | reject | warning |
| `package_*`, `identity_export_*`, `report_*` (`ready` / `failed` / `expired`) | download | success / error / warning |
| `share_link_created` / `share_link_revoked` | download | success / info |
| `group_renamed`, `group_merged`, `group_moved`, `group_removed`, `group_profile_set`, `group_deleted`, `group_reviewed` | group | info |
| `consent_granted` / `consent_withdrawn` | validate | success / warning |
| `facial_data_erased` / `data_exported` | group / download | success |
| `registration_pending`, `verification_code_sent` | email | info |
| `registered`, `email_verified`, `password_reset` | email | success |
| `login` / `login_failed` / `login_locked` | login | success / error / warning |
| `logout`, `logout_all`, `session_revoked` | login | info |
| `password_changed`, `profile_updated` | login | success |
| `role_changed`, `suspended`, `reactivated`, `account_deleted`, `admin_created_account`, `account_invited` | admin | info / warning / success / info / info / info |
| `invitation_accepted` | email | success |
| anything else | by category; `*failed` -> error, `*rejected`/`*cancelled`/`*expired` -> warning, `*completed`/`*ready` -> success, otherwise info | |

## 8b. Top-bar search

`GET /api/search/?q=<text>&limit=<n>` (signed in). `q` must be at least 2
characters after trimming, otherwise **400** `{"q": ["Type at least 2 characters
to search."]}`. `limit` is per kind: default 5, maximum 10 (larger values are
capped; `0`, negatives or non-numbers are a 400). Matching is case-insensitive
"contains"; `%` and `_` in `q` are ordinary characters. Only the caller's own
records are searched.

```json
{
  "query": "beach",
  "images": [{"id": 12, "name": "beach-01.jpg", "thumbnail_url": "http://.../api/images/12/thumbnail/",
              "status": "ready", "created_at": "2026-10-16T09:12:00Z"}],
  "groups": [{"id": 3, "display_name": "Beach friends", "member_count": 4}],
  "jobs":   [{"id": 7, "display_name": "Beach weekend", "status": "uploaded", "total_files": 3,
              "created_at": "2026-10-16T09:10:00Z"}]
}
```

`images` match the original file name (`status` is the same `display_status` as
`/api/images/my/`; `thumbnail_url` is `null` for a rejected image), `groups` match
the folder name (active folders only) and `jobs` are upload batches, matched on
the name the person gave the batch **or** the ZIP's file name (`wedding-photos.zip`
is found by "wedding"). A job's `display_name` is always the label "Your uploads"
shows: the chosen name, else the ZIP's file name, else `Upload <id>`, so a row is
never blank. Single and multiple uploads that were never renamed have no name to
search; they appear as `Upload <id>` wherever they are listed but are not found
by that label. Rate limit: 600 requests per hour per user.

## 8c. Search by what is in the photograph (new, off by default)

"beach", "red car", "birthday cake": a model looks at each accepted photograph once and ranks the person's own photographs against the words. **English queries only.** It is
switched off on a server until its owner has approved the model (`docs/model-licenses/semantic-search-model.md`), so the screen must work without it.

```text
GET /api/search/photos/?q=<2-200 characters>&limit=<n>
  -> 200 {enabled: true,  query, language: "en", model: "ViT-B-16-SigLIP", results: [{score: 0.2143, photo: {..the same row as GET /api/images/my/..}}, ...]}   best first
  -> 200 {enabled: false, query, language: "en", model: null, results: []}                                  the server has the feature off: hide the box, show nothing
  -> 503 {detail, code: "semantic_search_unavailable"}                                                       switched on but its model cannot be used: say "not available right now"
  -> 400 q shorter than 2 or longer than 200 characters, or a bad limit
```

- `GET /api/search/` (the top-bar search) gains a `photos` list (`id, name, thumbnail_url, status, created_at, score`) **only** when the feature is on and working; its other keys are
  unchanged. Use it to show "photos that look like this" under the name matches.
- Only the person's own photographs that are accepted (in a personal workspace that includes the ones the checks had a doubt about) and not in the trash. A photograph appears once it has been indexed:
  a few seconds after its checks finish, longer in a busy queue; a photograph from before the feature was switched on appears after the operator runs `python manage.py index_semantic_search`.
- `score` is a cosine similarity, not a percentage: show the order, not the number. A server-side minimum drops weak matches, so a query can honestly return fewer results than `limit`, or none.
- The words are not stored or logged. The vectors describe whole pictures (not faces), are kept apart from face data, and are deleted with the photograph.
- Rate limit: 600 requests per hour per user.

## 9. Privacy screens

- Consent: section 5.
- `GET /api/privacy/facial-data/` shows counts of stored biometric data;
  `DELETE /api/privacy/facial-data/ {"confirm": true}` erases it and verifies
  the erasure.
- `GET /api/privacy/export/` downloads the user's own data as JSON (biometric
  templates are never included).
- **"Consent required" is machine-readable.** When something that needs
  face-recognition consent is refused because the person has not agreed (starting
  identity analysis `POST /api/identities/images/{id}/` and face search
  `POST /api/identities/search/`), the status stays **400** and the sentence is
  unchanged, and the body now carries `"code": "consent_required"`:
  `{"detail": ["Biometric consent is required before face search."], "code":
  "consent_required"}`. Match on `code`, then send the person to the consent
  screen. Before this change the two identity endpoints answered with a bare
  list `["..."]`.

## 10. Admin pages (role `admin` only)

`/api/system/health/`, `/queue/`, `/analytics/`, `/audit/`, `/moderation/`,
`/batches/failed/`, `POST /api/system/batches/{id}/retry/`, `/jobs/failed/`,
`GET /api/user/admin/users/` (a plain array unless `page` is sent), `POST /api/user/admin/users/` (create an account) and
`PATCH /api/user/admin/users/{id}/`,
`GET /api/privacy/audit/`. A non-admin gets 403; use the `role` from login to
hide these routes, but never rely on that for security.

**Which endpoint feeds which admin screen** (the full table, with what each screen still cannot show, is `docs/alignment/admin-dashboard.md`):

| Screen | Endpoint |
|---|---|
| Overview, Statistics, Upload Statistics, Rejections | `GET /api/system/analytics/` (`?date_from=&date_to=&owner=`; the new numbers are under `overview`) |
| System Health, System Monitoring | `GET /api/system/health/` (`services`, `process`) and `GET /api/system/queue/` |
| Audit Logs | `GET /api/system/audit/` (`?page=&page_size=&user=&category=&source=`) |
| Security | `GET /api/system/security/alerts/`, `GET .../alerts/{id}/`, `POST .../alerts/{id}/acknowledge/ {note}`, `GET .../rules/` |
| Image Monitoring | `GET /api/system/images/`, `.../{id}/`, `.../{id}/preview/` |
| Users, Moderators | `GET/POST /api/user/admin/users/`, `PATCH /api/user/admin/users/{id}/` |


`GET /api/system/health/` runs its seven checks at the same time and waits at most 2 seconds for all of them, so with a service down it still answers in about 2 seconds (it used to take 6 to 18) with all seven services listed; a check that has not answered by then is reported with `code: "timeout"` (`down` for the database, cache/broker and storage, `degraded` for the others). The finished answer is kept for 5 seconds, so `checked_at` can be up to 5 seconds old and several screens asking at once cost one round of checks. The shape, and the 503 when a required service is down, are unchanged.

**Analytics `overview`** (additive; every existing key is unchanged). All counts are for the chosen window (default 30 days) unless said otherwise:

```json
{"uploads": {"total": 9, "recent": {"today": 9, "this_week": 9, "this_month": 9},
             "by_type": {"single": 1, "multiple": 5, "zip": 3}, "average_file_size_bytes": 172507,
             "per_day_by_type": [{"date": "2026-10-18", "single": 1, "multiple": 5, "zip": 3}]},
 "rejections": {"total": 0, "rate_percent": 0.0, "per_day": [{"date": "2026-10-18", "count": 0}]},
 "enhancement_success_per_week": [{"week": "2026-W42", "completed": 1, "failed": 0, "success_percent": 100.0, "failed_percent": 0.0}],
 "live_sessions": {"total": 0, "per_day": [{"date": "2026-10-18", "count": 0}]},
 "storage": {"originals_and_outputs_bytes": 2173276, "zip_packages_bytes": 0, "camera_snapshots_bytes": 0,
             "capacity_bytes": 494384795648, "used_percent": 0.0,
             "growth_by_month": [{"month": "2026-10", "added_bytes": 2173276, "total_bytes": 2173276}]},
 "change_vs_previous_period": {"uploads_percent": null, "enhancements_percent": null, "rejections_percent": null, "live_sessions_percent": null}}
```

* `rejections.total` counts a photograph once if it was refused at upload or rejected by its quality decision; `rate_percent` is that share of the
  photographs received (`null` when none). `uploads.recent` always ends now, whatever range was asked for.
* `change_vs_previous_period` compares the window with the equally long window just before it, and is `null` when that one is empty: show "-",
  never "0%".
* `capacity_bytes` is the size of the disk the files are on, and is `null` on object storage (S3 has no fixed capacity): hide the gauge then.
  `camera_snapshots_bytes` is already inside `originals_and_outputs_bytes`.
* The older `success_rate_percent` is the share of *quality decisions* that were accepted, not the share of enhancements that worked; the weekly
  enhancement success is `enhancement_success_per_week`. `totals.images.accepted` counts photographs not refused at upload.

**Background jobs running inside the request.** `GET /api/system/health/` has a top-level `background_jobs_inline` (boolean, always present) and, when it is
`true`, `checks.celery.eager: true` and the celery row of `services` has `detail: "eager"` (instead of `no_workers`): `CELERY_TASK_ALWAYS_EAGER` is on, so
uploads are processed inside the HTTP request and the progress screens have nothing to show. Show it as a notice ("Background jobs run inside the request: start a
Celery worker and set CELERY_TASK_ALWAYS_EAGER=False"), not as an outage. The server also logs a warning when it starts and reports the system check
`operations.W001`; production refuses to start with it on.

**Failed batches and jobs** (Failed Jobs / System Monitoring screens). Every key that was there is unchanged; these are added.

`GET /api/system/batches/failed/` (paged: `results`, `next`, `previous`, `count`) - one row per failed batch:

| Field | Meaning |
|---|---|
| `owner_id`, `owner_username` | who uploaded it (as before) |
| `organization_id`, `organization_name` | the organization it was uploaded to; a batch never filed under one shows the owner's own workspace; both `null` when they have none |
| `failure_stage`, `failure_stage_label` | where it failed: `preprocessing`, `face_validation`, `quality_analysis` or `enhancement`, and "Face validation" etc. for display. The stage most of its failed photographs failed at (a tie goes to the earlier stage). Empty when unknown. **Use this, not `current_stage`**: that one reads `completed` once every photograph has finished, failed or not |
| `failure_reason` | why, as a sentence ("The stored image could not be safely preprocessed."); the most common reason of its failed photographs. Never empty |
| `failed_images` | how many photographs failed |
| `failed_at` | when it failed (ISO 8601): the latest of its photographs, else when the batch finished, else its last change. `updated_at` is unchanged |
| `can_retry` | `true` when "Retry" would work now. Disable the button when `false` and show `retry_blocked_reason` ("This batch has no failed images to retry." / "The maximum of 3 batch retries has been reached.") as its tooltip |
| `retry_blocked_reason`, `max_retries`, `retries_left` | the sentence (empty when `can_retry`), the limit, and how many are left (`retry_count` is as before) |

`POST /api/system/batches/{id}/retry/` answers `202` with the batch's progress, as before (`status`, `stage`, `retry_count`, `counts`, `images` ...), and now also
`processing_status` (the same value as `status`, under the name the list uses, so the row can be updated in place: normally `queued`), `max_retries` and
`retries_left`. A batch that cannot be retried is a `400` `{"detail": ["..."]}` with the same sentence as `retry_blocked_reason`; an unknown batch is `404`.

`GET /api/system/jobs/failed/?kind=&owner=&limit_per_kind=` - `kinds.<kind>[]`, each row keeps `id`, `owner_id`, `owner_username`, `reason` (the stored text, may
be empty), `created_at`, `updated_at` and adds `kind`, `kind_label` ("Enhancement", "Download package" ...), `organization_id`, `organization_name` (its batch's, else
the owner's own workspace), `failure_stage` / `failure_stage_label` (the kind of work it is: `enhancement`, `preprocessing`, `identity_grouping`, `report`, ...),
`failure_reason` (always a sentence: `reason`, or "<kind> failed, but no reason was recorded.") and `failed_at` (when it finished failing, else `updated_at`). A row of
kind `upload_batch` is described exactly like the failed-batch list above, including `can_retry`.

`GET /api/system/moderation/` has no failure rows: it counts reviews. `organizations_needing_attention[]` is `{organization_id, organization_name, pending, in_review,
escalated, stale_open, overdue, oldest_open_at}` and `moderator_workload[]` is `{moderator_id, username, assigned_open, decided_total}`. A batch that failed in an
organization is in the failed-batch list (and the organization's moderators and owners are notified, `moderator_batch_failed`).

**Personal workspaces are not monitored.** A person with no team has a personal workspace, which has no moderator: a photograph the checks are unsure about opens no review there (the owner decides; the photograph stays usable), and personal workspaces are left out of `totals`, `organizations_needing_attention` and `moderator_workload`. `python manage.py close_personal_reviews` (dry run; `--apply` to change) closes the pending reviews that were opened there before this was fixed, with the reason "No moderator in a personal workspace" and no deciding moderator.

**Audit search.** `GET /api/system/audit/?q=` keeps only rows whose actor's name (username, or the identifier typed at a failed
sign-in), event (its stored name, or the sentence the screen shows: "signed in" finds `login`) or category (its name or label, for example
"accounts") contains the text, case-insensitively, up to 100 characters (longer is a 400). It combines with `?category=`, `?source=`, `?user=`
and paging, and `has_next` counts matches only. `filters_applied.q` echoes it (`null` without). A search with no match is an empty page.

**Audit rows** keep every old key and add: `actor` (`{id, username, role}`, `null` when the account is gone), `actor_name` (the username, or the
typed identifier for a failed sign-in by someone unknown), `actor_role`, `type` / `status` (read like the user timeline: `login`, `upload`, ...;
`success` / `warning` / `error` / `info`), `message` (a sentence about what happened, in the third person), `resource` (what it happened to) and
`ip_address` (recorded for sign-in events only, otherwise `null`):

```json
{"source": "auth", "event": "login", "actor": {"id": 2, "username": "adm", "role": "admin"}, "actor_name": "adm", "actor_role": "admin",
 "type": "login", "status": "success", "message": "Signed in.", "resource": "Accounts", "ip_address": "127.0.0.1", "created_at": "2026-10-18T17:59:27Z"}
```

**Security alerts** keep every old key and add `type` (`failed_login`, `authorization_failure`, `rate_limit`), `risk` (`medium` when the threshold is
crossed, `high` at three times the threshold; there is no `low` or `critical`), `message` and `ip_address` (`null` when the alert is about a user and no
address was recorded). Acknowledging needs a `note`:

```json
{"id": 2, "rule": "failed_login_address", "type": "failed_login", "risk": "medium", "status": "open", "ip_address": "127.0.0.1",
 "message": "15 failed sign-ins from address 127.0.0.1 in 15 minutes (alert at 10).", "event_count": 15, "threshold": 10}
```

**System health** adds `services` (one row per check: `key`, `name`, `status` = `operational` / `degraded` / `down`, `latency_ms`, `checked_at`,
`detail`, and for `celery` `queue_size` and `workers`, for `storage` `available_bytes`) and `process` (`started_at`, `uptime_seconds` of the API
process that answered). There are **no** CPU, memory or uptime-percentage figures, because nothing measures them: leave those columns out.
`queue_size` is `null` when the job counts could not be read.

**Account list** (`GET /api/user/admin/users/`) rows add `uploads_count` and `last_login` (`null` if never). `?q=` searches username, email and name,
`?role=user|moderator|admin` and `?status=active|suspended|inactive` filter (anything else is a 400 with the allowed values). Without `page` the
answer is still a plain array; with `?page=&page_size=` it is the usual paginated envelope.

**Creating an account (Add User / Create moderator forms)**

```text
POST /api/user/admin/users/  {username, email, password, role: "user" | "moderator", organization_id?, full_name?}
   -> 201 {id, username, email, full_name, role, is_email_verified, is_suspended, suspension_reason, is_active, date_joined}
   -> 400 {"email": [...]} / {"username": [...]} / {"password": [...]} / {"role": [...]}      (field errors)
   -> 400 {"detail": "Create an organization first, then try again."}                         (moderator, no organization)
   -> 400 {"detail": "There is more than one organization: send organization_id to choose one."}
   -> 403 for anyone who is not an administrator
```

The account is created already verified; no email is sent and the password is never returned, so show the sign-in details to the
administrator once and hand them over. `confirm password` is checked in the browser only. `role: "admin"` is refused. A moderator is
added to an active non-personal organization, so the moderator panel (which takes the first non-personal organization of
`GET /api/organizations/`) finds it; they log in with `role` and `dashboard` = `moderator`.

## 11. Things the UI must handle

| Situation | What to do |
|---|---|
| 401 `token_not_valid` | Refresh once, retry, else login |
| 401 "revoked" | Go to login (refresh will fail too) |
| 400 `{"detail": ["..."]}` | `detail` is a **list** here; show the first message |
| 400 `{"field": ["..."]}` | Show each message next to its field |
| 404 | "Not found" (also means "not yours") |
| 409 | Not ready or already decided; refresh the resource |
| 410 | Expired; offer regenerate (or explain a dead share link) |
| 429 | Show the message; honor `Retry-After` when present |
| `status: "failed"` on a job | Show `failure_reason`; offer retry where the API allows |

## 12. Not built yet

- There is no websocket/server-sent events channel; use polling.
- Live camera features are not implemented.

When in doubt about a field name or enum, open `/api/docs/`. The schema is
generated from the code and validated with zero warnings in CI.
