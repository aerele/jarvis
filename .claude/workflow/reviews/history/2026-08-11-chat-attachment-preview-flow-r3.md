# Flow review — 2026-08-11-chat-attachment-preview — round 3
Reviewer: Opus (strict-reviewer)
Date: 2026-08-11
Scope: Attachment preview on both chat surfaces (SPA `/jarvis`, PWA `/jarvis-mobile`): composer pending-attachment preview, sent-message attachment preview, and the `get_canvas` private-File security gate. App confirmed running (customer bench :8002).

## Driving method
- **Method C (direct driving — API / bench console / test runner): EXECUTED.** Backend send-path, the security gate, and component emit/prop wiring exercised against the live dev site + unit runners.
- **Method A (Playwright) / Method B (Claude in Chrome): NOT AVAILABLE this session.** Browser automation was declined this session (per invoker); Playwright hung in R1; a subagent cannot drive Claude-in-Chrome. Per `references/flow-review.md` Step 3, unexecuted scenarios that cover planned edge cases force RED. A MANUAL walkthrough on BOTH surfaces must be run against the final state before any commit/PR.

## Break attempts
| Scenario | Expected | Actual | Result |
|---|---|---|---|
| SEC: restricted user calls `get_canvas` for a canvas item whose `file_url` points at an admin-only private File (crafted/replayed exfil) | denied, no bytes | With real conversation ownership so the File-read gate is reached: `PermissionError: no permission to read this file`; no `data_url`/`content`. Admin perm-bypass confirms same url yields bytes → gate is sole barrier. FAILS CLOSED. | PASS |
| SEC (regression-guard validity, R3): does the shipped test fail if the gate is deleted? | test must fail | Live probe: gate NEUTRALIZED (`has_permission`→True) makes `get_canvas` return the File's `data_url` (no raise) → the test's `assertRaisesRegex` would fail "PermissionError not raised". Gate PRESENT → raises the exact asserted message. Test now fails iff gate absent. | PASS (R2 BREAK resolved) |
| Backend: attachment-only send (empty text + 1 pdf) | doc inserts, content=="", one canvas item, no `📎` | `test_attachment_only_message_inserts_with_empty_content` + no-marker pass | PASS |
| Backend: mixed image+pdf on one message | two canvas items [image, pdf], text kept | `test_mixed_image_and_pdf_both_stored` | PASS |
| Backend: docx attachment | canvas type:"file", text verbatim, no marker | `test_docx_attachment_is_type_file` | PASS |
| Backend: delegated (`ignore_permissions`) seed send | same canvas items | `test_delegated_send_stores_canvas_items` | PASS |
| Backend: no attachments | canvas NULL, content verbatim | `test_no_attachments_leaves_canvas_null` | PASS |
| Component (SPA vitest): click image thumbnail / file chip → `preview-attachment` right payload | emitted once, right url/name | Composer spec (7/7 live R3) | PASS |
| Component (SPA vitest): click remove × | `remove-attachment` only, NOT preview | Composer spec (live R3) | PASS |
| Component (SPA vitest): in-flight / failed entry | no preview trigger | Composer spec (live R3) | PASS |
| Component (SPA vitest): sent file chip left-click | `open-attachment` emitted, `href` preserved | Message spec (2/2 live R3) | PASS |
| Component (PWA node:test): `previewKind` routes stored items | image/pdf/file/sheet | canvas.test.js (7/7 live R3) | PASS |
| SPA browser: attach image+PDF → thumbnail+chip → click each → FilePreview opens (image renders / PDF iframe) → close | dialog opens & renders | — | NOT RUN (no browser; declined) |
| SPA browser: `×`-remove a pending attachment → gone, no preview | removed, no dialog | — | NOT RUN |
| SPA browser: sent message → click image (panel) + PDF chip (panel iframe); right-click PDF → Save-as | previews; native menu intact | — | NOT RUN |
| SPA browser: attachment-only message renders attachments with NO empty bubble | no empty bubble | — | NOT RUN |
| SPA browser: `.docx` → download fallback, not an error | nopreview download card | — | NOT RUN |
| PWA browser: tap pending image/PDF → FilePreviewSheet; `×` removes; uploading inert (disabled) | sheet opens; inert while uploading | — | NOT RUN |
| Attack (browser): back/refresh mid-preview, double-click preview/remove, slow-network upload then tap, deep-link | no dead states / double-fire | — | NOT RUN |

## Notes
- Runtime security posture is SOUND and the regression guard for it is now VALID (both live-verified this round).
- Component vitest/node:test runs exercise emit/prop wiring at unit level; they do NOT substitute for an end-to-end browser walkthrough of the dialog/panel/sheet actually opening and rendering a real private File.
- Per invoker: a manual walkthrough on BOTH surfaces is being arranged with the user before any commit/PR; FLOW stays pending-manual until then.

VERDICT: RED
