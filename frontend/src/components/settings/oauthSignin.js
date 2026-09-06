// Drives one connector's sign-in tab end to end: open it, resolve which row
// to sign in against, start the flow at the vendor, then poll until the
// backend sees a connection.
//
// The tab is opened SYNCHRONOUSLY, before any await - a window.open() issued
// after an awaited call has left the click's user-gesture window and gets
// popup-blocked (same trap LlmPoolEditor's sign-in already works around).
// Its opener is nulled so the vendor's page (which we don't control) can't
// reach back into this tab via window.opener.
//
// connected_at is compared to this call's own started_at, not just checked
// for truthiness: a user re-authorising an already-connected row is
// "connected" on the very first poll, from their PREVIOUS sign-in - only a
// connected_at strictly newer than started_at proves THIS flow finished.
//
// signIn(target, opts) - `target` is either a connector name string, or an
// async factory `() => Promise<name>` that creates the row. The factory runs
// AFTER the tab is opened and BEFORE connect_oauth, so a "Sign in with X"
// press can create its row in the very click that opens the tab, without
// anything existing before the user actually presses it (AddConnectorDialog
// leans on this to avoid registering a client at the vendor just from
// opening the dialog). If the factory throws, the tab is closed and the
// returned promise resolves {status:"error", message}.
//
// The return value is a Promise with one extra method attached to it,
// `.cancel()` (not a separate {promise, cancel} shape) - calling it closes
// the tab this call opened (if any) and stops the poll, resolving that SAME
// promise with {status:"cancelled"}. Safe to call after the promise has
// already settled (a no-op then).
import * as api from "@/api";
import { escapeHtml } from "@/lib/errors";

const POLL_INTERVAL_MS = 2000;
// The server keeps a started sign-in's state for 10 minutes - matched here so
// a slow vendor round trip doesn't time out on the client while the backend
// would still have honoured it.
const POLL_TIMEOUT_MS = 10 * 60 * 1000;

// Navigation goes through this one indirection so a test can replace `go`
// with a spy - jsdom refuses to let a script assign `window.location.href`
// outright.
export const nav = {
	go(url) {
		window.location.href = url;
	},
};

export function signIn(target, { label, agentName } = {}) {
	const w = openInterimTab(label, agentName);
	let settled = false;
	let stopPolling = null;
	let resolveFn;

	const promise = new Promise((resolve) => {
		resolveFn = (result) => {
			if (settled) return;
			settled = true;
			resolve(result);
		};
	});

	promise.cancel = () => {
		if (settled) return;
		if (stopPolling) stopPolling();
		closeTab(w);
		resolveFn({ status: "cancelled" });
	};

	runSignIn(
		target,
		w,
		resolveFn,
		() => settled,
		(stop) => {
			stopPolling = stop;
		}
	).catch((e) => {
		// Belt and braces - nothing inside runSignIn should throw past its own
		// try/catches, but an unhandled rejection here would otherwise leave
		// the caller's promise pending forever.
		resolveFn({ status: "error", message: (e && e.message) || "Could not sign in." });
	});

	return promise;
}

async function runSignIn(target, w, resolve, isSettled, setStopPolling) {
	let name = target;
	if (typeof target === "function") {
		try {
			name = await target();
		} catch (e) {
			closeTab(w);
			resolve({ status: "error", message: (e && e.message) || "Could not sign in." });
			return;
		}
	}
	if (isSettled()) return; // cancelled while the factory was creating the row

	let res;
	try {
		res = await api.connectOauth(name);
	} catch (e) {
		closeTab(w);
		resolve({ status: "error", message: (e && e.message) || "Could not sign in." });
		return;
	}
	if (isSettled()) return;
	if (!res || !res.ok || !res.url) {
		closeTab(w);
		resolve({
			status: "error",
			message: (res && res.error && res.error.message) || "Could not sign in.",
		});
		return;
	}

	if (!w) {
		// Popup blocked: fall back to navigating this tab away. The vendor
		// callback page's Back link returns the user with ?settings=connectors
		// so ConnectorsPane can pick the flow back up - nothing left to poll here.
		nav.go(res.url);
		resolve({ status: "navigated" });
		return;
	}
	w.location.href = res.url;
	setStopPolling(pollUntilDone(name, w, res.started_at, resolve));
}

// One centered line, no external asset - this tab only exists for the few
// seconds before it navigates to the vendor.
function openInterimTab(label, agentName) {
	let w = null;
	try {
		w = window.open("", "_blank");
	} catch (e) {
		w = null;
	}
	if (!w) return null;
	try {
		w.opener = null;
	} catch (e) {
		/* cross-origin or a browser that disallows the write - harmless either way */
	}
	try {
		w.document.write(
			`<!doctype html><title>${escapeHtml(agentName || "")}</title>` +
				`<body style="margin:0;height:100vh;display:flex;align-items:center;` +
				`justify-content:center;background:#FFFFFF;color:#383838;` +
				`font:14px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">` +
				`<div>Connecting to ${escapeHtml(label || "")}…</div></body>`
		);
		w.document.close();
	} catch (e) {
		/* interim tab is cosmetic only - a write failure here doesn't block sign-in */
	}
	return w;
}

function closeTab(w) {
	try {
		if (w && !w.closed) w.close();
	} catch (e) {
		/* nothing left to do if a cross-origin tab refuses to close */
	}
}

// Checks the sign-in status once. Returns {done:true, result} once the flow
// has a verdict, or {done:false} to keep polling (including on a network
// error for this tick, which is ignored rather than treated as failure).
async function checkSignInStatus(name, startedAt) {
	let s;
	try {
		s = await api.oauthSigninStatus(name);
	} catch (e) {
		return { done: false };
	}
	if (!s) return { done: false };
	// {ok:false, error:{code,message}} is a call-level failure (e.g. a
	// permission check), not the one-shot parked-error string below - both
	// are terminal, but this one carries its message a level deeper.
	if (s.ok === false) {
		return {
			done: true,
			result: {
				status: "error",
				message: (s.error && s.error.message) || "Could not sign in.",
			},
		};
	}
	if (typeof s.error === "string" && s.error) {
		return { done: true, result: { status: "error", message: s.error } };
	}
	if (s.connected && s.connected_at && new Date(s.connected_at) > new Date(startedAt)) {
		return { done: true, result: { status: "connected" } };
	}
	return { done: false };
}

// Polls until the flow has a verdict, calling `resolve` exactly once. Returns
// a `stop()` function the caller can invoke to cancel the loop early without
// resolving anything itself (signIn()'s own .cancel() does that part).
function pollUntilDone(name, w, startedAt, resolve) {
	const deadline = Date.now() + POLL_TIMEOUT_MS;
	let stopped = false;
	let timer = null;

	async function tick() {
		if (stopped) return;
		const outcome = await checkSignInStatus(name, startedAt);
		if (stopped) return;
		if (outcome.done) {
			closeTab(w);
			resolve(outcome.result);
			return;
		}
		if (w && w.closed) {
			// The callback may have landed a moment before the user closed the
			// tab - give it one more chance before calling the sign-in abandoned.
			const second = await checkSignInStatus(name, startedAt);
			if (stopped) return;
			resolve(second.done ? second.result : { status: "closed" });
			return;
		}
		if (Date.now() >= deadline) {
			// Leave the tab open - the user may still be mid-flow in it.
			resolve({ status: "timeout" });
			return;
		}
		timer = setTimeout(tick, POLL_INTERVAL_MS);
	}
	timer = setTimeout(tick, POLL_INTERVAL_MS);

	return () => {
		stopped = true;
		if (timer) clearTimeout(timer);
	};
}
