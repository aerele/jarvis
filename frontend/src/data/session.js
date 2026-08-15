// Auth = the Frappe session cookie (same as Desk). We just read who's logged in;
// if nobody is, the SPA bounces to Frappe's /login with a redirect back.
import { reactive, computed } from "vue";
import { call } from "frappe-ui";

export function sessionUser() {
	const cookies = new URLSearchParams(document.cookie.split("; ").join("&"));
	let user = cookies.get("user_id");
	if (user === "Guest") user = null;
	return user ? decodeURIComponent(user) : null;
}

export const session = reactive({
	user: sessionUser(),
	isLoggedIn: computed(() => !!session.user),
	async logout() {
		// Frappe's core logout is POST-only; a GET nav 403s. call() POSTs to
		// /api/method/logout with the CSRF token, clearing the session cookie.
		try {
			await call("logout");
		} catch {
			// Session may already be gone; head to /login either way.
		}
		window.location.href = "/login";
	},
});

export function requireLogin() {
	if (!session.isLoggedIn) {
		window.location.href = `/login?redirect-to=${encodeURIComponent(
			window.location.pathname
		)}`;
		return false;
	}
	return true;
}
