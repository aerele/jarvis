// Which links a dashboard frame may ask the parent to open. The frame's HTML is
// model-written, so it is untrusted: only same-origin Desk (/app/) and Jarvis
// SPA (/jarvis/) paths open, never /api/, /files/, other origins or javascript:.
const OPENABLE_PREFIXES = ["/app/", "/jarvis/"];

/** Resolve a frame-supplied href to an absolute URL to open, or null if refused. */
export function deskLinkUrl(href, origin) {
	if (typeof href !== "string" || !href.trim()) return null;
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
