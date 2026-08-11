# Code review — 2026-08-11-chat-attachment-preview — round 1
Reviewer: Opus (strict-reviewer)
Date: 2026-08-11
Scope (my change set only; the other session's Support/imagesAsChips hunks in the shared ChatView.vue / Message.vue were read but are NOT under review):
- jarvis/chat/api.py (`_att_type` / `_att_canvas_item` / `_EXT_CANVAS_TYPE`; store every attachment as canvas; drop the "📎" marker)
- jarvis/tests/test_chat_attachments.py (new, 9 tests)
- frontend/src/components/chat/Composer.vue (clickable thumbnail + chip → `preview-attachment`)
- frontend/src/views/ChatView.vue (composerAttachments.file_url; host `<FilePreview>`; `openUserFile`; `@preview-attachment`)
- frontend/src/components/chat/Message.vue (bubble-loop `<a>` chip: `:title` + `@click.prevent` — hunk @@ -104 only)
- frontend/src/components/chat/{Composer,Message}.attachPreview.spec.js (new, 6 specs)
- pwa/src/components/Composer.vue (clickable pending attachment → `preview`; × `@click.stop`)
- pwa/src/views/ChatView.vue (`@preview="onPreviewPending"` → `FilePreviewSheet`)
- pwa/src/lib/canvas.test.js (extended, 7 tests)

## Findings
| # | Severity | Location | What breaks | Required fix |
|---|----------|----------|-------------|--------------|
| 1 | BLOCKER | `jarvis/chat/api.py::get_canvas` L694-695 (fed by `send_message` L1254 filter + `_att_canvas_item` L70-80) | **Private-file exfiltration (IDOR) — LIVE-CONFIRMED.** `get_canvas` asserts only conversation ownership, then does `frappe.get_doc("File",{file_url})` + `fdoc.get_content()` with **no** File read-permission check. `send_message` stores any client-supplied `file_url` string verbatim as a canvas item (only presence is checked). So a Jarvis user can `send_message(attachments=[{"file_url":"/private/files/someone_elses.pdf"}])` on their own conversation, then call the whitelisted `get_canvas` and read that file's bytes. Live probe on jarvis-test.localhost: TEST_USER (whose `frappe.has_permission("File","read")` on an admin-only private file returned **False**) read that file's secret content back through `get_canvas` (`PROBE_LEAKED_SECRET_BYTES: True`). Contrast `turn_handler._prepare_attachments` L1933, which has exactly the `has_permission("File","read")` gate with an "exfil bypass" comment. Pre-exists via the image canvas path (not introduced by this diff), **but** this change makes attachment→canvas the primary flow for every file type and plan **edge 11 explicitly asserts `get_canvas` safely tolerates the new types** — that safety claim is false. | Add `if not frappe.has_permission("File","read",doc=fdoc.name): frappe.throw(..., frappe.PermissionError)` before `get_content()` in `get_canvas`, mirroring `_prepare_attachments` L1933. Recommended defense-in-depth: validate File readability at store time in `send_message` too, so a non-readable `file_url` never becomes a canvas item. |
| 2 | MAJOR | `frontend/src/components/chat/Composer.vue` L113 (`<img @click>`) & L154 (`<span @click>`); `pwa/src/components/Composer.vue` L74 (`<img @click>`) & L82 (`<div @click>`) | **Edge 15 not met (introduced).** The plan promises "preview triggers are focusable (`<button>`/`<a>`)". The *sent-message* triggers comply (Message.vue uses `<button>`/`<a>`), but the new *composer* pending-preview triggers are non-focusable `<img>`/`<span>`/`<div>` with a bare `@click` — no `tabindex`, `role`, or key handler. A keyboard/screen-reader user can *remove* a pending attachment (the × is a real `<button>`) but cannot *preview* it. No test covers keyboard operability (the specs click by title selector, which works on non-focusable elements). | Make each pending-preview trigger a real `<button>` (preferred), or add `tabindex="0"` + `role="button"` + `@keydown.enter/@keydown.space` on both composers; add a focusability assertion. |
| 3 | MINOR | `jarvis/chat/turn_handler.py` L856 | Stale docstring: "the persisted/visible user message keeps only the '📎 name' marker" — no longer true now the marker is dropped. Misleads readers about how file identity reaches the agent. | Update the comment: file bytes/identity reach the agent via `_prepare_attachments`' own `Attached …` blocks (built from the `attachments` kwarg), and the message stores a canvas card, not a text marker. |
| 4 | MINOR | `frontend/src/components/chat/Composer.vue` L128-130 | The SPA image-thumbnail × uses plain `@click`, not `@click.stop` like the file chip (L173) and the PWA × (L107). Functionally safe here — the `<img>` preview handler and the × `<button>` are siblings inside a non-clickable `<span>`, so the × can't fire preview — but it deviates from plan edge 2's literal "× uses `@click.stop`" and is inconsistent, and would silently start double-firing if the markup were later nested. | Add `@click.stop` for consistency/robustness. |
| 5 | MINOR | Plan edge 14 wording vs `pwa/.../onPreviewPending` + `FilePreviewSheet`/`CanvasFrame` | Plan edge 14 says a PWA pending `.html`/`.svg` preview "degrades to the sheet's fetch/fallback". It actually routes to `CanvasFrame` → `getCanvas("")`, which throws server-side and is caught → the "Couldn't load this chart." state. The promised outcome (never errors) holds, so this is a documentation inaccuracy, not a functional defect. | None required; note for accuracy. |

## Edge-case verification
| Plan edge case | Handling site | Test / evidence | Verified |
|---|---|---|---|
| 1. Non-image/non-pdf (xlsx/csv/txt/docx) → table/text/download, no error | `_att_type`→"file" (api.py L58-67); SPA `openArtifact` "file" branch (ChatView L6859-6874) → `previewFile`; `FilePreview` L225-240; PWA `FilePreviewSheet` L33-41 | backend `test_docx_attachment_is_type_file`; PWA `previewKind` docx→file, csv→sheet | YES |
| 2. Preview-click vs remove-× | file chip × `@click.stop` (SPA L173; PWA L107); preview on chip body only | Composer spec "removing a chip … NOT preview-attachment"; (SPA image × safe-but-not-`.stop` → Finding 4) | YES (w/ Finding 4) |
| 3. Upload in flight → not clickable | `composerAttachments` in-flight pill omits `file_url` (ChatView L9234-9236); Composer guards `a.file_url` | Composer spec "no preview trigger for in-flight/failed" | YES |
| 4. Failed upload → not clickable | `failedUploads` omit `file_url` (ChatView L9240-9247) | same Composer spec | YES |
| 5. Large PDF → lazy iframe, no inlining | `FilePreview` pdf → `<iframe :src=fileUrl>` (L34-39); PWA sheet pdf iframe (L97-102) | code; Playwright asserts iframe `src`=file_url (not a data: URL) | YES |
| 6. Private-File auth on every preview | iframe/img same-origin cookie; html via `fetch` cookie; `previewFile`→`read_file` perm-gated | **PARTIAL — `get_canvas` path is NOT perm-gated → Finding 1 (BLOCKER)** | NO |
| 7. Mixed image + pdf independently clickable | both stored as canvas (api.py L1354); Message renders thumbnail vs chip | backend `test_mixed_image_and_pdf_both_stored`; Message spec (thumbnail vs chip) | YES |
| 8. Attachment-only (empty text) inserts + renders | `send_message` inserts empty content when `atts` present (L1258 guard, L1353); SPA bubble box `v-if="text||html"` (Message L38) with attachment loop as sibling; PWA `v-if="it.msg.content"` + `MessageMedia` renders regardless (pwa ChatView L676-681) | backend `test_attachment_only_message_inserts_with_empty_content`; both render sites read | YES |
| 9. SVG = image (no `_att_type` svg branch) | `.svg` ∈ `_IMAGE_EXTS` (api.py L44) | backend `test_att_type_maps_by_extension` (svg→image) | YES |
| 10. Reopen preview mid-load | `FilePreview` `loadSeq` guard (L200/228/240); artifact panel conv/kind; PWA sheet re-derives on `item.file_url` watch | code (FilePreview.vue) | YES (code) |
| 11. Backend canvas consumers tolerate new types | `generated_media._existing_codex_filenames` reads only `source` (skips user atts); `previewKind`/`ensureCanvas` OK | **`get_canvas` resolves by `file_url` but WITHOUT a File-read gate → Finding 1**; other consumers verified (grep + code) | NO (get_canvas) |
| 12. Delegated / File-Box seed send | same `_att_canvas_item` path under `ignore_permissions` (api.py L1374) | backend `test_delegated_send_stores_canvas_items` | YES |
| 13. Worker byte delivery unaffected | `_prepare_attachments` builds `Attached …` blocks from the `attachments` kwarg (turn_handler L1893-1999), independent of `display_content`/canvas | code | YES |
| 14. PWA pending html/svg preview never errors | `CanvasFrame` `getCanvas("")` wrapped in try/catch → "Couldn't load" (CanvasFrame L26-40) | code (outcome holds; Finding 5 = doc wording) | YES |
| 15. Accessibility: focusable triggers | sent-message triggers are `<button>`/`<a>` ✓; **composer triggers are non-focusable img/span/div → Finding 2 (MAJOR)** | — | NO (composer) |

## Attack pass (playbook) highlights
- **IDOR (cat 4):** get_canvas returns arbitrary private-file bytes on conversation-ownership only → Finding 1, live-confirmed.
- **Input (cat 1):** `_att_type`/`_att_canvas_item` tolerate missing/empty `file_name`/`file_url` (`or ""`, ext fallback); `send_message` filters `atts` to dicts with `file_url` (L1254) so `_att_canvas_item["file_url"]` never KeyErrors. `title` never empty (kind fallback). OK.
- **Hostile file_name:** stored as canvas `title`, rendered as Vue text (`{{ cv.title }}`) and `img alt` → auto-escaped; `_prepare_attachments` sanitizes via `_safe_label_name`. No injection. OK.
- **Failure injection (cat 3):** `previewFile`/`getCanvas`/`fetch` failures are caught on all three surfaces (openArtifact nopreview / FilePreview none+download / FilePreviewSheet error / CanvasFrame "Couldn't load"). OK.
- **State (cat 2):** double-open preview is idempotent; `loadSeq`/`item` watch guard stale async. OK.

VERDICT: RED
