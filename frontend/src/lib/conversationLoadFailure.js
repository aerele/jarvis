// Only an authoritative refusal for the chat still being loaded can send the
// reader home. HTTP status/message text alone cannot identify this condition.
export function conversationLoadFailure(
	error,
	{ requestedId, currentId, loadId, latestLoadId, viewActive }
) {
	if (!viewActive || requestedId !== currentId || loadId !== latestLoadId) return "ignore";
	return error?.exc_type === "PermissionError" || error?.exc_type === "DoesNotExistError"
		? "unavailable"
		: "throw";
}
