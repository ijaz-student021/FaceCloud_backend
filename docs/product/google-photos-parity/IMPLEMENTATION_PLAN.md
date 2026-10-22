# Implementation plan: Google-Photos-like features 1-8

Companion of [PRD.md](PRD.md). Branch `feat/google-photos-parity`, on top of PR #110 (which contains #108) with the #111 face-check fix cherry-picked. **One commit per feature**, each tested on SQLite
and PostgreSQL before the next starts, one pull request at the end (a feature can be dropped by reverting its commit). Written 2026-10-22.

## 1. Order and why

| Step | Feature | Why here |
|---|---|---|
| 0 | PRD + this plan | the owner asked for them first |
| 1 | **F1** review only where someone can review | smallest, fixes the "In review" confusion everything else sits on; defines `moderator_can_decide` |
| 2 | **F2** group photographs and small faces | the other half of "photographs should not be held up"; changes the face check, so it comes before features that rely on verdicts |
| 3 | **F5** favourites | trivial; later features (memories, trash, albums) read it |
| 4 | **F3** people avatars, chips, search, person filter | uses the folders as they are after F2 |
| 5 | **F4** albums | new tables, no change to existing flows |
| 6 | **F7** memories | needs `taken_at`, favourites, people, events |
| 7 | **F6** trash | touches the most places; done after albums, favourites, memories exist so it can be audited against them |
| 8 | **F8** semantic search | the heaviest and the only one with a licence question; isolated and off by default; last so a problem there cannot delay the rest |
| 9 | frontend handoff prompts, final suites, the pull request | |

## 2. Cross-cutting rules

* Migrations are additive (new tables, nullable or defaulted columns). No data is rewritten except one explicit backfill command (`taken_at`), run by hand.
* Every new endpoint: owner-scoped queryset, throttle scope, `extend_schema` description and example, entry in `config/api_docs.py` CATALOGUE (the test suite refuses an undocumented operation), a section in `docs/FRONTEND_API_GUIDE.md`.
* New list endpoints reuse `MyImagesPagination` and the photograph row serializer, so a screen's type works everywhere.
* Settings are validated like the existing ones (`ImproperlyConfigured` on nonsense) and listed in `.env.example`.
* Tests: SQLite locally, PostgreSQL locally (`select_for_update`, constraints), and the CI jobs; each feature has tests that **fail without the change** where the change fixes a behaviour (checked by mutation).
* No frontend change (read-only to this work); `FRONTEND_PROMPTS.md` at the end says what to build, from the real API.
* No real AWS, no model enabled by default, no licence decision made for the owner.

## 3. Feature designs

### F1: review only where someone can review

* New `quality_control/effective.py`: `moderator_can_decide(image)` (the organization exists and `not is_personal`; the same rule as `identities.verdict.no_moderator_can_decide`, which stays as it is), `effective_decision(image, decision=None)` (`needs_review` becomes `accepted` when no moderator can decide), `tips_for(reasons)` (a sentence per reason code, in the member's words; reuses `moderation/member_messages.py` where one exists).
* Used by: `uploads/display_status.py` (the status word), `uploads/progress.py` (`_state_for_image`: the terminal status and so the counts and the batch status), `uploads/views.py` (the My Images status filter: `accepted` includes effective-accepted, `needs_review` is org-only), `notifications/events.notify_quality_result` (a personal `needs_review` becomes "Photo accepted" with the tip), `accounts/dashboard.py` (counts), `downloads` (`accepted_results` takes effective-accepted photographs), `activity` (no "needs a manual quality review" event for a personal photograph), `moderation` (no review row is opened in a personal workspace: nobody can decide it).
* Quality serializers gain `effective_decision`, `review_required`, `tips`; `decision` is untouched.
* Tests: matrix of (personal / organization) x (accepted / needs_review / rejected) over every consumer; batch becomes `completed`; the old behaviour for organizations is pinned by tests that already exist (they must still pass untouched).

### F2: group photographs and small faces

* `validation/services.py` decision block: single face too small -> `needs_review` (`face_too_small`), no rejection; 2+ faces -> accepted when at least one `_face_is_usable`, `group_face_too_small` only when none is; remove the `large_group_detected` review (keep the reason code readable for old rows); a `tips` note carries the count of unusable faces (stored in `message`).
* `identities/services.py`: group the `IDENTITY_MAX_FACES_PER_IMAGE` (20) largest faces; record `faces_skipped` in the analysis (existing JSON field if there is one, otherwise a message).
* Labels (`reports/labels.py`, `moderation/member_messages.py`) and `docs/features/15-face-detection-and-validation.md` updated.
* Measurement: composite photographs from a public face dataset (LFW) with 1, 2, 3, 5, 6, 8, 12 and 24 faces, some shrunk; verdicts before (main) and after, memberships created, enhancement outcome. Synthetic and labelled so.
* Tests: each count, small faces, the cap, a one-small-face photograph is not rejected, existing group tests updated for the new rule (the changed expectations are listed in the commit message).

### F5: favourites

* `ImageUpload.is_favorite` (default False), `favorited_at`; index (`owner`, `is_favorite`). `PUT/DELETE /api/images/{id}/favorite/`, `POST /api/images/favorites/`, `?favorite=` filter, `is_favorite` on rows. Bulk reports `updated_ids` / `not_found_ids`.

### F3: people

* `identities/people.py`: `people_for(user, q, limit, include_unnamed)` from active folders with at least one visible photograph; `is_named` is "the display name is not the generated default" (read from how folders are named today).
* Avatar: pick the best face of a folder (highest detector confidence, then sharpness, then size) from its confirmed memberships; crop its box from the **standardized** image with a 35% margin, squared, resized to `size` (96 or 192, default 96), JPEG; `Cache-Control: private, max-age=3600`; 404 when the folder has no face. No new table (computed on the fly; if a measurement shows it is slow it gets a cache later).
* `IdentityGroupSerializer.avatar_url`; `search/` gains `people`; `MyImageListView` gains `?person=` (repeatable, AND).
* Consent: avatars are derived from faces already analysed under consent; nothing new is analysed. A withdrawn consent with erasure removes the folders and so their avatars.
* Tests: crop is the face (pixel check on a synthetic image with a known face box), ordering, `q`, unnamed, owner scope, intersection filter, erased data gives no avatar.

### F4: albums

* New app `albums`: `Album`, `AlbumImage` (unique pair, `added_at`, `position`), endpoints as in the PRD, serializer with `photo_count` and `cover_thumbnail_url`, throttle scope `album_write`, limits from settings. Photograph filter `?album=`. `search/` gains `albums`. Hidden from the album: photographs that are rejected at upload (not in My Images) and, after F6, trashed ones.
* Tests: CRUD, idempotent add, skipped ids, limits, cover rules (cover removed when its photograph leaves), delete cascades, owner scope, ordering, pagination.

### F7: memories

* `ImageUpload.taken_at` (nullable) from EXIF at upload (`uploads/services._save_valid_image`), tolerant of missing or bad EXIF; `manage.py backfill_taken_at` for existing photographs; `captured_at = taken_at or created_at`.
* `memories/` module (no models): builders for `on_this_day`, `event` (from `events/services`), `person` (from folders), `select_highlights()` deterministic; `GET /api/memories/` (cached five minutes per user) and `/{id}/`.
* Tests: EXIF parsing on real small JPEGs with and without EXIF; builders on fixtures; selection excludes duplicates and photographs within a minute of each other; thresholds (3 and 5 photographs); trash and rejected photographs never appear.

### F6: trash

* `ImageUpload.trashed_at` (nullable, indexed). Default manager `ImageUpload.objects` hides trashed rows; `ImageUpload.all_objects` (and `Meta.base_manager_name = "all_objects"`) sees them. **`privacy/cleanup.referenced_media_names` switches to the base manager**, so the orphan sweep keeps trashed files; a test trashes a photograph and runs the sweep.
* `uploads/trash.py`: `move_to_trash(images)` (in one transaction: set `trashed_at`; for folder memberships mark `removed` and remember them in `TrashEntry`; expire ready packages and folder exports as `delete_images` does; refresh folders), `restore(images)` (clear `trashed_at`, put the memberships back, refresh folders, album places stay since they were never removed), `purge(images)` (the existing `delete_images`). `TrashEntry(image, membership_ids, previous_states, trashed_by)`.
* Beat task `uploads.purge_trash` daily. `TRASH_RETENTION_DAYS=30`.
* Audit: every place that lists photographs or derived rows is checked: the 54 `ImageUpload.objects` uses are covered by the manager; joins and derived lists (enhancement results and jobs, quality and validation results, duplicates, identity images and groups, reports, shares, the editor, moderator queues, dashboard counts) get an explicit `image__trashed_at__isnull=True`. The table-driven test lists them all.
* Tests: trash / restore / purge for a photograph with every derived row; every listing omits it and its counts drop; restore brings all back; purge deletes files and rows; the sweep leaves it alone; permanent delete endpoints unchanged; auto-enhancement and grouping hooks ignore a trashed photograph.

### F8: semantic search

* `search/semantic/`: an embedder interface (`embed_images`, `embed_text`, `dimension`, `name`, `checksum`), a `FakeEmbedder` for tests, an `OpenClipEmbedder` loaded lazily from a pinned file (checksum verified like the other checkpoints), `ImageEmbedding(image 1:1, vector bytes (float16), model, model_checksum, created_at)`, indexing task queued after a photograph's verdict (accepted or effective-accepted) on the existing enhancement queue, `manage.py index_semantic_search`, `GET /api/search/photos/`.
* Settings: `SEMANTIC_SEARCH_ENABLED=False`, model path and SHA-256, `SEMANTIC_SEARCH_MIN_SCORE`, `SEMANTIC_SEARCH_MAX_RESULTS`. The production guard counts the model like any other with an unverified licence unless the record says otherwise.
* Model: chosen only after its licence, source and checksum are read and recorded in `docs/model-licenses/semantic-search-model.md`; candidates are an MIT-licensed OpenAI CLIP ViT-B/32 and an Apache-2.0 SigLIP, both to be verified, not assumed. The dependency (`open_clip_torch` or equivalent) is added to `requirements.txt` only if it installs cleanly and passes `pip-audit`; otherwise the feature stays plumbing plus fake embedder and the report says so.
* Evaluation: a small labelled set of public images with known content and queries; top-1 and top-3 reported **with the set's size**; speed per photograph on this machine.
* Tests (CI, no model): ranking by cosine on fixed vectors, owner scope, trash, the floor, the switch, a missing model answering clearly, the indexing hook idempotent, the embeddings absent from the privacy export and from the facial-data counts.

## 4. Risks tracked while working

Trash audit completeness (the table-driven test is the control); face-rule change side effects on grouping counts (before/after table); a heavy dependency for F8 (fallback stated above); time (each feature is a separate commit that can be dropped).

## 5. Definition of done (per feature and overall)

Code, migrations, tests green on SQLite and PostgreSQL, spectacular and `makemigrations --check` clean, ruff and bandit clean, the guide and `FEATURES.md` updated, a measurement or an honest statement of what was not measured, then the PRD's "Result" line filled in at the end. One pull request; the owner merges; the branch is never merged by this work.
