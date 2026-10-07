// Which links a dashboard frame may ask the parent to open. The frame's HTML is
// model-written, so it is untrusted: only same-origin Desk (/app/) and Jarvis
// SPA (/jarvis/) paths open, never /api/, /files/, other origins or javascript:.
// /desk/ is where Frappe v16 redirects /app/.
const OPENABLE_PREFIXES = ["/app/", "/desk/", "/jarvis/"];

/** Resolve a frame-supplied href to an absolute URL to open, or null if refused. */
export function deskLinkUrl(href, origin) {
	if (typeof href !== "string" || !href || href !== href.trim()) return null;
	let url;
	try {
		url = new URL(href, origin);
	} catch {
		return null;
	}
	if (url.origin !== origin) return null;
	if (!OPENABLE_PREFIXES.some((p) => url.pathname.startsWith(p))) return null;
	return url.href;
}

/** Open a frame-requested link in a new tab if allowed and user-initiated. */
export function openDeskLink(href, win = window) {
	// A forged message with no click behind it must not open tabs (Chrome carries
	// the frame click's activation to the parent); old browsers fall back to the popup blocker.
	const ua = win.navigator && win.navigator.userActivation;
	if (ua && !ua.isActive) return false;
	const url = deskLinkUrl(href, win.location.origin);
	if (!url) return false;
	win.open(url, "_blank", "noopener,noreferrer");
	return true;
}
