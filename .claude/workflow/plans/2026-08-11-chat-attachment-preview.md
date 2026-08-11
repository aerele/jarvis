# Plan: View/preview user-attached files (PDF + image) in chat — SPA + PWA
STATUS: APPROVED
Date: 2026-08-11
Owner: Fable (team leader)

## Goal
In both the customer chat SPA (`/jarvis`) and the mobile PWA (`/jarvis-mobile`), a
user who attaches a file can actually SEE it: an image or PDF (and other file
types) is previewable both (a) in the composer, before/while sending, and (b) on
the message once sent. Today the composer shows a static thumbnail or a dead
`📎 name` chip, and a sent PDF is unclickable text. After this change, clicking a
pending attachment opens a preview, and a sent PDF is a clickable card that
previews in-app — on both surfaces.

## Context
Verified by reading the code, not assumed:

- **Reusable preview components already exist on both surfaces.**
  - SPA: `frontend/src/components/FilePreview.vue` — a `Dialog` routing by kind:
    `pdf` → same-origin `<iframe>`, `image` → `<img>`, `html`/`svg` → sandboxed
    `srcdoc`, `xlsx`/`csv` → sheet table via `api.previewFile`, `txt`/code → text,
    download-only fallback. Session-cookie fetch (private-File auth preserved);
    already used by `pages/files/FilesList.vue`.
  - PWA: `pwa/src/components/FilePreviewSheet.vue` — the same routing as a bottom
    sheet, driven by a canvas-like `item` + `previewKind(item)` (`lib/canvas.js`).
  - **We reuse both; we do not build a viewer.**
- **Composer (both) already renders attachments but nothing is clickable.**
  - SPA `components/chat/Composer.vue`: image thumbnail (`a.preview_url`),
    "Uploading…" pill, failure chip, `📎 name` chip — none clickable.
  - PWA `components/Composer.vue`: `<img v-if="a.preview">` thumbnail or a name
    span + busy spinner + remove `×` — none clickable.
- **Sent messages.**
  - SPA `components/chat/Message.vue` (`:attachments="m.canvas"`,
    `@open-attachment="openArtifact(m,$event)"`): image canvas → clickable
    thumbnail → artifact panel; non-image canvas → plain `<a href target=_blank>`.
  - PWA `components/MessageMedia.vue` (`:items="it.msg.canvas"`, `@open` →
    `FilePreviewSheet`): routes EVERY item through `previewKind(item)` — image →
    thumbnail button, html/svg → inline `CanvasFrame`, everything else → a
    clickable artifact chip that emits `@open`. `previewKind` derives the kind
    from `item.type` OR the `file_url` extension, so a `pdf`/`file` canvas item is
    already rendered and previewable with no PWA message-side change.
- **The one backend gap** (`jarvis/chat/api.py::send_message`): image attachments
  are stored as `canvas` items (`type:"image"`, `file_url`, `title`) — that is why
  they preview. **Non-image attachments are NOT stored as canvas at all**; they
  only append a `📎 name` marker to the visible message text, leaving no
  `file_url` to click. Fixing this one seam feeds BOTH surfaces.
- **Worker/agent byte path is decoupled from display** (verified): the turn is
  enqueued with `enqueue_kwargs["attachments"] = atts` (the raw full list) and
  `turn_handler._prepare_attachments(user_message, attachments, vision_ok)` inlines
  from that kwarg — independent of `display_content` AND `canvas`. Images already
  prove this (no text marker, yet the agent receives them).

Why this approach: the only missing backend piece is representing non-image
attachments as `canvas` items; the only missing frontend piece is making the
already-rendered composer affordances open a preview. Everything else already
exists. Rejected: bundling a PDF.js viewer (violates self-contained/CSP, duplicates
the two existing preview components); inlining PDF bytes into the render (large —
previews load lazily via `file_url` in an iframe/img).

## Architecture / approach

Four coordinated changes; the backend one is shared by both surfaces.

**1. Backend — represent non-image attachments as canvas file items** (shared).
In `jarvis/chat/api.py::send_message`, add a sibling to `_att_is_image`:

```
_EXT_TYPE = {"pdf": "pdf", "html": "html", "htm": "html"}  # svg is already an image via _IMAGE_EXTS
def _att_type(att) -> str:
    if _att_is_image(att): return "image"
    ext = (att.get("file_name") or att.get("file_url") or "").rsplit(".", 1)[-1].lower()
    return _EXT_TYPE.get(ext, "file")
```

Store ALL attachments (image + non-image) as canvas items `{name: hash,
type: _att_type(a), file_url, title: file_name}`. **Drop the `📎 name` text
marker** — the card replaces it (safe: worker reads the `atts` kwarg, not
`display_content`; edge 13). `display_content` becomes the user's plain text,
which may now be empty for an attachment-only message (edge 8). These `type`
values feed both the SPA `openArtifact` switch and the PWA `previewKind` (both
accept `pdf`/`image`/`html`/`file`), and `previewKind` re-derives from `file_url`
regardless, so the field is belt-and-suspenders.

**2. SPA composer preview.**
- `views/ChatView.vue::composerAttachments` gains `file_url: f.file_url` on every
  real-file entry (uploading/failed entries stay without one).
- `components/chat/Composer.vue`: image thumbnail + `📎` chip body become clickable,
  emitting a new `preview-attachment` event `{file_url, file_name}` — ONLY when
  `a.file_url` is present. The remove `×` uses `@click.stop`.
- `views/ChatView.vue` hosts one `<FilePreview v-model :fileUrl :fileName>` and an
  `openUserFile({file_url, file_name})` handler bound via `@preview-attachment`.

**3. SPA sent-message preview.**
- `components/chat/Message.vue`, **bubble loop only (~line 102)**: keep the file
  chip an `<a :href="cv.file_url">` (right-click-save / middle-click-new-tab keep
  working) and add `@click.prevent="emit('open-attachment', cv)"` so a left-click
  previews in the already-wired artifact panel. **Do NOT touch the trailer loop
  (~line 220)** — that loop renders only for consumers that do NOT supply
  `#below-body` (its `v-if="!hasBelowBody"`); chat supplies one, so editing the
  trailer buys nothing and risks the other session's hunks there. No ChatView
  routing change: `@open-attachment` already calls `openArtifact`, which handles
  pdf/image/file.
- **Support cross-check:** the Support page (the other session's WIP) also renders
  customer messages with `variant="bubble"`, so they hit THIS same bubble-loop file
  chip. That is safe: SupportThreadPage binds `@open-attachment` → `window.open(file_url)`,
  which is functionally identical to the `href` navigation it replaces — no dead
  button, no behaviour change. (The `href` is retained regardless.)

**4. PWA composer preview.** (Sent messages need NO change — see Context.)
- `components/Composer.vue`: the pending attachment (thumbnail/name) becomes
  clickable, emitting a new `preview` event with the row — ONLY when
  `a.file_url` is present; the remove `×` uses `@click.stop`.
- `views/ChatView.vue`: bind `@preview="onPreviewPending"` on `<Composer>` and set
  `preview = { item: { file_url, title: name, name: file_url }, messageName: "" }`,
  reusing the existing `FilePreviewSheet`. `previewKind` derives image/pdf from the
  synthesized `file_url`; `FilePreviewSheet` uses `item.file_url` directly for
  image/pdf (messageName is only consulted for the html/svg `CanvasFrame` path).

Result: pending attachments preview (SPA `FilePreview` dialog / PWA
`FilePreviewSheet`); sent attachments preview (SPA artifact panel / PWA
`FilePreviewSheet`). Each surface uses its own existing, idiomatic component.

## Task breakdown
| ID | Task | Weight | Assignee | Depends on | Acceptance criteria |
|----|------|--------|----------|------------|---------------------|
| T1 | Backend: `_att_type`; store non-image atts as canvas file items; drop `📎`; allow empty `display_content` when atts present | Heavy | Lead (Fable) | — | Non-image attachment persists a canvas item `{type,file_url,title}`; images unchanged; no `📎`; attachment-only send stores empty content and inserts; worker still enqueued with full `atts` |
| T2 | SPA composer: clickable thumbnail+chip → `preview-attachment` (guard `file_url`, `×` `.stop`); `composerAttachments` carries `file_url`; ChatView hosts `<FilePreview>` + `openUserFile` | Medium | dev-sonnet → Lead* | T1 | Clicking a pending image/file opens `FilePreview` with the right url/name; `×` removes without previewing; uploading/failed not clickable |
| T3 | SPA Message.vue bubble-loop chip: add `@click.prevent="emit('open-attachment', cv)"` (keep `href`); trailer loop untouched | Light | Lead (Fable) | T1 | Left-click opens the artifact panel; right/middle-click still open/download; trailer loop unchanged vs the other session's version |
| T4 | PWA composer: clickable pending attachment → `preview` (guard `file_url`, `×` `.stop`); ChatView `onPreviewPending` → `FilePreviewSheet` | Medium | dev-sonnet → Lead* | T1 | Tapping a pending image/PDF opens `FilePreviewSheet`; `×` removes without previewing; uploading not clickable. (No MessageMedia change.) |
| T5 | Tests: backend module test + SPA vitest + PWA test | Medium | dev-sonnet → Lead* | T1–T4 | Every edge case has a test; backend run via single module only |

\* dev-sonnet / dev-haiku subagents are not spawnable in this environment; Lead
implements delegated tasks in the main session, honoring the weight as the review bar.

## Edge cases and failure modes (reviewer will verify each one)
1. **Non-image, non-pdf file** (xlsx/csv/txt/docx): SPA `FilePreview` /
   `openArtifact('file')` and PWA `FilePreviewSheet` all fall to table/text/
   download without erroring.
2. **Preview-click vs remove-click** (both composers): the `×` uses `@click.stop`;
   preview fires only from the thumbnail/chip body.
3. **Upload in flight**: no `file_url` → not clickable (SPA pill; PWA busy row).
4. **Failed upload** (SPA): the failure chip has no `file_url` → not clickable.
5. **Large PDF**: preview loads lazily via `<iframe src=file_url>` / `<img>`; bytes
   are never inlined into the render (CSP + size constraint).
6. **Private-File auth**: every preview loads via the session-authed `file_url`
   (same-origin iframe/img, or cookie `fetch` for html/table); no anonymous path.
7. **Mixed image + PDF on one message**: both render and are independently
   clickable on both surfaces.
8. **Attachment-only message (empty text)**: after dropping `📎`, `display_content`
   can be `""`. `send_message` must insert with empty content when `atts` is
   non-empty; both bubbles render attachments with no empty text block. (SPA
   ChatView already guards a blank bubble; PWA renders `v-if="it.msg.content"`.)
9. **SVG attachment**: already an image via `_att_is_image` (`.svg` ∈ `_IMAGE_EXTS`)
   → `<img>`/panel image (script-inert). No `svg` branch needed in `_att_type`;
   documented so a reviewer does not flag it.
10. **Reopen preview on a different file mid-load**: SPA `FilePreview` `loadSeq`
    guard + artifact-panel `conv`/`kind` guard; PWA sheet re-derives on `item`.
11. **Backend canvas consumers tolerate the new types** (verified):
    `generated_media._existing_codex_filenames` reads only `source` (user atts have
    none → skipped); SPA `ensureCanvas` only runs for html/svg; PWA `previewKind`
    handles every kind. **Security (get_canvas):** this change makes
    attachment→canvas the primary flow for ALL file types, so `get_canvas` now
    enforces `has_permission("File","read")` before `get_content()` — it previously
    asserted only conversation ownership, so a crafted/replayed `file_url` on a
    canvas item could exfil a private File (returned as srcdoc content or a base64
    data_url). Mirrors the gate `read_file`/`_prepare_attachments` already enforce;
    covered by `TestGetCanvasFileReadGate` (restricted non-admin user).
12. **Delegated / File-Box seed send**: `send_message` with `attachments` under
    `ignore_permissions` runs the same canvas path — file items stored, no `📎`.
13. **Worker byte delivery unaffected**: the agent receives bytes/vision from the
    `attachments` enqueue kwarg (`atts`), decoupled from `display_content`/`canvas`.
14. **PWA pending html/svg preview**: a pre-send `.html`/`.svg` upload previewed
    with `messageName=""` routes through `CanvasFrame`, which needs a real message
    to fetch canvas content; with no message it renders nothing but does not error
    (CanvasFrame guards its load). Acceptable — image/PDF (the ask) render straight
    from `file_url`. Documented, not a blocker.
15. **Accessibility**: composer preview triggers are native `<button>`s
    (keyboard-focusable, Enter/Space activate) on BOTH the SPA and PWA composers —
    the PWA button is `:disabled` until upload completes so an in-flight attachment
    is inert; sent-message triggers are `<button>`/`<a>`; SPA `FilePreview` is a
    focus-trapped `Dialog`; PWA sheet is Esc/backdrop-closable. Covered by the
    Composer spec's "real <button>s" test.

## Test plan
- **Backend unit** (`bench --site jarvis-test.localhost run-tests --app jarvis
  --module jarvis.tests.test_chat_attachments` — single module ONLY; the full
  suite is destructive on this live site):
  - non-image attachment → one canvas item, `type` from extension
    (`pdf`→`pdf`, `docx`→`file`); `display_content` has no `📎`. (edge 1,8,12)
  - image path unchanged (`type:"image"`). (regression guard)
  - attachment-only send → empty `display_content`, doc inserts, canvas present. (edge 8)
  - `_att_type` mapping incl. svg→image and unknown→file. (edge 9)
  - delegated (`ignore_permissions`) send stores the same canvas items. (edge 12)
- **SPA unit** (vitest): `composerAttachments` carries `file_url` only for real
  files (edge 3,4); `Composer` emits `preview-attachment` on click, not on `×`/
  uploading/failed (edge 2,3,4); `Message.vue` bubble chip emits `open-attachment`
  and keeps `href` (edge 7); `openUserFile` sets+opens the dialog.
- **PWA unit** (existing `node:test` convention for `lib/`, e.g. extend
  `lib/canvas.test.js`): assert `previewKind` maps a `{type:"pdf"}` and a
  `{file_url:".../x.pdf"}` item to `pdf` and a `{file_url:".../x.docx"}` to `file`
  (proves MessageMedia routes T1's items to the chip + `@open`). `Composer`/
  `ChatView` wiring verified in flow review (no vue test runner wired in the PWA).
- **Flow review scenarios** (executed against the running app; the Chrome
  extension was declined this session — Playwright or a manual walkthrough on BOTH
  `/jarvis` and `/jarvis-mobile`):
  1. Attach image + PDF → thumbnail + chip appear → click each → preview opens
     (image renders; PDF iframe renders) → close.
  2. `×`-remove a pending attachment → gone, no preview opens.
  3. Send → on the sent message, click the image (panel/sheet) and the PDF chip
     (panel/sheet iframe) → both preview; SPA right-click PDF still offers Save-as.
  4. Attachment-only message (no text) sends and displays attachments, no empty
     bubble. (edge 8)
  5. Attach a `.docx` → preview shows the download fallback, not an error. (edge 1)

## Files to create / modify
- `jarvis/chat/api.py` — `_att_type`; canvas file items; drop `📎`; empty content (T1);
  `get_canvas` File-read gate (security fix from code review R1). (T1)
- `jarvis/chat/turn_handler.py` — docstring corrected (no more `📎` marker). (T1)
- `frontend/src/components/chat/Composer.vue` — clickable thumbnail/chip → `preview-attachment`. (T2)
- `frontend/src/views/ChatView.vue` — `composerAttachments.file_url`; import/host `<FilePreview>`; `openUserFile`; `@preview-attachment`. (T2)
- `frontend/src/components/chat/Message.vue` — bubble chip `@click.prevent` (one line). (T3)
- `pwa/src/components/Composer.vue` — clickable pending attachment → `preview`. (T4)
- `pwa/src/views/ChatView.vue` — `@preview` → `onPreviewPending` → `FilePreviewSheet`. (T4)
- NEW `jarvis/tests/test_chat_attachments.py`; SPA vitest spec(s); extend `pwa/src/lib/canvas.test.js`. (T5)
- No new components (reuse `FilePreview.vue` / `FilePreviewSheet.vue`); no MessageMedia change.

## Open questions — RESOLVED at approval
1. **Shared-checkout collision** — user: proceed now; bind both sessions' work and
   push ONE combined PR AFTER this feature completes. Action: build on the current
   tree, keep my `Message.vue` edit at ~102 (separate hunk; their hunks are at
   93/220/332; I avoid 220) and ChatView edits additive; **never `git add -A` /
   `git commit -a`** — stage only my files; re-diff right before staging. PWA/api.py
   are not in the other session's file set → collision-free.
2. **Branch** — user: continue on `_compat_work`.
3. **Surfaces** — user: BOTH PWA and SPA (every chat UI/UX change reflects both).
   Folded in above; PWA sent-messages need no change (MessageMedia/previewKind/
   FilePreviewSheet already handle canvas file items from T1).

## Definition of done
- All tasks meet acceptance criteria
- Code review VERDICT: GREEN  ·  Flow review VERDICT: GREEN (both surfaces)
- Committed only after both greens (staging ONLY the attachment-preview files —
  never `git add -A` in this shared checkout); the combined PR (this feature +
  the other session's work) raised only after flow review passed on the final state
