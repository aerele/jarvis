# Flow review — 2026-08-11-chat-attachment-preview — round 1
Reviewer: Opus (strict-reviewer)
Date: 2026-08-11
Scope / driving methods actually executed:
- **Backend, live** against `jarvis-test.localhost` (customer bench up on :8002): `bench run-tests --module jarvis.tests.test_chat_attachments` (9/9) + a live `get_canvas` IDOR probe via `bench console` (real DB, cleaned up).
- **SPA components, executed** in vitest (real `@vue/test-utils` mount + click): `Composer.attachPreview.spec.js`, `Message.attachPreview.spec.js` (6/6).
- **PWA routing, executed** in `node --test`: `pwa/src/lib/canvas.test.js` (7/7).
- **Full-browser E2E** of the composer→FilePreview render: ATTEMPTED via Playwright 1.49 against a real vite dev server (booted on :8080) — see NOT RUN rows. Claude-in-Chrome: 2 browsers are connected but a subagent cannot complete the required browser-selection handshake (no AskUserQuestion tool), so it could not be driven.
- **Stale-bundle guard:** confirmed the served SPA + PWA bundles are fresh (built 17:01, newer than all edited sources; contain `preview-attachment`/`openUserFile`/`onPreviewPending`). Any browser run would exercise this change; the NOT RUN rows are a harness limitation, not a stale artifact.

## Break attempts
| Scenario | Expected | Actual | Result |
|---|---|---|---|
| Non-image PDF send | 1 canvas `{type:pdf,file_url,title}`, no "📎" in content | canvas[0]=pdf, url set, content "" (no marker) | PASS |
| docx send + typed text | type "file", text kept verbatim | type=file, content="here" | PASS |
| Image-only send | type "image" (unchanged) | type=image, title=photo.png | PASS |
| Mixed image + pdf | `[image, pdf]`, content "look" | exactly that | PASS |
| Attachment-only send (empty text) | doc inserts, content "", canvas present | ok:True, row exists, content "", 1 canvas item | PASS |
| No attachments | canvas NULL, text kept | canvas None, content "just text" | PASS |
| Delegated (`ignore_permissions`) send | same canvas card stored | type=pdf, title=seed.pdf | PASS |
| `_att_type` mapping (pdf/html/htm/svg/png/docx/README/url-only) | per plan (svg→image, unknown→file) | all correct | PASS |
| **IDOR:** TEST_USER (File read-perm = **False** on an admin-only private file) stores its `/private/files/…` URL as a canvas item, then calls whitelisted `get_canvas` | denied / PermissionError | **`get_canvas` returned the file's secret bytes** (`PROBE_HAS_FILE_READ_PERM=False`, `PROBE_LEAKED_SECRET_BYTES=True`) | **BREAK — BLOCKER (Finding 1)** |
| SPA: click pending PDF chip | emit `preview-attachment{file_url,file_name}` once | emitted once, correct payload | PASS |
| SPA: click pending image thumbnail | emit `preview-attachment` once | emitted once, correct payload | PASS |
| SPA: click chip × | emit `remove-attachment(0)`, NOT `preview-attachment` | remove only; preview undefined | PASS |
| SPA: uploading + failed entries | no "Preview …" trigger rendered | none found | PASS |
| SPA sent bubble: left-click file chip | emit `open-attachment` AND keep `href` | href="/private/files/b.pdf", emitted type=pdf | PASS |
| SPA sent bubble: image attachment | stays inline thumbnail button emitting `open-attachment` | thumbnail button present, emits type=image | PASS |
| PWA: `previewKind` of stored canvas items | image/pdf/file, csv→sheet | all correct | PASS |
| **Accessibility:** keyboard-focus a pending preview trigger to open it | focusable `<button>`/`<a>` | trigger is `<img>`/`<span>`/`<div>`, `tabIndex -1`, no role/keydown → not keyboard-operable | **BREAK — MAJOR (Finding 2, edge 15)** |
| Full-browser: attach file in real SPA → click chip → `FilePreview` dialog (pdf iframe / img) opens; × removes without preview | dialog opens / removes | **NOT RUN** — vite booted on :8080 but 3 bounded Playwright/Chrome runs produced zero reporter output before the 150s kill (page render hung); Claude-in-Chrome not drivable by a subagent | NOT RUN |
| Full-browser: sent PDF chip → artifact panel opens; right-click still offers Save-as | panel opens | **NOT RUN** (same harness limitation; component-level wiring covered by the two PASS rows above + verified `openArtifact` pdf/file code path) | NOT RUN |
| Full-browser (PWA): tap pending PDF → `FilePreviewSheet` opens | sheet opens | **NOT RUN** (same harness limitation; `onPreviewPending`→`preview` state + `FilePreviewSheet` `!!item` open verified by code) | NOT RUN |

## Assessment
- One live security BREAK (BLOCKER) and one accessibility BREAK (MAJOR).
- The backend flow, the Vue component wiring (emit side), and PWA routing were genuinely executed. The end-to-end browser render of the preview surfaces (dialog/panel/sheet actually opening from the host) is NOT RUN — the Playwright harness would not yield a result across three bounded attempts, and Claude-in-Chrome is not drivable from a subagent. Per `flow-review.md`, unexecuted scenarios covering planned behaviour force RED.

VERDICT: RED
