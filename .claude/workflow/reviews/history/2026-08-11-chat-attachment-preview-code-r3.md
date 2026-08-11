# Code review — 2026-08-11-chat-attachment-preview — round 3
Reviewer: Opus (strict-reviewer)
Date: 2026-08-11
Scope: FOCUSED re-review of the two deltas since R2 — (1) `jarvis/tests/test_chat_attachments.py::TestGetCanvasFileReadGate` (the invalid regression test that was R2 Finding 1) and (2) the plan's T3/trailer wording (R2 Finding 2). Carried forward from R2 (unchanged, re-confirmed by reading): `jarvis/chat/api.py` (`_att_type`/`_att_canvas_item`, canvas storage, `get_canvas` File-read gate api.py:694-702), `jarvis/chat/turn_handler.py` (docstring only — diff is exactly the planned `📎`→canvas-card wording), SPA `Composer.vue`/`Message.vue`/`ChatView.vue` hunks, PWA `Composer.vue`/`ChatView.vue`, `Composer.attachPreview.spec.js`, `Message.attachPreview.spec.js`, `pwa/src/lib/canvas.test.js`. Out of scope (other session's support feature, same working tree): `user_settings_api.py`, `jarvis_user_settings.json`, `support/*`, `UserMenu.vue`, `stores/support.js`.

## Findings
| # | Severity | Location | What breaks | Required fix |
|---|----------|----------|-------------|--------------|
| 1 | MINOR (non-blocking) | `test_chat_attachments.py:183-193` | `TestGetCanvasFileReadGate` creates the restricted `User` row but never deletes it (`addCleanup` cleans the msg/conv/File, not the user). Guarded by `if not frappe.db.exists` so it is reused, not duplicated — matches the codebase's own `_ensure_test_user` fixture convention. No correctness impact. | Optional: delete the user in `_cleanup`, or leave as-is (consistent with existing fixtures). Does not block. |

R2 Finding 1 (MAJOR — invalid regression test) — **RESOLVED, independently verified**. R2 Finding 2 (MINOR — plan wording) — **RESOLVED** (plan lines 97-100 now say the trailer loop renders for "consumers that do NOT supply `#below-body`"; lines 103-107 now correctly state the Support page renders customer messages via the BUBBLE loop and is safe because `SupportThreadPage` binds `@open-attachment` → `window.open(file_url)`).

## Edge-case verification
| Plan edge case | Handling site | Test | Verified |
|---|---|---|---|
| 1 Non-image non-pdf (docx/csv/txt) | `_att_type`→"file" api.py:58-67; SPA `openArtifact` else→`previewFile`; PWA `previewKind`→file/sheet | `test_docx_attachment_is_type_file`; canvas.test.js | YES (backend 10/10 live; PWA 7/7) |
| 2 Preview vs remove click | SPA/PWA `@click.stop` on × | Composer spec "removing a chip … NOT preview-attachment" | YES (SPA vitest 7/7 live R3) |
| 3 Upload in flight → not clickable | composerAttachments omits file_url; PWA `:disabled="!a.file_url"` | Composer spec in-flight | YES (SPA vitest live R3) |
| 4 Failed upload (SPA) → not clickable | failed chip has no file_url | same spec | YES (SPA vitest live R3) |
| 5 Large PDF lazy iframe/img | `openArtifact` pdf→panel url; FilePreview iframe/img | — (render is FLOW) | CODE-verified; render→FLOW |
| 6 Private-File auth preserved | FilePreview same-origin fetch; `preview_file`→`read_file` perm gate; **`get_canvas` File-read gate api.py:701-702** | **`TestGetCanvasFileReadGate` — NOW VALID** | **YES (test 10/10 + reviewer live probe: gate is sole barrier)** |
| 7 Mixed image+PDF independently clickable | per-item v-for; both stored api.py:1362 | `test_mixed_image_and_pdf_both_stored`; Message spec | YES (backend + SPA live) |
| 8 Attachment-only (empty text) | `display_content=message.strip()` api.py:1361; empty-guard requires atts api.py:1266 | `test_attachment_only_message_inserts_with_empty_content` | YES (backend live); no-empty-bubble render→FLOW |
| 9 SVG is an image | `_att_is_image` (.svg∈_IMAGE_EXTS) | `test_att_type_maps_by_extension` svg→image; canvas.test.js | YES (backend + PWA live) |
| 10 Reopen preview mid-load | SPA FilePreview `loadSeq`; PWA re-derives on `item` | — | CODE-verified; interactive→FLOW |
| 11 Canvas consumers tolerate new types + get_canvas security | `previewKind` all kinds; `openArtifact` pdf/image/html/file; `generated_media` reads `source` (absent on user atts); **get_canvas File-read gate** | **`TestGetCanvasFileReadGate` NOW REACHES + TRIGGERS the gate** | **YES — see below** |
| 12 Delegated / File-Box seed send | same `_att_canvas_item` under `ignore_permissions` api.py:1382-1384 | `test_delegated_send_stores_canvas_items` | YES (backend live) |
| 13 Worker byte delivery unaffected | bytes ride `enqueue_kwargs["attachments"]=atts` api.py:1439-1440 | decoupling structural; enqueue mocked | YES (code-verified) |
| 14 PWA pending html/svg preview no-op | `onPreviewPending` messageName=""; CanvasFrame guards load | — | CODE-verified; render→FLOW |
| 15 Accessibility — native buttons | SPA `<button>`; PWA `<button :disabled>`; Message `<a>`/`<button>` | Composer spec "real <button>s" | YES (SPA vitest live); keyboard activation→FLOW |

## Reviewer live executions (R3)
- `bench --site jarvis-test.localhost run-tests --app jarvis --module jarvis.tests.test_chat_attachments` → **10/10 OK**.
- `npx vitest run Composer.attachPreview.spec.js Message.attachPreview.spec.js` (from `frontend/`) → **7/7 passed** (live R3).
- `node --test pwa/src/lib/canvas.test.js` → **7/7 passed** (live R3).
- Drift check: `find … -newer …-code-r2.md` shows only `api.py` (mtime re-touched this session; `git diff` = exactly the planned T1 hunks, no smuggled change) and the test file. No in-scope frontend file changed since R2.
- **Independent gate-is-sole-barrier probe** (bench console, restricted non-admin user owning the conversation, canvas item pointing at an admin-only private File):
  - Precondition live: `owner == restricted user` (ownership gate passes → File-read gate reached); `has_permission("File","read") == False`.
  - Gate PRESENT (shipped code): `get_canvas` raises `PermissionError: no permission to read this file` — the exact message the test's `assertRaisesRegex` requires.
  - Gate NEUTRALIZED (`has_permission` patched True, simulating gate deletion / admin bypass): `get_canvas` RETURNS `{... "data_url": "data:application/octet-stream;base64,…"}` — the private File's bytes leak.
  - Therefore the test **fails iff the gate is absent** → it is now a genuine regression guard. R2 Finding 1 is fixed.
- `ruff check` + `ruff format --check` (jarvis app pyproject) on `api.py` + `test_chat_attachments.py` + `turn_handler.py` → **clean / already formatted**.

## Commit-hygiene caution (not a finding, carried from R2)
Working tree interleaves this feature with the other session's uncommitted support feature inside shared files (`ChatView.vue`, `Message.vue`). Plan DoD requires staging ONLY the attachment-preview hunks (never `git add -A`); `git add -p` will need hunk-splitting where `<FilePreview>` sits adjacent to `<SupportCopyPromptDialog/>`.

VERDICT: GREEN
