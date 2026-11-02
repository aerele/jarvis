// A macro run in the phone app's signals. Dependency-free so node --test can hold
// it (the app has no component harness, see importFinishedRender.test.js).
//
// Conversations seen running a macro (macro:progress / macro:done name them). A
// run's chat is not in the chat list and each step is a turn of its own: without
// this every step was a "Task finished" for a chat the list never shows. The run
// is told once, when it ends (macro:done). In memory only: a step's turn ends
// seconds after its progress event.
const macroConvs = new Set();
const MACRO_CONVS_MAX = 200;
export function noteMacroConversation(conv) {
	if (!conv || macroConvs.has(conv)) return;
	macroConvs.add(conv);
	if (macroConvs.size > MACRO_CONVS_MAX) macroConvs.delete(macroConvs.values().next().value);
}
export function isMacroConversation(conv) {
	return !!conv && macroConvs.has(conv);
}

/** What a macro:done is worth: { title, body } for a run that finished or failed. */
export function macroDoneEntry(e) {
	const name = e.macro_name ? `: ${e.macro_name}` : "";
	if (e.status === "failed")
		return {
			title: `Macro failed${name}`,
			body: e.error || "Open the run to see what happened.",
		};
	if (e.status === "completed")
		return {
			title: `Macro finished${name}`,
			body: e.error || "Open the chat to see what it found or did.",
		};
	return null;
}
