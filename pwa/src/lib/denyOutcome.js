// Pure outcome mapping for DecisionSheet's Deny button (P0c: Deny now calls
// dismiss_tool instead of being a client-only dismiss). Extracted so it is
// unit-testable without mounting Vue - the PWA has no component-mount test
// tooling (no vitest/@vue/test-utils; see package.json's test script), unlike
// the desktop SPA.
//
// Given the dismiss_tool RPC envelope, decide the sheet's next state, any
// error text, and the "resolved" kind to emit (or null to stay in review).
// dismiss_tool is a benign no-op on an already-consumed token (returns ok with
// status "already_handled"), so the only failure shape here is a real
// ok:false (a storage/transport error) - never an "InvalidConfirmation"
// branch like Approve's, which is why this stays its own small function
// instead of sharing Approve's.
export function denyOutcome(response) {
	// A close migration can revoke a legacy token while this sheet is open.
	// This is terminal, not a retryable dismiss failure: DecisionSheet keeps a
	// Done-only cancellation explanation open and excludes this token first.
	if (response?.reason_code === "safety_update") {
		return {
			state: "cancelled",
			error:
				response.error?.message ||
				"This confirmation was cancelled during an agent safety update. Submit the action again if it is still needed.",
			resolved: null,
		};
	}
	if (response && response.ok === false) {
		return {
			state: "review",
			error: response.error?.message || response.reason || "Couldn't discard this action.",
			resolved: null,
		};
	}
	return { state: "denied", error: "", resolved: "denied" };
}
