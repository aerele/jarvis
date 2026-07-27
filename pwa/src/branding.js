// Whitelabel branding for the mobile PWA, delivered by the jarvis_mobile.py
// boot payload (window.agent_name / window.brand_logo_url /
// window.brand_favicon_url). Blank falls back to the Jarvis defaults. Mirrors
// the desktop SPA's src/branding.js.

const DEFAULT_NAME = "Jarvis";

export const agentName = (window.agent_name || "").trim() || DEFAULT_NAME;
export const brandLogoUrl = (window.brand_logo_url || "").trim();
export const brandFaviconUrl = (window.brand_favicon_url || "").trim();

// Patch the tab title + favicon and repoint the manifest link to the per-tenant
// manifest (jarvis/pwa.py ManifestRenderer). The new manifest URL is OUTSIDE the
// service worker's precache, so it never serves the stale build-time one. Called
// once from main.js; best-effort.
export function applyBrandChrome() {
	try {
		if (agentName !== DEFAULT_NAME) document.title = agentName;
		if (brandFaviconUrl) {
			document
				.querySelectorAll('link[rel="icon"], link[rel="apple-touch-icon"]')
				.forEach((l) => {
					l.setAttribute("href", brandFaviconUrl);
					l.removeAttribute("sizes");
					l.removeAttribute("type");
				});
		}
		const m = document.querySelector('link[rel="manifest"]');
		if (m) m.setAttribute("href", "/jarvis-mobile.webmanifest");
	} catch (e) {
		/* best-effort */
	}
}
