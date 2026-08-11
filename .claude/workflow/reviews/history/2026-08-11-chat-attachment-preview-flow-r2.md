# Flow review — 2026-08-11-chat-attachment-preview — round 2
Reviewer: Opus (strict-reviewer)
Date: 2026-08-11
Scope: Attachment preview on both chat surfaces (SPA `/jarvis`, PWA `/jarvis-mobile`): composer pending-attachment preview, sent-message attachment preview, and the `get_canvas` private-File security gate. App confirmed running (customer bench :8002 ping → 200).

## Driving method
- **Method C (direct driving — API / bench console / test runner): EXECUTED.** Backend send-path and the security gate were exercised against the live dev site; the SPA/PWA component wiring was exercised via their unit test runners.
- **Method A (Playwright) / Method B (Claude in Chrome): NOT AVAILABLE this session.** The user DECLINED browser automation this session (per invoker); Playwright hung in R1; a subagent cannot drive Claude-in-Chrome. Therefore the full-browser interaction scenarios below are recorded NOT RUN. Per `references/flow-review.md` step 3, unexecuted scenarios that cover planned edge cases force RED. A MANUAL walkthrough on BOTH surfaces must be arranged with the user before any commit/PR.

## Break attempts
| Scenario | Expected | Actual | Result |
|---|---|---|---|
| SEC: restricted user calls `get_canvas` for a canvas item whose `file_url` points at an admin-only private File (crafted/replayed exfil) | denied, no bytes returned | With forced real conversation ownership (so the ownership gate passes and the File-read gate is reached): `PermissionError: no permission to read this file`; no `data_url`/`content` returned. Admin perm-bypass confirmed the same url yields real bytes → gate is the sole barrier. FAILS CLOSED. | PASS |
| SEC: same exfil via the shipped test's exact setup (conv inserted with `owner=USER` under Administrator session) | reach + trigger the File-read gate | Conv actually owned by `Administrator`; blocked earlier by ownership gate (`not your conversation`). The File-read gate is NOT reached — the automated test proves nothing about the gate (see code Finding 1). | BREAK (test coverage, not runtime security) |
| Backend: attachment-only send (empty text + 1 pdf) | doc inserts, `content==""`, one canvas item, no `📎` | `test_attachment_only_message_inserts_with_empty_content` + `_no_marker` pass (10/10 module) | PASS |
| Backend: mixed image+pdf on one message | two canvas items `[image, pdf]`, text kept | `test_mixed_image_and_pdf_both_stored` passes | PASS |
| Backend: docx attachment | canvas `type:"file"`, text verbatim, no marker | `test_docx_attachment_is_type_file` passes | PASS |
| Backend: delegated (`ignore_permissions`) seed send | same canvas items stored | `test_delegated_send_stores_canvas_items` passes | PASS |
| Backend: no attachments | `canvas` NULL, content verbatim | `test_no_attachments_leaves_canvas_null` passes | PASS |
| Component (SPA vitest): click image thumbnail / file chip → `preview-attachment` with correct payload | emitted once, right url/name | Composer spec passes (7/7) | PASS |
| Component (SPA vitest): click remove × | `remove-attachment` only, NOT `preview-attachment` | Composer spec passes | PASS |
| Component (SPA vitest): in-flight / failed entry | no preview trigger rendered | Composer spec passes | PASS |
| Component (SPA vitest): sent file chip left-click | `open-attachment` emitted, `href` preserved | Message spec passes (2/2) | PASS |
| Component (PWA node:test): `previewKind` routes stored user-attachment items | image/pdf/file/sheet as expected | canvas.test.js passes (7/7) | PASS |
| SPA browser: attach image+PDF → thumbnail+chip → click each → FilePreview dialog opens (image renders / PDF iframe) → close | dialog opens & renders | — | NOT RUN (no browser; user declined) |
| SPA browser: `×`-remove a pending attachment → gone, no preview | removed, no dialog | — | NOT RUN |
| SPA browser: sent message → click image (panel) + PDF chip (panel iframe); right-click PDF → Save-as still offered | previews; native menu intact | — | NOT RUN |
| SPA browser: attachment-only message renders attachments with NO empty bubble | no empty bubble | — | NOT RUN |
| SPA browser: `.docx` → download fallback, not an error | nopreview download card | — | NOT RUN |
| PWA browser: tap pending image/PDF → FilePreviewSheet; `×` removes; uploading is inert (disabled) | sheet opens; inert while uploading | — | NOT RUN |
| Attack (browser): back/refresh mid-preview, double-click preview/remove, slow-network upload then tap, deep-link | no dead states / double-fire | — | NOT RUN |

## Notes
- Runtime security posture is SOUND: the `get_canvas` File-read gate fails closed (live-verified by direct driving). The RED here is (a) the invalid regression test for that gate (code Finding 1, MAJOR) and (b) the full-browser interaction scenarios not being executable this session.
- The component-level vitest/node:test runs exercise the emit/prop wiring but are unit-level; they do NOT substitute for an end-to-end browser walkthrough of the dialog/panel/sheet actually opening and rendering a real private File.

VERDICT: RED
