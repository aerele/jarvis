// Global immersive chrome for the customer-facing Jarvis Desk pages, plus the
// client-side redirect for the Desk pages that have since moved into the SPA.
//
// Loaded on EVERY Desk page via hooks.app_include_js. On the Jarvis routes
// (chat / onboarding) it hides the Frappe navbar + page-head so the page reads
// as a native full-screen product; it reverts on every other route.
//
// Why a global content-hashed bundle (not the page CSS): a hashed asset can't
// be served stale the way a plain CSS file can — that was why the header
// "wouldn't disappear" on the account page. It also covers onboarding, which
// has no chat bundle of its own.
//
// The onboarding page already paints a full-viewport `position:fixed` overlay
// (.jo-bg, inset:0), so once the navbar is hidden it uses the whole screen on
// its own — no extra layout CSS needed. The chat page manages its own height
// (mount.js); dropping the navbar's vertical space is enough.

(function () {
	if (window.__jarvisImmersive) return;
	window.__jarvisImmersive = true;

	const ROUTES = ["jarvis-chat", "jarvis-onboarding"];

	// Desk routes that have moved into the SPA. The hooks.py website_redirects
	// entry handles a fresh HTTP request, but a customer already inside Desk
	// navigates client-side: frappe's own router resolves the route with no
	// server round-trip, so nothing server-side ever gets the chance to
	// redirect and they would land on "Page not found" instead of billing.
	const RETIRED = { "jarvis-account": "/jarvis/billing" };

	const CSS =
		"body.jarvis-immersive header.navbar," +
		"body.jarvis-immersive .navbar," +
		"body.jarvis-immersive .page-head{display:none!important}" +
		"body.jarvis-immersive .main-section{padding-top:0!important}";

	function ensureStyle() {
		if (document.getElementById("jarvis-immersive-style")) return;
		const s = document.createElement("style");
		s.id = "jarvis-immersive-style";
		s.textContent = CSS;
		document.head.appendChild(s);
	}

	function sync() {
		ensureStyle();
		const route = (window.frappe && frappe.get_route && frappe.get_route()) || [];
		const head = route[0] || "";

		const movedTo = RETIRED[head];
		if (movedTo) {
			// replace(), not assign(): the retired route must not stay in history,
			// or Back lands on it and bounces the customer straight out again.
			window.location.replace(movedTo);
			return;
		}

		document.body.classList.toggle("jarvis-immersive", ROUTES.indexOf(head) !== -1);
	}

	if (window.frappe && frappe.router && frappe.router.on) {
		frappe.router.on("change", sync);
	}
	$(document).on("page-change", sync);
	$(sync); // run once on ready
})();
