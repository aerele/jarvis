import { ref } from "vue";

// Cross-view hand-off for "Save as macro". The chat (ChatView) no longer hosts
// the macro editor - it lives on /macros. So the chat's "Save as macro" actions
// stash a draft macro here and router.push("/macros"); MacrosView consumes it
// ONCE on mount and opens its editor pre-filled. Shape:
//   { macro_name?: string, description?: string, steps: [{ label?, prompt, skills? }] }
export const pendingMacroPrefill = ref(null);

export function setMacroPrefill(draft) {
	pendingMacroPrefill.value = draft || null;
}

// Read-and-clear: returns the pending draft (or null) and empties the slot so a
// later plain visit to /macros doesn't re-open a stale editor.
export function takeMacroPrefill() {
	const v = pendingMacroPrefill.value;
	pendingMacroPrefill.value = null;
	return v;
}
