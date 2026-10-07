// Who the Frappe session cookie says is logged in, or null for Guest/absent.
// Dependency-free so lib/errors.js (and its node:test suite) can use it without
// pulling in vue / frappe-ui via data/session.js.
export function cookieUser() {
	const cookies = new URLSearchParams(document.cookie.split("; ").join("&"));
	let user = cookies.get("user_id");
	if (user === "Guest") user = null;
	return user ? decodeURIComponent(user) : null;
}
