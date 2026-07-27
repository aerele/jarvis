// Whitelabel branding, delivered by the www/jarvis.py boot payload as
// window.agent_name / window.brand_logo_url / window.brand_favicon_url. Blank
// values fall back to the Jarvis defaults. Read once at module load - the boot
// values are stable for the page's lifetime.

const DEFAULT_NAME = "Jarvis";

export const agentName = (window.agent_name || "").trim() || DEFAULT_NAME;
export const brandLogoUrl = (window.brand_logo_url || "").trim();
export const brandFaviconUrl = (window.brand_favicon_url || "").trim();

// True once the customer has set any custom identity (name or logo). Lets copy
// decide between "Jarvis" defaults and the tenant brand.
export const isWhitelabeled = agentName !== DEFAULT_NAME || !!brandLogoUrl;

// Patch the browser-tab title + favicon to the tenant brand. Called once at
// boot from main.js; safe before mount. Best-effort - never blocks boot.
export function applyBrandChrome() {
	try {
		if (agentName !== DEFAULT_NAME) {
			document.title = `${agentName} Chat`;
		}
		if (brandFaviconUrl) {
			const links = document.querySelectorAll(
				'link[rel="icon"], link[rel="apple-touch-icon"]'
			);
			if (links.length) {
				// The static shell ships Jarvis PNGs; retarget every icon link so
				// the tab favicon tracks the tenant brand.
				links.forEach((l) => {
					l.setAttribute("href", brandFaviconUrl);
					l.removeAttribute("sizes");
					l.removeAttribute("type");
				});
			} else {
				const l = document.createElement("link");
				l.rel = "icon";
				l.href = brandFaviconUrl;
				document.head.appendChild(l);
			}
		}
	} catch (e) {
		/* chrome patch is best-effort */
	}
}
