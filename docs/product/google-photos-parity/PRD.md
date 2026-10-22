# Product requirements: a Google-Photos-like experience for a person's own library

Status: draft for implementation, 2026-10-22. Owner: the project owner. Written before the work, to be updated as each feature ships (the "Result" lines at the end of each feature).
Companion: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## 1. Problem

FaceCloud was built around moderation: an organization uploads photographs, a moderator reviews what the checks doubt. A **person using it for their own photographs** meets that
machinery and gets nothing from it:

* A normal portrait was sent to "In review" because of how the face was positioned (fixed in PR #111, not yet merged). Nobody can review a personal workspace, yet the screen says
  "In review". A group photograph with one small face, a landscape, a photograph of food: also "In review" or refused.
* There is no way to group photographs the way people do (albums), mark the ones they love (favourites), take back a deletion (trash), see people as faces to tap, or be reminded of
  moments (memories). Searching works on file names, not on what a photograph shows.

The reference point is Google Photos: **keep everything the person sends, work out who and what is in it, and never make them do work for the system's doubts.**

## 2. Users

| User | Needs |
|---|---|
| **Member (personal workspace)** | Upload without worrying; browse by person, album, favourite; find a photograph by saying what is in it; undo a mistake; nothing "pending" that nobody will ever decide. |
| **Organization member** | Same, and photographs they file under an organization still go to that organization's moderators (unchanged). |
| **Moderator** | Only the photographs that really need a human decision: no new noise, and no loss of the cases they handle today. |
| **Examiner / reviewer of the project** | A working end-to-end story with measured behaviour and honest limits. |

## 3. Goals and non-goals

Goals: the eight features below, each additive (no field, URL or status removed), tested on SQLite and PostgreSQL, documented in `docs/FRONTEND_API_GUIDE.md`, measured on real or clearly-labelled synthetic photographs, and safe with the project's consent, privacy and licence rules.

Non-goals: a mobile app; a frontend (the frontend is the owner's; this work ends at the API and a prompt for it); cloud backup of a phone; shared albums between people; video; editing tools beyond the existing editor; Urdu-language semantic search (see feature 8); changing any model licence decision; enabling a model by default whose licence is not on file.

## 4. Principles that decide the details

1. **Review exists only where someone can review.** A personal workspace has no moderator, so a "needs review" verdict there is information (a tip), not a state.
2. **Reject only what is unusable** (almost black, blown out, corrupt, too small to be a photograph). A face being small, or absent, or many, is not a reason to refuse a photograph.
3. **Everything is reversible** where a person would expect it to be (trash), and never silently destroyed (the orphan-file sweep must not eat a trashed photograph).
4. **Additive.** Existing fields keep their meaning for existing clients; new behaviour comes with new fields, parameters or endpoints. Where a value's *meaning* has to change (feature 1), it changes only for personal workspaces and is documented.
5. **Face data stays under the consent rules** (`docs/features/41-privacy-and-facial-data.md`). Avatars are crops of faces already analysed under consent; semantic search embeddings describe whole photographs and are not biometric templates, and are kept apart from the face embeddings.
6. **Evidence over claims.** Each feature ends with a measurement; what was not measured is said.

## 4a. Decisions taken for the owner (the owner asked for autonomous work; each is reversible and listed for review)

| # | Decision | Why | Where it can be changed |
|---|---|---|---|
| D1 | `face_too_small` on a single face moves from **rejected** to **needs_review** (for everyone). | Google keeps the photograph. A person far away in a landscape is still a good photograph. Rejection stays for unusable image quality. | `validation/services.py` |
| D2 | A group photograph (any number of faces) is **accepted when at least one face is big enough**; the faces too small to group are skipped and said so in a tip. | Same. Today 2-5 faces with one small face, and more than 5 faces, are sent to review. | `validation/services.py` |
| D3 | At most **20 faces per photograph** are grouped (the largest). | Bounds the work of one photograph; a crowd of 60 does not stall the queue. | `IDENTITY_MAX_FACES_PER_IMAGE` |
| D4 | Trash keeps photographs **30 days**. | Google's number. | `TRASH_RETENTION_DAYS` |
| D5 | Albums: up to 500 per person, 10,000 photographs per album. | Generous, but bounded. | settings |
| D6 | Semantic search is **off by default**, English queries only, and ships only with a model whose licence is verified and recorded. | Licence rules; CLIP-family models are English. | `SEMANTIC_SEARCH_ENABLED` |

## 5. Features

Acceptance criteria are what the tests and the measurement check. API shapes are the contract; the guide documents them.

### Feature 1: Review only where someone can review

**Story.** As a member in my personal workspace, I never see "In review", because nobody can review my photographs; I see the photograph accepted, with a tip when the checks had a doubt ("a little dim", "no face found").

**Behaviour.** `moderator_can_decide(image)` is true only for a photograph filed under an organization that is not personal. For anything else a stored verdict `needs_review` is shown as `accepted`.

* `display_status`, the batch progress `status` and its `counts`, the My Images status filter, the batch status (`partial` no longer because of such photographs), the dashboard counts, the notification ("Photo accepted" with the tip instead of "Photo needs review"), and the `accepted_results` ZIP all use the **effective** verdict.
* The quality decision keeps `decision` exactly as the checks decided (it is evidence); it gains `effective_decision`, `review_required` (true only where a moderator can decide) and `tips` (plain sentences, one per reason).
* An organization's photographs behave exactly as before.

**Acceptance.** A personal photograph with verdict `needs_review` shows `accepted` everywhere above and `review_required: false` with at least one tip; the same photograph filed under an organization shows `needs_review` and `review_required: true`; a rejected photograph is unchanged; a batch of only such photographs is `completed`, not `partial`.

### Feature 2: Group photographs and small faces

**Story.** I send a family photograph with six people, some of them small. It is accepted, enhanced, and every face big enough goes to its person's folder, like Google Photos.

**Behaviour (decisions D1-D3).** Face check: no face: `needs_review` (a tip in a personal workspace, a review in an organization); one usable face: accepted (edge-touching: review, as in #111); **one face too small: `needs_review`, not rejected**; two or more faces: **accepted when at least one is usable**, with a tip counting the faces too small to group; none usable: `needs_review` (`group_face_too_small`). **No upper limit on faces for acceptance**; grouping takes the 20 largest and records how many it skipped.

**Acceptance.** Composite photographs with 1, 2, 3, 5, 6, 8, 12 and 20+ faces (some small) are accepted when a usable face exists; each usable face (up to 20) becomes a membership; the enhancement engine runs on them; the verdicts before and after are tabulated in the PR.

### Feature 3: People you can tap (avatars, chips, search)

**Story.** I open search and see the people in my library as little round faces; I tap one and get their photographs; I can combine two people; typing a name finds them.

**API.** `GET /api/identities/people/?q=&limit=&include_unnamed=` returns the chips: `[{id, display_name, is_named, photo_count, avatar_url}]`, most photographs first. `GET /api/identities/groups/{id}/avatar/?size=` serves a square face crop (the best face of the folder). `avatar_url` is added to the folder serializer. `GET /api/search/` gains `people` (same shape) next to the existing `groups`. `GET /api/images/my/` gains `?person=<id>` (repeat it for photographs that contain **all** of those people).

**Acceptance.** Avatars are crops of the folder's best face, correct (not the whole photo), cached with `Cache-Control`; an unnamed folder is `is_named: false`; the list excludes empty folders; a person filter returns exactly the photographs with that face; two person filters return the intersection; everything is owner-scoped.

### Feature 4: Albums

**Story.** I make "Eid 2026", add photographs, set a cover, and open it later.

**API.** `/api/albums/` (list, create), `/api/albums/{id}/` (get, patch, delete), `/api/albums/{id}/images/` (list; `POST` adds, `POST .../remove/` removes), `/api/albums/{id}/cover/`. `GET /api/images/my/?album=<id>`. `/api/search/` gains `albums`. Deleting a photograph removes it from albums; deleting an album never deletes photographs.

**Acceptance.** Owner-scoped; idempotent adds that report skipped ids; limits (D5) enforced with clear 400s; counts and covers correct; a trashed photograph is hidden from the album and returns with its place when restored.

### Feature 5: Favourites

**API.** `PUT/DELETE /api/images/{id}/favorite/`, `POST /api/images/favorites/ {image_ids, favorite}`, `GET /api/images/my/?favorite=true`; `is_favorite` on photograph rows. **Acceptance.** Owner-scoped, idempotent, bulk reports unknown ids, the filter works with the others.

### Feature 6: Trash

**Story.** I delete by mistake and get it back within 30 days; after that it is gone for good.

**API.** `POST /api/images/trash/ {image_ids}` moves to the trash; `GET /api/images/trash/` lists (with `trashed_at`, `days_left`); `POST /api/images/trash/restore/`; `POST /api/images/trash/empty/ {confirm: true}`; `DELETE /api/images/trash/{id}/` deletes one for good. The existing `DELETE /api/images/{id}/` and `bulk-delete` stay **permanent and immediate**, exactly as documented. A daily task purges what is older than `TRASH_RETENTION_DAYS`.

**Behaviour.** A trashed photograph disappears from My Images, search, albums, person folders (their counts and covers), events, memories, the editor, shares, downloads and packages (ready ones are expired, as a permanent delete does); its files stay. Restoring puts it back where it was (folder memberships, album places, favourite). The orphan-file sweep never deletes a trashed photograph's files. Purging uses the existing permanent-delete path, so everything it already removes goes.

**Acceptance.** A table-driven test creates a photograph with every kind of derived row, trashes it, and checks that **each** listing endpoint omits it and each count drops; restoring brings every one back; the purge removes files and rows; the orphan sweep leaves a trashed photograph's files alone.

### Feature 7: Memories

**Story.** The app shows "Moments from 12 Oct", "2 years ago today", "With Ayesha": a few of my best photographs, chosen for me.

**API.** `GET /api/memories/?limit=` returns cards `{id, kind, title, subtitle, start, end, photo_count, cover_thumbnail_url, preview_thumbnail_urls}`; `GET /api/memories/{id}/` returns the card and its photographs. Kinds: `on_this_day`, `event`, `person`. Ids are stable and derived (no new table). Needs the photograph's **capture date**: a new `taken_at` (EXIF `DateTimeOriginal`, else the upload time), filled at upload and by a backfill command.

**Selection.** Best photographs of a moment: not a duplicate of one already chosen, not within a minute of one already chosen, favourites first, then higher quality score, then a face present; deterministic.

**Acceptance.** Cards appear only with at least 3 photographs (event 5); a card's photographs are all the owner's and not trashed; selection is deterministic and free of near-duplicates on a fixture with duplicates; `taken_at` reads EXIF on real files, and falls back safely.

### Feature 8: Search by what is in the photograph

**Story.** I type "beach" or "birthday cake" or "red car" and get the photographs that show it.

**Behaviour.** A joint image-text model (CLIP family) embeds each photograph once (after its verdict) and the query on demand; results are ranked by cosine similarity, owner-scoped, above a similarity floor. **Off by default** (`SEMANTIC_SEARCH_ENABLED`), no model is enabled whose licence is not on file (`docs/model-licenses/`), embeddings are stored apart from face embeddings and are not biometric templates, and the feature says plainly that queries are English. `GET /api/search/photos/?q=&limit=` returns ranked photographs with a `score`; `GET /api/search/` gains `photos` when enabled. A management command indexes existing photographs.

**Acceptance.** With a fake embedder the plumbing (indexing, ranking, owner scoping, trash, disable switch, missing model) is tested in CI; with the real model a small labelled public set gives measured recall, reported with its size; the model's licence, source and SHA-256 are recorded; the default configuration is unchanged.

## 6. Success measures (all measured, none assumed)

| Measure | Target | How |
|---|---|---|
| Portraits and group photographs sent to review or refused by the face check | none for usable photographs | the feature 2 composite set and the #111 portrait |
| "In review" shown in a personal workspace | never | feature 1 tests |
| A trashed photograph visible anywhere | never | the table-driven trash test |
| Semantic search, top-3 contains the right photograph | reported with the sample size | feature 8 evaluation |
| Existing behaviour | unchanged | the full suite on SQLite and PostgreSQL stays green, and CI |

## 7. Risks

| Risk | Mitigation |
|---|---|
| Trash hides a photograph from most code through one manager rule, and a missed consumer could delete data (the orphan sweep). | Explicit patch of the sweep, a test that trashes then runs the sweep, and the audit test over every listing. |
| Changing the meaning of `display_status` for personal photographs could confuse a client that keys on `needs_review`. | Only personal photographs; `decision` unchanged; new `effective_decision` / `review_required`; documented as a change. |
| Face-rule changes let more photographs into enhancement and grouping. | The adaptive engine's own gates are unchanged; before/after measured; D1-D3 reversible in one place each. |
| A CLIP model adds a heavy dependency and a licence question. | Off by default; fake embedder in CI; licence evidence recorded before enabling; the commit can be reverted alone. |
| Scope: eight features in one stretch. | One commit per feature on one branch, tested on both databases at the end of each; the order puts the smallest, safest first. |

## 8. Out of scope now (future work, for the report)

Object/scene tags shown as text, shared albums and partner sharing, place names (no location data is read), "Ask Photos"-style questions, Best Take, Magic Eraser, Urdu queries, video.

## 9. Results (2026-10-22)

All eight features are implemented on the branch `feat/google-photos-parity`, one commit each, with tests on SQLite and on PostgreSQL. Numbers are from this machine (Apple M1) and from the
evidence files named; "not measured" says what a claim does not cover.

| # | Feature | Result | Evidence | Not measured or known limits |
|---|---|---|---|---|
| 1 | Review only where someone can review | Done. In a personal workspace a stored `needs_review` is shown as accepted with tips; `decision` is unchanged; organizations behave as before. | `quality_control/test_effective_decision.py`; mutation: removing the rule fails 12 of its tests | No frontend change was made; a client that keys on `needs_review` for personal photographs must read `effective_decision` |
| 2 | Group photographs and small faces | Done. D1-D3 in force. | `docs/evaluation/2026-10-22-group-photographs.md`: 11 composites through the real endpoints, 7 of 11 reviewed or rejected before, none after | Composites of LFW faces, not real group photographs; the 20-face cap did not bind in that data |
| 3 | People as avatars, chips, person filter | Done. | `identities/test_people_avatars.py`; an avatar costs 6-52 ms from a JPEG and up to 270 ms from a 12 MP PNG | Avatar quality depends on the stored face box; not measured with large libraries |
| 4 | Albums | Done, D5 limits enforced. | `albums/test_albums.py` | - |
| 5 | Favourites | Done. | `uploads/test_favorites.py` | - |
| 7 | Memories and capture dates | Done. Cards: on this day, events, people. | `uploads/test_capture.py`, `memories/test_memories.py`; building a person's memories: 4 ms for 1,000 photographs, 56 ms for 20,000 (SQLite) | Thresholds (3, 5, 10 photographs; 6 h gaps) are product choices, not tuned; EXIF has no time zone, so the camera's local time is read as UTC (right to the day) |
| 6 | Trash with 30-day restore | Done. | `uploads/test_trash.py`, `uploads/test_trash_audit.py`; the audit crawls every GET path as owner, organization admin and public, before and after, and found 15 addresses to patch; it finds the photograph in 30+ places before the trash | The crawl covers GET paths; write actions on a trashed photograph's derived rows go through the same patched lookups but each was not separately tested; a photograph still being processed or waiting for a moderator cannot be trashed; admin lists and analytics deliberately include the trash |
| 8 | Search by what is in the photograph | Done as plumbing plus a measured model, **off by default**. | `docs/evaluation/2026-10-22-semantic-search.md`: 3,669 photographs, first result right for 36 of 37 breed words and for dog and cat; `docs/model-licenses/semantic-search-model.md` | English only; scenes, attributes and a personal library were not measured; the licence record says `Owner approval: pending` and the feature cannot start until the owner changes it; PostgreSQL ranking speed not measured |

Suites on the code commits (2026-10-22, local): **2937 tests, OK on SQLite (45 skipped) and OK on PostgreSQL 18 (40 skipped)**; ruff, bandit (medium and high), `makemigrations --check` and `spectacular --validate --fail-on-warn` clean. CI is the final judge and was not run before the push.

### Decisions that are still the owner's

1. **Approve (or refuse) the search-by-meaning model**: read `docs/model-licenses/semantic-search-model.md`; the feature stays off until its `Owner approval:` line says `approved`.
2. **The order of the open pull requests** (#109 collages removal, #110 automatic enhancement, #111 face-check fix, and this one, whose base is `feat/auto-enhance-after-upload`).
3. **Retention** (`TRASH_RETENTION_DAYS=30`), the **20-face cap**, and the **album limits** are settings; change them if the numbers are wrong for the product.
4. Unchanged from before this pack: whether automatic enhancement should apply `level=high`/`dynamic`, and whether to start a Celery worker in `.env`.
