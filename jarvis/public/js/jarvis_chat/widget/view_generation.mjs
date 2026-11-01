// A Desk chat view is more specific than its conversation id. New Chat can
// clear a view before an old request returns, and a later load can select the
// same id again. Use this small, framework-free fence for every async result.
export function advanceViewGeneration(generation) {
	return generation + 1;
}

export function isViewCurrent(
	generation,
	conversation,
	currentGeneration,
	currentConversation
) {
	return generation === currentGeneration && conversation === currentConversation;
}
