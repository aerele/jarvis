import { ref } from "vue";

// Cross-view hand-off for a ```jarvis-goto redirect (main chat -> Dashboards
// builder, issue #884). Main chat no longer builds dashboards itself: the
// agent restates the request and points here with a fenced block, ChatView
// stashes it and router.push("/dashboards"); DashboardsPage consumes it ONCE
// on mount (and clears it on EVERY mount so a stale prompt can never fire
// later): when `autoSend` is set it hands the text to the chat pane's
// sendText(), which sends it as the next message on the builder's thread.
//
// jarvis#912: a repeat hand-off for the SAME goto message (the "Continue in
// Dashboards" card is clickable on every later visit to the transcript, with
// no per-click guard) must not build a second conversation on top of the one
// the first hand-off already created. ChatView looks up that mapping (see
// lib/chatGoto.js's fired-stamp helpers) before stashing this payload: when a
// builder conversation is already known for the message, `resume` + `conv`
// are set instead of `autoSend`, and `text` still rides along as the seed for
// a fresh build IF that conversation turns out to have been deleted meanwhile.
// Shape:
//   { text: string, autoSend: true, messageId } - first hand-off, builds fresh
//   { text: string, resume: true, conv: string, messageId } - repeat hand-off,
//     navigates to the recorded conversation (falls back to the first shape's
//     behaviour if `conv` no longer exists)
export const pendingDashboardPrefill = ref(null);

export function setDashboardPrefill(payload) {
	pendingDashboardPrefill.value = payload || null;
}

// Read-and-clear: returns the pending payload (or null) and empties the slot
// so a later plain visit to Dashboards doesn't re-fill a stale prompt.
export function takeDashboardPrefill() {
	const v = pendingDashboardPrefill.value;
	pendingDashboardPrefill.value = null;
	return v;
}
