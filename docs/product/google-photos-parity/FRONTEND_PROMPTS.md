# Frontend prompts: what to build for the Google-Photos parity pack

Written for: the person (or coding assistant) building the web frontend against this backend. The backend work for all eight features is finished and tested; **nothing here
needs a backend change**. Every address, field and status below is the real API: the exact contract, with the rules a screen must follow, is in
[`docs/FRONTEND_API_GUIDE.md`](../../FRONTEND_API_GUIDE.md) (section numbers are given). Build in this order; each prompt can be given on its own.

Rules for every screen: send the bearer token on every request, **including image addresses** (thumbnails, avatars and downloads need the Authorization header, so load them with
`fetch` and an object URL, not a bare `<img src>`); treat any `404` on a photograph as "it is gone or in the trash", never as an error screen; use the field a response names, never a
guess from another one; every list is the caller's own, so there is nothing to filter by user.

## 1. Nothing is "in review" for a person on their own

> In My Images, the upload progress and batch screens and the notifications, stop showing "In review" for photographs in the person's own workspace. Read `effective_decision` (not
> `decision`) on a quality result, and `review_required`: when `review_required` is `false` show the photograph as accepted and list its `tips` (plain sentences such as "This photo is a little
> dark; more light gives better results.") under it, as a quiet hint, never as a warning, never with the word "review". Show "Waiting for a moderator" only when `review_required` is `true`
> (a photograph filed under an organization). A `rejected` photograph is shown as rejected with its reason, as before. The My Images status filter `accepted` already includes the
> photographs that used to be "in review", and `needs_review` now means "waiting for a moderator". (Guide section 3.)

## 2. Group photographs and small faces

> No new screen. A photograph with several faces is accepted and appears in each person's folder; a photograph with one small face is accepted too. If `tips` mention faces that were too
> small to place in a folder, show the tip. Never show "too many faces" as an error: at most 20 faces are grouped and the rest are skipped with a tip. (Guide sections 3 and 5.)

## 3. People as avatars

> Build the "People" strip (above My Images and in the search bar's empty state): `GET /api/identities/people/?limit=12` returns `[{id, display_name, is_named, photo_count, avatar_url}]`,
> most photographs first. Draw each as a round avatar (fetch `avatar_url`, 96 px; `?size=96|192|384` on that address for other sizes) with the name under it, or a "+ Add name" prompt when
> `is_named` is `false` (rename with the existing folder rename endpoint). Tapping a person opens My Images filtered by `?person=<id>`; selecting a second person narrows to photographs
> with both (repeat the parameter). In the top-bar search, show `people` from `GET /api/search/?q=` as the same chips. When `avatar_url` is `null` or the image answers 404, show initials.
> (Guide section 5.)

## 4. Albums

> Build Albums: a grid from `GET /api/albums/` (cover from `cover_thumbnail_url`, title, `photo_count`), a detail screen using `GET /api/albums/{id}/` and
> `GET /api/albums/{id}/images/` (the same photograph rows as My Images), "New album" (`POST /api/albums/` with a title and optionally the selected `image_ids`), rename (`PATCH`), delete
> (`DELETE`; say plainly that photographs are not deleted), "Add to album" from the photograph menu and from multi-select (`POST /api/albums/{id}/images/`; use `?image=<id>` on the list
> to show which albums already hold the photograph), "Remove from album" (`POST .../images/remove/`) and "Make cover" (`PUT .../cover/`). Show the `400` sentences when a limit (500
> albums, 10,000 photographs per album) is reached. Add albums to the search results (`albums` key). (Guide section "Albums".)

## 5. Favourites

> Add a heart to every photograph (grid hover, viewer, multi-select bar): `PUT /api/images/{id}/favorite/` and `DELETE` (both idempotent; update the heart from the response),
> `POST /api/images/favorites/ {image_ids, favorite}` for several at once. A "Favourites" view is `GET /api/images/my/?favorite=true`. Every photograph row has `is_favorite`. (Guide section 3.)

## 6. Memories and the timeline

> Build a "Memories" row on the home screen from `GET /api/memories/?limit=10`: cards with `title` ("2 years ago today", "Moments from 12 Oct", "With Ayesha"), `subtitle`, the cover
> (`cover_thumbnail_url`) and up to four `preview_thumbnail_urls`. Tapping opens `GET /api/memories/{id}/` (the best photographs, oldest first, as gallery rows) with a "See all N photos"
> button that opens My Images with the card's `filter` as its query string (`taken_from`, `taken_to` or `person`). Take memory ids from the list, and treat a `404` on one as "this memory has
> changed" (refresh the row). Photographs carry `taken_at` (the camera's date; `null` when the file has none): sort and group a timeline by `taken_at` falling back to `created_at`, and use
> `?taken_from=YYYY-MM-DD&taken_to=YYYY-MM-DD` for date ranges. (Guide section "Memories".)

## 7. Trash

> Replace "Delete" in the photograph menu and the multi-select bar with "Move to trash": `POST /api/images/trash/ {image_ids}`. The answer lists `skipped` photographs with a reason
> (`still_processing`: "Still being processed, try again in a moment"; `in_review`: "Waiting for a moderator") and the rest are moved; show an undo toast that calls
> `POST /api/images/trash/restore/`. Build a Trash screen from `GET /api/images/trash/` (thumbnails from each row's `thumbnail_url`, "N days left" from `days_left`), with Restore
> (`POST .../restore/`), "Delete forever" (`DELETE /api/images/trash/{id}/`), and "Empty trash" behind a confirmation (`POST .../empty/ {confirm: true}`; repeat while `remaining` is above
> 0). Keep "Delete forever" (`POST /api/images/bulk-delete/`) available for people who want it at once. After a move, every count (albums, people, the dashboard) is already lower: just
> refetch. The dashboard summary has `trash_images` and `trash_bytes`; `storage_bytes` still counts the trash. (Guide section "Trash".)

## 8. Search by what is in the photograph (build it last; it is off by default)

> In the search bar, call `GET /api/search/photos/?q=` when the person types two or more characters. If the answer has `enabled: false`, hide this part of the search entirely and show
> nothing in its place. If it answers `503` (`semantic_search_unavailable`), show "Search by what is in a photo is not available right now". Otherwise show `results` (each is
> `{score, photo}`, already best first; show the photograph as a normal grid cell; do not show the score). Say once, near the box, that **only English words work** ("beach", "red car",
> "birthday cake"). The names, people and albums results from `GET /api/search/` stay as they are. A photograph that was uploaded seconds ago may not be searchable yet. (Guide section
> 8c.)

## Checks to run by hand when it is built

1. Upload a dark photograph on a personal account: it ends as accepted with a tip, never "in review"; the same photograph under an organization still shows "waiting for a moderator".
2. Upload a group photograph: it appears in every person's folder it contains.
3. Move a photograph to the trash: it vanishes from My Images, its album, its person's count and the search; restore it: all of them return.
4. Open the trash after 29 days (change `TRASH_RETENTION_DAYS=1` locally and wait): the photograph is gone and its thumbnail answers 404.
5. With `SEMANTIC_SEARCH_ENABLED=False` the search bar looks exactly as it did before.
