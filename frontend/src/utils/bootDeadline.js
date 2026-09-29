// Client-side deadline for ChatView's boot requests. frappe-ui's call() is a bare fetch
// with no timeout, and ChatView keeps the composer disabled while `booting`, so one boot
// request that never settles (a half-open connection) would lock the composer for good.
// Same race-a-timer idea as rawOnboardingCall/usePaymentFlow, except a timeout RESOLVES
// (undefined) instead of rejecting: boot carries on with defaults and the caller decides
// how to tell the user. The underlying request is not cancelled; a late reply still runs
// its own side effects.
export const BOOT_TIMEOUT_MS = 15000;

export function withDeadline(promise, ms, onTimeout) {
	let timer;
	const deadline = new Promise((resolve) => {
		timer = setTimeout(() => {
			if (onTimeout) onTimeout();
			resolve(undefined);
		}, ms);
	});
	return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}
