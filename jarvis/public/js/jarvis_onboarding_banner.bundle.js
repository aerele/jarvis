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
	// The SPA's billing page (frontend/src/router/index.js's "Billing" route,
	// mounted under createWebHistory("/jarvis")), used by the renew-plan CTA.
	// Same hand-kept-in-sync constraint as AI_MODELS_SETTINGS_URL above.
	var BILLING_URL = "/jarvis/billing";
	// The wizard resumes whatever step was last persisted unless the URL
	// explicitly says otherwise - a bare /jarvis/onboarding link lands a
	// reconnecting customer back on that stale step, not the reconnect offer.
	// Must match readiness.js's RECONNECT_INTENT_URL (hasReconnectIntent() is
	// what OnboardingView.vue checks), same hand-kept-in-sync constraint as the
	// two constants above.
	var RECONNECT_INTENT_URL = "/jarvis/onboarding?reconnect=1";
	// Floor between server re-checks for the chatty triggers (route change,
	// tab focus). A bfcache restore bypasses it: that is the exact moment the
	// flag is most likely stale and the user is looking straight at it.
	var RECHECK_MIN_MS = 30 * 1000;
	var lastCheckAt = 0;
	var checking = false;

	// Reasons that mean "an established workspace's control plane is between
	// states" (still mid-apply, a confirmed outage, an unresolved authority
	// incident, or an account moved to a different site) - none of them are
	// fixed by anything a Desk nudge can offer, every one is reachable only
	// AFTER is_ready_for_chat's local signup+credential checks already passed
	// (account.py's _admin_chat_gate), and there is no honest, specific copy
	// for them yet worth writing a whole bubble around - so the nudge stays
	// quiet instead. The SPA gate (frontend/src/onboarding/readiness.js's
	// NOT_ONBOARDED_REASONS) never force-redirects any of these either.
	//
	// Deliberately NOT here: llm_pool_provisioning / llm_provisioning. Those
	// two ALSO cover the exact "disconnected all models, apply-confirmed
	// marker cleared with no fresh apply timestamp to soften it into
	// llm_applying" shape for an established workspace (account.py's
	// _provisioning_verdict), and nothing else on the Desk offers that
	// workspace a CTA - suppressing them here would leave it with no way
	// back in. See nudgeVariant()'s own case for the two.
	var SUPPRESSED_REASONS = [
		// Pre-existing (jarvis C2): an established workspace's first pool/direct
		// leg is mid-apply.
		"llm_applying",
		"container_provisioning",
		"container_unavailable",
		"authority_repair_required",
		"site_replaced",
	];

	function isSuppressedReason(reason) {
		return SUPPRESSED_REASONS.indexOf(reason || "") !== -1;
	}

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
		// is_ready_for_chat returns ready:false for several reasons that read
		// exactly like a never-set-up workspace here (jarvis_onboarded === false)
		// but are not one - see SUPPRESSED_REASONS above. Suppress the nudge
		// entirely for those rather than routing them through nudgeVariant(): an
		// established workspace between states needs no Desk nudge at all, and
		// the "Set up Jarvis" fallback pitch would be actively wrong for it.
		if (isSuppressedReason(frappe.boot.jarvis_ready_reason)) return false;
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
		// account.py only ever returns this once signup + credentials have
		// already passed once (account._admin_chat_gate is the sole caller), so
		// a lapsed subscription always means "was working, now paused" - never
		// "never got that far". Renew is the one action that can fix it; the
		// wizard's signup step cannot (jarvis review: it would dead-end at the
		// duplicate-signup guard).
		if (reason === "subscription_suspended") {
			return {
				name: agentName,
				aria: "Renew " + agentName,
				text:
					"Your plan has lapsed, so " +
					agentName +
					" is paused. Renew to pick up where you left off.",
				ctaLabel: "Renew plan →",
				href: BILLING_URL,
			};
		}
		// Slice 4b (C10b): the subscription leg's OAuth strand aged out mid-connect
		// (account.py maps admin's chat_readiness "ReconnectRequired" here), also
		// only reachable after signup + credentials passed once. The recovery is
		// the wizard's OWN reconnect step (OnboardingView.vue's
		// startAccountReconnect / can_reconnect offer) - there is no equivalent
		// action in Settings, so this still points at the wizard, but with honest
		// copy instead of the never-set-up "meet Jarvis" pitch below, and at
		// RECONNECT_INTENT_URL rather than a bare /jarvis/onboarding: without the
		// reconnect=1 flag OnboardingView.vue resumes whatever step was last
		// persisted instead of rendering the reconnect offer, landing this
		// customer on the wrong screen.
		if (reason === "reconnect_required") {
			return {
				name: agentName,
				aria: "Reconnect " + agentName,
				text:
					"Your AI subscription needs reconnecting, so " +
					agentName +
					" can't reply right now. Reconnect to pick up where you left off.",
				ctaLabel: "Reconnect →",
				href: RECONNECT_INTENT_URL,
			};
		}
		// account.py's _provisioning_verdict: an established workspace (one
		// _has_been_chat_ready has already confirmed) whose apply-confirmed
		// marker was cleared - e.g. "disconnect AI model connections" in pool or
		// subscription mode - and whose last_sync_requested_at is either unset or
		// too stale to soften this into llm_applying/llm_apply_stuck. Unlike
		// those two soft reasons this is not necessarily still converging, so
		// point at Settings to check/redo the connection rather than promising a
		// Retry that may have nothing in flight to retry.
		if (reason === "llm_pool_provisioning" || reason === "llm_provisioning") {
			return {
				name: agentName,
				aria: "Check " + agentName + " AI models",
				text:
					"Your AI model connection is not finished applying, so " +
					agentName +
					" can't reply yet. Check it in Settings.",
				ctaLabel: "Check AI models →",
				href: AI_MODELS_SETTINGS_URL,
			};
		}
		// Every other reason keeps the original never-set-up pitch and
		// destination - deliberately, not just by omission:
		//   - "signup" / "" / unrecognised: no admin api_key yet, or an older
		//     boot payload with nothing to classify.
		//   - "llm_setup": account.py's own docstring guarantees this fires ONLY
		//     for a workspace that never finished onboarding (no LLM config ever
		//     confirmed AND the subscription never went Active) - a half-created
		//     signup, not an established one. Both frontend/src/onboarding/
		//     readiness.js and the widget's panel_readiness.mjs gate on it as
		//     "never onboarded" for the same reason.
		//   - "llm_rejected": a first sync admin explicitly refused. Established
		//     precedent (panel_readiness.mjs's own comment) is that "the desk
		//     onboarding banner routes it to setup too" so the three desk
		//     surfaces (SPA gate, widget, this nudge) agree - do not special-case
		//     it here without updating that comment and the other two surfaces.
		//   - "readiness_unconfirmed": account.py guarantees this only for a
		//     workspace nothing has ever confirmed ready (an established one
		//     fails OPEN through the same outage instead - _admin_unreachable_
		//     verdict) - so it never fires on a workspace with history either.
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

	// Test-only export: this file is a plain script loaded via app_include_js
	// (see the module comment at the top), not an ES module, so it cannot be
	// `import`ed the way the widget's *.mjs files are. `module` only exists
	// under CommonJS (node:test, which the sibling *.test.mjs file uses via
	// createRequire) - a real browser has no `module` global, so this is inert
	// in production and does not change what ships to the Desk.
	if (typeof module !== "undefined" && module.exports) {
		module.exports = { nudgeVariant: nudgeVariant, isSuppressedReason: isSuppressedReason };
	}

	if (window.frappe && window.frappe.router) {
		start();
	} else if (window.$) {
		$(start);
	} else {
		document.addEventListener("DOMContentLoaded", start);
	}
})();
