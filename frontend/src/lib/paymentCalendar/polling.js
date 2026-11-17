// Shared by the snapshot, ledger and balances composables.
const POLL_DELAYS = [1000, 2000, 3000, 5000];
const HIDDEN_POLL_MS = 15000;

// Backoff for a job that is still queued or running; slower while the tab is hidden.
export function nextPollDelay(attempt) {
	if (document.hidden) return HIDDEN_POLL_MS;
	return POLL_DELAYS[Math.min(attempt, POLL_DELAYS.length - 1)];
}

// The part of an API failure that is safe to keep and show.
export function safeError(result) {
	return {
		code: result?.code || "internal_error",
		state: result?.state || "failed",
		retryable: result?.retryable !== false,
	};
}
