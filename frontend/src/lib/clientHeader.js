/**
 * Marks every same-origin /api/ request with `X-Jarvis-Client: desktop|pwa`.
 *
 * The bench labels the admin's "User activity" rows by surface and, for the stock
 * Frappe `upload_file` (which Desk uses too), tells a Jarvis upload from a Desk one by
 * this header alone (jarvis/activity_actions.py). It is a LABEL, never a gate: a caller
 * can forge it and gains nothing.
 *
 * One shim for both apps (the PWA imports it through `@shared`), installed ONCE, before
 * anything can fire a request, by patching the two browser primitives every request goes
 * through, so frappe-ui's `call`/`frappeRequest` (fetch), the raw `fetch` sites and
 * frappe-ui's FileUploader (XMLHttpRequest) are all covered without touching each caller:
 *
 *   - window.fetch
 *   - XMLHttpRequest.prototype.open / send
 *
 * Cross-origin requests are left exactly as they were, as is anything outside /api/.
 * It never throws into the caller: if the header cannot be added, the request goes out
 * unmarked.
 */

export const CLIENT_HEADER = "X-Jarvis-Client";
const API_PREFIX = "/api/";
const INSTALLED = Symbol.for("jarvis.clientHeader");

/** True for an absolute or relative URL that points at this origin's /api/. */
export function isSameOriginApi(url, win = window) {
	try {
		const target = new URL(String(url), win.location.href);
		return target.origin === win.location.origin && target.pathname.startsWith(API_PREFIX);
	} catch {
		return false;
	}
}

function urlOf(input) {
	if (input && typeof input === "object" && "url" in input) return input.url; // Request
	return input; // string or URL
}

export function install(client, win = window) {
	if (win[INSTALLED]) return;
	win[INSTALLED] = client;

	const nativeFetch = win.fetch;
	if (typeof nativeFetch === "function") {
		win.fetch = function patchedFetch(input, init) {
			try {
				if (isSameOriginApi(urlOf(input), win)) {
					const HeadersCtor = win.Headers || globalThis.Headers;
					const RequestCtor = win.Request || globalThis.Request;
					const headers = new HeadersCtor(
						(init && init.headers) || (input && input.headers) || undefined
					);
					headers.set(CLIENT_HEADER, client);
					// A Request carries its own headers: when the caller passed only a
					// Request, rebuild it with ours; otherwise `init` overrides.
					if (
						init === undefined &&
						typeof RequestCtor === "function" &&
						input instanceof RequestCtor
					) {
						return nativeFetch.call(win, new RequestCtor(input, { headers }));
					}
					return nativeFetch.call(win, input, { ...init, headers });
				}
			} catch {
				// fall through: send it unmarked rather than fail the call
			}
			return nativeFetch.call(win, input, init);
		};
	}

	const Xhr = win.XMLHttpRequest;
	if (Xhr && Xhr.prototype) {
		const proto = Xhr.prototype;
		const nativeOpen = proto.open;
		const nativeSend = proto.send;
		const marked = new WeakSet();
		proto.open = function patchedOpen(method, url, ...rest) {
			try {
				if (isSameOriginApi(url, win)) marked.add(this);
				else marked.delete(this); // an XHR object reused for another URL
			} catch {
				// unmarked
			}
			return nativeOpen.call(this, method, url, ...rest);
		};
		proto.send = function patchedSend(...args) {
			try {
				if (marked.has(this)) this.setRequestHeader(CLIENT_HEADER, client);
			} catch {
				// e.g. the caller already closed it: send it unmarked
			}
			return nativeSend.apply(this, args);
		};
	}
}
