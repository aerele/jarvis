// HTML-escape helper (SAR-1 defense-in-depth, security review). frappe-ui's
// ConfirmDialog renders its `message` via v-html, so ANY untrusted value
// interpolated into a confirm message — a requester-authored wiki page title,
// a skill name, a target role, or a warning string built from those — MUST be
// escaped before it reaches that sink, or a stored `<img onerror=…>` runs in
// the reviewer's privileged session on Approve. Pure + importable so it is
// node-tested (the promotionBudget.js precedent) and reused verbatim at every
// call site. Server-side neutralization of the wiki title is the second belt.
// The implementation moved to lib/errors.js (#699), which needs the very same
// escape for the OTHER v-html sink in the app: frappe-ui's Toast binds its
// `message` prop with v-html too, so an error message decoded back to plain
// text has to be re-escaped on the way into it. Re-exported here under the
// original name so every existing call site and this module's own test keep
// working against ONE implementation instead of two that can drift apart.
// Relative path, not the "@/" alias: this test is run by plain `node --test`,
// which does not resolve Vite's aliases.
export { escapeHtml as esc } from "../../lib/errors.js";
