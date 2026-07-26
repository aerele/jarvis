// Not-onboarded desk nudge — a chat popup that pops out of the Jarvis chat
// launcher (bottom-right of the Desk), styled like Jarvis is messaging the
// user to finish setup and start easing their ERP workflows. Replaces the old
// top banner with a friendlier, on-brand "Jarvis is chatting" bubble thread.
//
// Reads `frappe.boot.jarvis_onboarded` (set in jarvis.boot.set_jarvis_boot)
// and `frappe.boot.sysdefaults.setup_complete` (frappe.is_setup_complete()).
// Shows only for a not-onboarded System Manager AFTER ERPNext's own setup
// wizard is finished, so it never pops over the wizard. Dismissal is
// per-tab-session (sessionStorage) so it returns next fresh session until
// setup is finished. Loaded on every Desk page via hooks.app_include_js.
//
// That boot flag is written ONCE per page load, so a Desk that stays open
// across a setup finishing elsewhere keeps nagging until a hard reload. The
// reported case: open the Desk, go to /jarvis, complete onboarding, press
// Back. The browser restores the Desk from bfcache, no boot runs, and the
// nudge is still sitting there on a workspace that is now set up. So the
// nudge re-asks the server whenever the page is shown again, and only while
// the flag still says not-onboarded, which leaves a set-up Desk at zero
// extra round trips.

(function () {
	if (window.__jarvisOnboardingBanner) return;
	window.__jarvisOnboardingBanner = true;

	var DISMISS_KEY = "jarvis_onboarding_nudge_dismissed";
	var NUDGE_ID = "jarvis-onboarding-nudge";
	var STYLE_ID = "jarvis-onboarding-nudge-style";
	// No point nagging a user already mid-setup on the legacy desk page.
	var HIDE_ON_ROUTES = ["jarvis-onboarding"];
	// Same check the SPA gate and the widget popup use, so all three surfaces
	// agree on what "set up" means.
	var READY_METHOD = "jarvis.account.is_ready_for_chat";
	// Floor between server re-checks for the chatty triggers (route change,
	// tab focus). A bfcache restore bypasses it: that is the exact moment the
	// flag is most likely stale and the user is looking straight at it.
	var RECHECK_MIN_MS = 30 * 1000;
	var lastCheckAt = 0;
	var checking = false;

	function isSystemManager() {
		return !!(
			window.frappe &&
			frappe.user &&
			frappe.user.has_role &&
			frappe.user.has_role("System Manager")
		);
	}

	function dismissed() {
		try {
			return !!sessionStorage.getItem(DISMISS_KEY);
		} catch (e) {
			// sessionStorage unavailable (privacy mode etc.), so treat it as
			// not dismissed and show the nudge.
			return false;
		}
	}

	// frappe.is_setup_complete(), which flips true once ERPNext's own setup
	// wizard finishes and the first Company exists. A missing value fails safe
	// to "not complete" so the nudge stays hidden rather than racing a wizard.
	function setupComplete() {
		return (
			!!window.frappe && !!frappe.boot && (frappe.boot.sysdefaults || {}).setup_complete == 1
		);
	}

	function shouldShow() {
		if (!window.frappe || !frappe.boot) return false;
		if (frappe.boot.jarvis_onboarded !== false) return false;
		// ERPNext's own setup wizard must be finished first: completing it
		// creates the first Company. Until then the desk IS the setup wizard,
		// so nudging the user to set up Jarvis on top of it is just noise.
		// frappe.boot.sysdefaults.setup_complete is frappe.is_setup_complete().
		if (!setupComplete()) return false;
		if (!isSystemManager()) return false;
		if (dismissed()) return false;
		var route = (frappe.get_route && frappe.get_route()) || [];
		if (HIDE_ON_ROUTES.indexOf(route[0] || "") !== -1) return false;
		return true;
	}

	// Ask the server whether setup finished since this page booted. Only ever
	// corrects false to true: nothing can make a set-up workspace un-set-up
	// mid-session, and an expiring credential is the degraded path's problem,
	// not this nudge's.
	function reverify(force) {
		if (!window.frappe || !frappe.boot) return;
		if (frappe.boot.jarvis_onboarded !== false) return;
		// Nobody would see the correction, so do not spend a round trip on it.
		// setupComplete() belongs here too: during ERPNext's setup wizard the
		// nudge is gated off entirely, so re-checking would poll the server on
		// every route change for something that can never be shown.
		if (!setupComplete() || !isSystemManager() || dismissed()) return;
		if (checking) return;
		var now = new Date().getTime();
		if (!force && now - lastCheckAt < RECHECK_MIN_MS) return;
		lastCheckAt = now;
		checking = true;
		var req = frappe.call({
			method: READY_METHOD,
			callback: function (r) {
				// Anything other than an explicit ready leaves the flag alone.
				// A failed or malformed check must never clear a nudge the
				// workspace still needs.
				if (r && r.message && r.message.ready) {
					frappe.boot.jarvis_onboarded = true;
					sync();
				}
			},
		});
		// Release the in-flight guard on BOTH outcomes. A guard left stuck on
		// would silently kill every later re-check in this tab.
		if (req && req.always) req.always(releaseCheck);
		else releaseCheck();
	}

	function releaseCheck() {
		checking = false;
	}

	function dismiss() {
		try {
			sessionStorage.setItem(DISMISS_KEY, "1");
		} catch (e) {
			/* ignore */
		}
		remove();
	}

	function remove() {
		var el = document.getElementById(NUDGE_ID);
		if (el && el.parentNode) el.parentNode.removeChild(el);
	}

	function ensureStyles() {
		if (document.getElementById(STYLE_ID)) return;
		var st = document.createElement("style");
		st.id = STYLE_ID;
		// Colors come from the Desk's own theme tokens so the popup follows the
		// desk light/dark automatically (bubbles white↔dark; the CTA flips
		// black↔white via --text-color bg / --fg-color text). Hardcoded fallbacks
		// keep it sane if a token is ever missing.
		st.textContent =
			"@keyframes jvNudgeIn{from{opacity:0;transform:translateY(8px) scale(.98)}to{opacity:1;transform:none}}" +
			"#" +
			NUDGE_ID +
			"{position:fixed;right:24px;bottom:92px;z-index:1050;width:320px;max-width:calc(100vw - 40px);" +
			"font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;flex-direction:column;gap:8px;}" +
			"#" +
			NUDGE_ID +
			" .jvn-row{display:flex;opacity:0;animation:jvNudgeIn .32s cubic-bezier(.2,.7,.3,1) forwards;}" +
			"#" +
			NUDGE_ID +
			" .jvn-row.r1{animation-delay:.05s;}" +
			"#" +
			NUDGE_ID +
			" .jvn-bubble{position:relative;background:var(--card-bg,#fff);border:1px solid var(--border-color,#e6e6ee);border-radius:14px;padding:10px 13px;font-size:13px;line-height:1.45;color:var(--text-color,#20232e);box-shadow:0 6px 22px -8px rgba(20,20,40,.22);}" +
			"#" +
			NUDGE_ID +
			" .jvn-name{font-size:11px;font-weight:600;color:var(--text-muted,#8a8aa0);margin-bottom:3px;}" +
			"#" +
			NUDGE_ID +
			" .jvn-btn{display:inline-flex;align-items:center;gap:6px;margin-top:10px;background:var(--text-color,#16181d);color:var(--fg-color,#fff);border:0;border-radius:8px;padding:7px 13px;font-size:12.5px;font-weight:600;text-decoration:none;cursor:pointer;}" +
			"#" +
			NUDGE_ID +
			" .jvn-x{position:absolute;top:-9px;right:-9px;width:22px;height:22px;border-radius:50%;background:var(--card-bg,#fff);border:1px solid var(--border-color,#e6e6ee);color:var(--text-muted,#6b6b80);font-size:13px;line-height:1;cursor:pointer;box-shadow:0 2px 6px rgba(20,20,40,.14);display:flex;align-items:center;justify-content:center;}" +
			// Speech tail: a small oval + a trailing smaller oval that drift down
			// toward the launcher, so it reads as a soft cloud puff rather than the
			// old hard rotated square.
			"#" +
			NUDGE_ID +
			" .jvn-tail{position:relative;align-self:flex-end;margin:4px 34px 0 0;width:15px;height:11px;border-radius:50%;background:var(--card-bg,#fff);border:1px solid var(--border-color,#e6e6ee);box-shadow:0 4px 12px -6px rgba(20,20,40,.2);opacity:0;animation:jvNudgeIn .32s ease .5s forwards;}" +
			"#" +
			NUDGE_ID +
			" .jvn-tail::after{content:'';position:absolute;right:-10px;bottom:-8px;width:8px;height:7px;border-radius:50%;background:var(--card-bg,#fff);border:1px solid var(--border-color,#e6e6ee);box-shadow:0 4px 12px -6px rgba(20,20,40,.2);}";
		document.head.appendChild(st);
	}

	function bubbleRow(cls, buildBubble) {
		var row = document.createElement("div");
		row.className = "jvn-row " + cls;
		row.appendChild(buildBubble());
		return row;
	}

	function inject() {
		if (document.getElementById(NUDGE_ID)) return;
		ensureStyles();

		var wrap = document.createElement("div");
		wrap.id = NUDGE_ID;
		wrap.setAttribute("role", "complementary");
		wrap.setAttribute("aria-label", "Set up Jarvis");

		// Single bubble — greeting + pitch + CTA + dismiss.
		wrap.appendChild(
			bubbleRow("r1", function () {
				var b = document.createElement("div");
				b.className = "jvn-bubble";

				var x = document.createElement("button");
				x.type = "button";
				x.className = "jvn-x";
				x.setAttribute("aria-label", "Dismiss");
				x.textContent = "×";
				x.addEventListener("click", dismiss);
				b.appendChild(x);

				var n = document.createElement("div");
				n.className = "jvn-name";
				n.textContent = "Jarvis";
				b.appendChild(n);

				var t = document.createElement("div");
				t.textContent =
					"Hey 👋 I'm Jarvis. Set me up and I'll handle the ERP busywork like quotes, invoices, and reports.";
				b.appendChild(t);

				var cta = document.createElement("a");
				cta.className = "jvn-btn";
				cta.href = "/jarvis/onboarding";
				cta.textContent = "Set up Jarvis →";
				b.appendChild(cta);
				return b;
			})
		);

		// Tail pointing down toward the chat launcher.
		var tail = document.createElement("div");
		tail.className = "jvn-tail";
		wrap.appendChild(tail);

		document.body.appendChild(wrap);
	}

	function sync() {
		if (shouldShow()) inject();
		else remove();
	}

	// Render from what this page already knows, then confirm it against the
	// server. Wrapped at every call site rather than passed as a handler
	// directly, because a listener's own argument (the route, the event) would
	// land in `force` and defeat the throttle.
	function syncAndVerify(force) {
		sync();
		reverify(force);
	}

	function start() {
		if (!window.frappe) return;
		syncAndVerify(false);
		if (frappe.router && frappe.router.on)
			frappe.router.on("change", function () {
				syncAndVerify(false);
			});
		// bfcache restore, which is the Back-from-onboarding case: `persisted`
		// means no boot ran, so the flag is whatever it was before the user
		// left. Force past the throttle, the answer is the whole point of the
		// navigation.
		window.addEventListener("pageshow", function (e) {
			if (e && e.persisted) syncAndVerify(true);
		});
		// Setup finished in another tab and the user came back to this one.
		document.addEventListener("visibilitychange", function () {
			if (document.visibilityState === "visible") syncAndVerify(false);
		});
	}

	if (window.frappe && window.frappe.router) {
		start();
	} else if (window.$) {
		$(start);
	} else {
		document.addEventListener("DOMContentLoaded", start);
	}
})();
