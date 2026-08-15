import { ref } from "vue";

// Cross-view hand-off for "Discuss in chat" (Skills → Review tab, Dashboards
// → view page). A source surface builds a prefilled prompt, stashes it here
// and router.push("/"); ChatView consumes it ONCE on mount (and clears it on
// EVERY mount so a stale prompt can never fire later): it starts a FRESH
// conversation via newChat(), fills the composer, and when `autoSend` is set
// sends the text as the user's first message in that conversation. Optional
// `context` ({doctype, name}) rides the first send as viewing context —
// api.js#sendMessage forwards it when it carries a doctype. Shape:
//   { text: string, autoSend: true, context?: {doctype, name} }
export const pendingChatPrefill = ref(null);

export function setChatPrefill(payload) {
	pendingChatPrefill.value = payload || null;
}

// Read-and-clear: returns the pending payload (or null) and empties the slot
// so a later plain visit to the chat doesn't re-fill a stale prompt.
export function takeChatPrefill() {
	const v = pendingChatPrefill.value;
	pendingChatPrefill.value = null;
	return v;
}
