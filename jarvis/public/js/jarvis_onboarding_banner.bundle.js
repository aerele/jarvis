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
// The jarvis_onboarded flag is written ONCE per page load, so a Desk that
// stays open across a setup finishing elsewhere keeps nagging until a hard
// reload. The reported case: open the Desk, go to /jarvis, complete
// onboarding, press Back. The browser restores the Desk from bfcache, no boot
// runs, and the nudge is still sitting there on a workspace that is now set
// up. So the nudge re-asks the server whenever the page is shown again, and
// only while the flag still says not-onboarded, which leaves a set-up Desk at
// zero extra round trips.

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
	// The stuck-apply Retry lever (jarvis#825). Admin-gated server-side
	// (require_jarvis_admin, which accepts System Manager - the same role this
	// nudge is already gated to), probe-first and throttled 180s.
	var RESYNC_METHOD = "jarvis.onboarding.resync_llm";
	// Deep link to the AI models settings tab, used by the reconnect variant's
	// CTA. Must match config.mjs's AI_MODELS_SETTINGS_URL: this standalone desk
	// bundle is loaded via app_include_js and cannot import from the widget
	// module graph, so the string is kept in sync here by hand.
	var AI_MODELS_SETTINGS_URL = "/jarvis/?settings=aimodels";
	// Floor between server re-checks for the chatty triggers (route change,
	// tab focus). A bfcache restore bypasses it: that is the exact moment the
	// flag is most likely stale and the user is looking straight at it.
	var RECHECK_MIN_MS = 30 * 1000;
	var lastCheckAt = 0;
	var checking = false;

	// ERPNext's own setup wizard must be finished first: completing it creates
	// the first Company. Until then the desk IS the setup wizard, so nudging the
	// user to set up Jarvis on top of it is just noise.
	// frappe.boot.sysdefaults.setup_complete is frappe.is_setup_complete().
	function erpnextSetupComplete() {
		if (!window.frappe || !frappe.boot) return false;
		return (frappe.boot.sysdefaults || {}).setup_complete == 1;
	}

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

	function shouldShow() {
		if (!window.frappe || !frappe.boot) return false;
		if (frappe.boot.jarvis_onboarded !== false) return false;
		// jarvis C2: is_ready_for_chat itself returns ready:false for the soft
		// "llm_applying" reason (an established workspace's first pool/direct leg
		// is mid-apply), so jarvis_onboarded reads exactly like a never-set-up
		// workspace here - but it is not one. Suppress the nudge entirely rather
		// than routing it through nudgeVariant()'s reconnect copy: an established
		// workspace mid-apply needs no Desk nudge at all, and the "Set up Jarvis"
		// fallback pitch would be actively wrong for it.
		if ((frappe.boot.jarvis_ready_reason || "") === "llm_applying") return false;
		if (!erpnextSetupComplete()) return false;
		// A second, sturdier setup signal alongside the sysdefaults flag above:
		// the Company count (jarvis_site_setup_complete, set in jarvis.boot)
		// stays correct even when setup_complete was flipped by a fixture or a
		// restore. Strict === false so an older boot payload without the key
		// behaves exactly as before.
		if (frappe.boot.jarvis_site_setup_complete === false) return false;
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
		// The nudge's own visibility predicate, reused rather than re-listed.
		// If the nudge would not be on screen (already onboarded, mid-ERPNext
		// wizard, not a System Manager, dismissed, or on a hidden route) then
		// correcting the flag changes nothing anyone can see, so it is not
		// worth a round trip. Deriving it keeps the two from drifting apart
		// the next time a guard is added to shouldShow().
		if (!shouldShow()) return;
		if (checking) return;
		var now = new Date().getTime();
		if (!force && now - lastCheckAt < RECHECK_MIN_MS) return;
		lastCheckAt = now;
		checking = true;
		// Release the in-flight guard on EVERY outcome, including a synchronous
		// throw out of frappe.call before it ever returns a promise. A guard
		// left stuck on would silently kill every later re-check in this tab.
		try {
			var req = frappe.call({
				method: READY_METHOD,
				callback: function (r) {
					// Anything other than an explicit ready leaves the flag
					// alone. A failed or malformed check must never clear a
					// nudge the workspace still needs.
					if (r && r.message && r.message.ready) {
						frappe.boot.jarvis_onboarded = true;
						sync();
					}
				},
			});
			if (req && req.always) req.always(releaseCheck);
			else releaseCheck();
		} catch (e) {
			releaseCheck();
		}
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

	// Which bubble to show, decided from frappe.boot.jarvis_ready_reason (set in
	// jarvis.boot.set_jarvis_boot, same round trip as jarvis_onboarded). A
	// workspace that was NEVER onboarded and one that finished onboarding but
	// later lost its AI connection ("llm_credentials") both read jarvis_onboarded
	// === false, and would look identical to this nudge without the reason -
	// but the first needs the full wizard and the second only needs a model
	// reconnected, so they get different copy and a different destination.
	function nudgeVariant() {
		var reason = (window.frappe && frappe.boot && frappe.boot.jarvis_ready_reason) || "";
		// White-label: the reconnect copy names the agent, so its whole bubble -
		// text, header and aria-label - must not say "Jarvis" for a tenant that
		// renamed it. The never-onboarded pitch below is left as literal "Jarvis",
		// unchanged, per the existing copy.
		var agentName =
			(window.frappe && frappe.boot && frappe.boot.jarvis_agent_name) || "Jarvis";
		if (reason === "llm_credentials") {
			return {
				name: agentName,
				aria: "Reconnect " + agentName,
				text:
					"Your AI connection dropped, so " +
					agentName +
					" can't reply right now. Reconnect a model to pick up where you left off.",
				ctaLabel: "Reconnect a model →",
				href: AI_MODELS_SETTINGS_URL,
			};
		}
		// jarvis#825: an established workspace whose last AI update got stuck (the
		// soft llm_applying window aged out without converging). Like llm_credentials
		// this reads jarvis_onboarded === false but is NOT a never-set-up workspace,
		// so the generic "Set up Jarvis" pitch below would be actively wrong. Honest
		// copy + a Retry that re-drives the saved config IN PLACE (resync) rather than
		// sending them to the wizard - `action` instead of `href`, handled in inject().
		if (reason === "llm_apply_stuck") {
			return {
				name: agentName,
				aria: "Retry " + agentName + " update",
				text:
					"Your last " +
					agentName +
					" update didn't finish, so replies may fail. Retry to finish it.",
				ctaLabel: "Retry",
				action: "resync",
			};
		}
		// Every other reason (never onboarded, or an empty/unknown reason from an
		// older boot payload) keeps the original pitch and destination.
		return {
			name: "Jarvis",
			aria: "Set up Jarvis",
			text: "Hey 👋 I'm Jarvis. Set me up and I'll handle the ERP busywork like quotes, invoices, and reports.",
			ctaLabel: "Set up Jarvis →",
			href: "/jarvis/onboarding",
		};
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

		var variant = nudgeVariant();

		var wrap = document.createElement("div");
		wrap.id = NUDGE_ID;
		wrap.setAttribute("role", "complementary");
		wrap.setAttribute("aria-label", variant.aria);

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
				n.textContent = variant.name;
				b.appendChild(n);

				var t = document.createElement("div");
				t.textContent = variant.text;
				b.appendChild(t);

				// An in-place action (jarvis#825 Retry) is a <button> that stays on
				// the page; every other variant is an <a> that navigates to the wizard
				// or the settings pane.
				var cta;
				if (variant.action === "resync") {
					cta = document.createElement("button");
					cta.type = "button";
					cta.className = "jvn-btn";
					cta.textContent = variant.ctaLabel;
					cta.addEventListener("click", function () {
						retryApply(cta, variant.ctaLabel);
					});
				} else {
					cta = document.createElement("a");
					cta.className = "jvn-btn";
					cta.href = variant.href;
					cta.textContent = variant.ctaLabel;
				}
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

	// jarvis#825 Retry: re-drive the saved AI config, then re-read readiness and
	// re-sync the nudge from the fresh verdict. A successful resync flips the reason
	// back to llm_applying (or clears it once ready), and shouldShow() suppresses the
	// nudge for llm_applying - so the bubble visibly goes away on success rather than
	// leaving stale "didn't finish" copy on screen. frappe.boot.jarvis_ready_reason is
	// a cached boot value that reverify() never rewrites (it only corrects the
	// onboarded flag false->true), so this updates it by hand from the fresh check.
	function retryApply(btn, label) {
		if (btn.disabled) return;
		btn.disabled = true;
		btn.textContent = "Retrying…";
		var restore = function () {
			btn.disabled = false;
			btn.textContent = label;
		};
		// Re-read readiness, update the cached boot flags from the fresh verdict,
		// then re-sync the nudge. Returns the readiness call so the button can be
		// restored only AFTER this settles - not before.
		var recheck = function () {
			return frappe.call({
				method: READY_METHOD,
				callback: function (r) {
					var m = (r && r.message) || null;
					if (m) {
						frappe.boot.jarvis_onboarded = !!m.ready;
						frappe.boot.jarvis_ready_reason = m.reason || "";
					}
					sync();
				},
			});
		};
		// The whole point is that a stuck-apply retry must NOT re-enable the button
		// until its outcome is on screen: resync -> recheck -> THEN restore. Chaining
		// restore onto the resync call directly (a sibling of recheck) would flip the
		// button back mid-recheck, letting an admin double-click straight into the
		// server's 180s throttle for no benefit. So restore rides recheck's own
		// completion, and recheck only starts once resync has settled.
		var afterResync = function () {
			var rq;
			try {
				rq = recheck();
			} catch (e) {
				restore();
				return;
			}
			if (rq && rq.always) rq.always(restore);
			else restore();
		};
		try {
			var req = frappe.call({ method: RESYNC_METHOD });
			if (req && req.then) req.then(afterResync, afterResync);
			else afterResync();
		} catch (e) {
			restore();
		}
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
