// `__()` for SPA copy: Frappe's translator when the page has one, else the source
// string with {0}-style placeholders filled. Returns plain text (callers escape it
// for an HTML sink).
export function __(text, args = []) {
	const t = typeof window !== "undefined" && typeof window.__ === "function" ? window.__ : null;
	if (t) return t(text, args);
	return String(text).replace(/\{(\d+)\}/g, (m, i) => (args[i] != null ? String(args[i]) : m));
}
