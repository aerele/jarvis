import { ref } from "vue";

/**
 * The install prompt, captured the moment Chrome offers it.
 *
 * This listener is deliberately registered at MODULE load — imported by main.js
 * before the app mounts — and not inside a component's onMounted. Chrome fires
 * `beforeinstallprompt` as soon as the page meets the install criteria, which on
 * a warm refresh (bundle precached, worker already active) lands in the same
 * millisecond the app mounts (measured: event at 141ms, mount at 141ms). A
 * listener attached in onMounted therefore loses the race intermittently, the
 * event is gone for that page load, and the install banner silently never
 * appears — exactly the "no install option on refresh" symptom.
 *
 * The event fires at most once per page load, so it must be stashed rather than
 * waited for: calling prompt() later is what actually opens the install dialog.
 */
export const installPrompt = ref(null);

// True once the app is running as an installed app — there is nothing left to offer.
export function isStandalone() {
	return (
		window.matchMedia("(display-mode: standalone)").matches ||
		window.navigator.standalone === true
	);
}

window.addEventListener("beforeinstallprompt", (e) => {
	// Suppress Chrome's own mini-infobar; we surface the offer in-app instead.
	e.preventDefault();
	installPrompt.value = e;
});

// Chrome fires this after a successful install; drop the stale event so the
// banner doesn't linger on the next navigation.
window.addEventListener("appinstalled", () => {
	installPrompt.value = null;
});
