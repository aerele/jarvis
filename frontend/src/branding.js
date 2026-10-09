// Whitelabel branding. It starts from the www/jarvis.py boot payload
// (window.agent_name / window.brand_logo_url / window.brand_favicon_url) and
// changes in place when the Branding pane saves (applyBranding), so a new brand
// shows at once, with no reload (admin-v2#622). Read it from `brand` where it is
// used; a copy made at setup or module load goes stale. Blank values fall back to
// the Jarvis defaults.
import { reactive } from "vue";

const DEFAULT_NAME = "Jarvis";

export const brand = reactive({
	name: (window.agent_name || "").trim(),
	logoUrl: (window.brand_logo_url || "").trim(),
	faviconUrl: (window.brand_favicon_url || "").trim(),
	get agentName() {
		return this.name || DEFAULT_NAME;
	},
	// True once the customer has set any custom identity (name or logo). Lets copy
	// decide between "Jarvis" defaults and the tenant brand.
	get isWhitelabeled() {
		return this.agentName !== DEFAULT_NAME || !!this.logoUrl;
	},
});

// Shared worker-warning banner copy: OnboardingGate.vue and OnboardingView.vue
// both render this Banner for the same degraded-worker signal, and previously
// hardcoded the identical title + interpolated message in two places.
export const WORKER_WARNING_TITLE = "Replies may be slow";
export function workerWarningMessage(name) {
	return `${name} is low on background workers, so answers may take longer. Add more workers to your bench to speed this up.`;
}

// The shell's own title and icon links, kept so a removed brand puts them back.
let defaultChrome = null;

// Patch the browser-tab title + favicon to the tenant brand. Called at boot from
// main.js (safe before mount) and again by applyBranding. Best-effort - never blocks.
export function applyBrandChrome() {
	try {
		const links = [
			...document.querySelectorAll('link[rel="icon"], link[rel="apple-touch-icon"]'),
		];
		if (!defaultChrome) {
			defaultChrome = {
				title: document.title,
				links: links.map((l) => ({
					el: l,
					href: l.getAttribute("href"),
					sizes: l.getAttribute("sizes"),
					type: l.getAttribute("type"),
				})),
			};
		}
		document.title =
			brand.agentName !== DEFAULT_NAME ? `${brand.agentName} Chat` : defaultChrome.title;
		if (!defaultChrome.links.length) {
			// No icon link in the shell: add one for the brand favicon.
			let added = document.querySelector("link[data-jv-brand-icon]");
			if (brand.faviconUrl && !added) {
				added = document.createElement("link");
				added.rel = "icon";
				added.setAttribute("data-jv-brand-icon", "");
				document.head.appendChild(added);
			}
			if (added) {
				if (brand.faviconUrl) added.href = brand.faviconUrl;
				else added.remove();
			}
			return;
		}
		// The static shell ships Jarvis PNGs; retarget every icon link so the tab
		// favicon tracks the tenant brand, and restore them when it is removed.
		for (const { el, href, sizes, type } of defaultChrome.links) {
			for (const [attr, value] of [
				["href", brand.faviconUrl || href],
				["sizes", brand.faviconUrl ? null : sizes],
				["type", brand.faviconUrl ? null : type],
			]) {
				if (value == null) el.removeAttribute(attr);
				else el.setAttribute(attr, value);
			}
		}
	} catch (e) {
		/* chrome patch is best-effort */
	}
}

/** Apply a saved brand ({agent_name, brand_logo_url, brand_favicon_url}) in place. */
export function applyBranding(data) {
	brand.name = (data?.agent_name || "").trim();
	brand.logoUrl = (data?.brand_logo_url || "").trim();
	brand.faviconUrl = (data?.brand_favicon_url || "").trim();
	applyBrandChrome();
}
