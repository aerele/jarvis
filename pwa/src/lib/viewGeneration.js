// A chat view is more specific than its conversation id. A user can leave a
// conversation and later return to the same id while an earlier request is
// still pending. Every async continuation must therefore prove both identity
// and generation before it mutates the view.
export function advanceViewGeneration(generation) {
	return generation + 1;
}

export function isViewCurrent(generation, conversation, currentGeneration, currentConversation) {
	return generation === currentGeneration && conversation === currentConversation;
}
