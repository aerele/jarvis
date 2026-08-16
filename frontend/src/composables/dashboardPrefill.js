import { ref } from "vue";

// Cross-view hand-off for a ```jarvis-goto redirect (main chat -> Dashboards
// builder, issue #884). Main chat no longer builds dashboards itself: the
// agent restates the request and points here with a fenced block, ChatView
// stashes it and router.push("/dashboards"); DashboardsPage consumes it ONCE
// on mount (and clears it on EVERY mount so a stale prompt can never fire
// later): when `autoSend` is set it hands the text to the chat pane's
// sendText(), which sends it as the next message on the builder's thread.
// Shape:
//   { text: string, autoSend: true }
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
