/* global Razorpay */ // loaded at runtime by Razorpay's checkout.js, not a module import
frappe.pages["jarvis-account"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "My Jarvis Account",
		single_column: true,
	});
	if (!window.Razorpay) {
		frappe.require("https://checkout.razorpay.com/v1/checkout.js");
	}

	injectStyles();

	const esc = frappe.utils.escape_html;
	const inr = (n) => "₹" + Number(n || 0).toLocaleString("en-IN");
	const cycleLabel = (c) => (String(c).toLowerCase() === "annual" ? "/year" : "/month");

	// LLM provider defaults - mirror the onboarding wizard's PROVIDER_DEFAULTS
	// so the section behaves identically there and here.
	//
	// The admin-managed catalog (api_key_models, fetched via get_chat_ui_settings
	// below) carries no base-URL data, so `.baseUrl` here stays the source of
	// truth. `.model` is this literal's FALLBACK only - providerDefaultModel()
	// below prefers the catalog's is_default flag, which also fixes this file's
	// own copy of the stale "gpt-4o" OpenAI default (see LlmPoolEditor.vue's
	// identical fix).
	const PROVIDER_DEFAULTS = {
		Anthropic: { model: "claude-sonnet-4-6", baseUrl: "https://api.anthropic.com" },
		OpenAI: { model: "gpt-5.5", baseUrl: "https://api.openai.com/v1" },
		"Google Gemini": {
			model: "gemini-2.5-pro",
			baseUrl: "https://generativelanguage.googleapis.com",
		},
		Mistral: { model: "mistral-large-latest", baseUrl: "https://api.mistral.ai/v1" },
		Groq: { model: "llama-3.3-70b-versatile", baseUrl: "https://api.groq.com/openai/v1" },
		"Together AI": {
			model: "meta-llama/Llama-3.3-70B-Instruct-Turbo",
			baseUrl: "https://api.together.xyz/v1",
		},
		DeepSeek: { model: "deepseek-chat", baseUrl: "https://api.deepseek.com" },
		"Moonshot (Kimi)": { model: "kimi-k2.6", baseUrl: "https://api.moonshot.ai/v1" },
		"xAI Grok": { model: "grok-4.5", baseUrl: "https://api.x.ai/v1" },
		"GLM / Z.ai": { model: "glm-4.6", baseUrl: "https://api.z.ai/api/paas/v4" },
		"GLM / Z.ai (Coding Plan)": {
			model: "glm-4.6",
			baseUrl: "https://api.z.ai/api/coding/paas/v4",
		},
		OpenRouter: {
			model: "anthropic/claude-sonnet-4-6",
			baseUrl: "https://openrouter.ai/api/v1",
		},
		"Ollama (local)": { model: "llama3", baseUrl: "http://host.docker.internal:11434/v1" },
		"vLLM (local)": { model: "", baseUrl: "" },
		"OpenAI-Compatible": { model: "", baseUrl: "" },
	};
	const PROVIDERS = Object.keys(PROVIDER_DEFAULTS);
	// Stored pools may carry the provider *id* (e.g. "openai_compat" from presets /
	// admin normalization) rather than the dropdown *label* ("OpenAI-Compatible").
	// Map id -> label so the <select> selects the right option on load; a value that
	// is already a label passes through unchanged.
	const PROVIDER_LABEL_BY_ID = {
		anthropic: "Anthropic",
		openai: "OpenAI",
		google: "Google Gemini",
		mistral: "Mistral",
		groq: "Groq",
		together: "Together AI",
		deepseek: "DeepSeek",
		moonshot: "Moonshot (Kimi)",
		xai: "xAI Grok",
		zai: "GLM / Z.ai",
		zai_coding: "GLM / Z.ai (Coding Plan)",
		openrouter: "OpenRouter",
		ollama: "Ollama (local)",
		vllm: "vLLM (local)",
		openai_compat: "OpenAI-Compatible",
	};
	function providerLabel(v) {
		if (!v) return v;
		if (PROVIDER_DEFAULTS[v]) return v; // already a label
		return PROVIDER_LABEL_BY_ID[v] || v; // map id -> label (or keep unknown as-is)
	}

	// ---- model suggestions (Custom-mode <datalist>) -----------------------
	// The Custom rows bind the model field to a <datalist> so common IDs are
	// pickable while arbitrary IDs stay typeable (openai_compat / Ollama / shim
	// need custom model ids). Suggestions are sourced per provider:
	//   1) the already-loaded preset catalog (models grouped by vendor id), then
	//   2) the admin-managed catalog (apiKeyModels, from get_chat_ui_settings), then
	//   3) the provider's default model. Deduped, catalog-first.
	//
	// STATIC_MODEL_SUGGESTIONS and SUB_MODEL_SUGGESTIONS (the old hardcoded
	// per-provider datalists) are gone: a model added in the admin desk now
	// shows up here with no deploy. SUB_MODEL_SUGGESTIONS previously carried
	// only openai/google, so a tenant on a Grok or Kimi subscription saw no
	// suggestions at all on this page; subscriptionModels (already fetched
	// dynamically, see loadInitial) carries all four.
	//
	// Preset-catalog vendor ids -> provider dropdown label. The catalog uses
	// "gemini"; the dropdown label is "Google Gemini" (stored id "google"), so
	// alias both to the same label. Others fall through to providerLabel.
	function catalogVendorLabel(vid) {
		if (vid === "gemini") return "Google Gemini";
		return providerLabel(vid);
	}
	// The api-key-tier model preselected for a provider. Prefers the admin-managed
	// catalog's is_default flag; falls back to the PROVIDER_DEFAULTS literal when
	// the catalog has no default for this label (fetch failed, or no api-key rows
	// at all for it).
	function providerDefaultModel(label) {
		const rows = apiKeyModels[label] || [];
		const flagged = rows.find((m) => m.is_default);
		return (flagged && flagged.model_id) || (PROVIDER_DEFAULTS[label] || {}).model || "";
	}
	// Suggested model ids for an API-key row's provider (label or id).
	function modelSuggestionsForProvider(provider) {
		const label = providerLabel(provider || "");
		const out = [];
		const push = (id) => {
			if (id && out.indexOf(id) === -1) out.push(id);
		};
		(presetCatalog || []).forEach((e) =>
			(e.models || []).forEach((m) => {
				if (catalogVendorLabel(m.provider) === label) push(m.model);
			})
		);
		(apiKeyModels[label] || []).forEach((m) => push(m.model_id));
		push(providerDefaultModel(label));
		return out;
	}
	// Upstream id (openai/google/xai/kimi) -> the subscription-surface provider
	// LABEL subscriptionModels is keyed by (get_chat_ui_settings /
	// jarvis/_subscription_models.py). Mirrors LlmPoolEditor.vue's
	// UPSTREAM_OAUTH_PROVIDER so the two never drift.
	const UPSTREAM_TO_SUB_LABEL = {
		openai: "OpenAI",
		google: "Google Gemini",
		xai: "xAI Grok",
		kimi: "Kimi (Moonshot)",
	};
	// Suggested model ids for a subscription row's chosen upstream.
	function subModelSuggestions(upstream) {
		return subscriptionModels[UPSTREAM_TO_SUB_LABEL[upstream] || ""] || [];
	}
	// A <datalist> of model-id suggestions. Empty list → the bound input just
	// behaves as a plain free-text field.
	function renderModelDatalist(id, suggestions) {
		const opts = (suggestions || [])
			.map((m) => `<option value="${esc(m)}"></option>`)
			.join("");
		return `<datalist id="${id}">${opts}</datalist>`;
	}

	// Subscription states that allow LLM credential changes. The others all
	// imply "service is paused" so the form is read-only.
	const EDITABLE_STATES = new Set(["Active", "Cancelled"]);

	const $root = $(`
		<div class="ja">
		  <div class="ja-brand">
		    <div class="ja-logo">✦</div>
		    <div class="ja-brand-name">Jarvis</div>
		    <div class="ja-brand-tag">Your AI workspace for ERPNext.</div>
		    <div class="ja-brand-foot">Manage your plan, credentials, and billing here.</div>
		  </div>
		  <div class="ja-panel">
		    <div class="ja-body"></div>
		  </div>
		</div>`);
	const $bg = $(`<div class="ja-bg"></div>`).appendTo(page.main);
	$root.appendTo($bg);
	const $body = $root.find(".ja-body");

	// ---- state -------------------------------------------------------------
	let account = null; // payload from get_account
	let settingsLocal = null; // local Jarvis Settings LLM fields snapshot

	// Subscription providers + models (chat-subscription OAuth path).
	// Populated by loadInitial() via jarvis.chat.api.get_chat_ui_settings -
	// the canonical catalogue lives in jarvis/_subscription_models.py, so
	// the JS side is a pure consumer and never drifts from the Python /
	// security allowlist (jarvis.oauth.api validates against the same dict).
	// Until the fetch lands, the maps are empty and renderSubscriptionPanel
	// renders the loading placeholder. Punch-list "_SUBSCRIPTION_MODELS
	// duplicated 4-5 times" from the 2026-06-16 cross-repo review.
	let subscriptionModels = {};

	// Providers whose approval screen hands back a BARE authorization code
	// rather than redirecting to a callback URL. xAI Grok reaches this page:
	// provOptions is built from subscriptionModels above, which carries every
	// key of jarvis/_subscription_models.py including "xAI Grok". Telling that
	// customer to copy the address bar sends them hunting for a URL that never
	// appears. MUST match `code_only_paste` in jarvis/oauth/providers.py (that
	// flag is what makes the backend accept a bare code; this only steers copy).
	const CODE_ONLY_PASTE_PROVIDERS = ["xAI Grok"];
	const isCodeOnlyPaste = (p) => CODE_ONLY_PASTE_PROVIDERS.indexOf(p) !== -1;
	let defaultModels = {};
	// Api-key-tier suggestions (provider label -> [{model_id, label, is_default}]),
	// same get_chat_ui_settings response as subscriptionModels/defaultModels above.
	// Replaces the old hardcoded STATIC_MODEL_SUGGESTIONS; see modelSuggestionsForProvider.
	let apiKeyModels = {};
	// Preset failover ladders (jarvis.onboarding.get_preset_catalog). Fetched in
	// loadInitial alongside the pool config so the Preset tab is populated before
	// the user opens it. Empty on fetch failure → Preset tab shows a fallback line.
	let presetCatalog = [];

	// AI provider card state.
	// aiTab follows llm_auth_mode by default; can diverge briefly while
	// the user is exploring (e.g. on api_key but previewing subscription).
	// sub* holds in-flight paste-back state during sign-in.
	const ui = {
		aiTab: "api_key",
		subProvider: "OpenAI",
		// Populated from defaultModels[subProvider] once
		// loadInitial fetches the catalogue. Empty until then so a
		// stale ui.subModel can't sneak into a paste-back signin
		// before the canonical Python set has been confirmed.
		subModel: "",
		subNonce: null,
		subAuthorizeUrl: null, // shown to the customer in Screen 2
		subExpiresAt: null,
		// LLM SETUP mode (mirrors the onboarding wizard): "quick" | "preset" |
		// "custom". Seeded from the saved config in loadInitial so the editor
		// opens on the tab that matches what's stored.
		llmMode: "quick",
		selectedPreset: null, // Preset tab: chosen catalog key
		savedPreset: "", // preset key currently persisted (for the hint)
		presetKeys: {}, // Preset tab: vendor -> api_key (progressive)
		customRows: [], // Custom tab: ordered failover rows (see blankCustomRow)
	};

	// ---- helpers -----------------------------------------------------------
	// Copy text to clipboard with a graceful fallback for insecure contexts
	// (HTTP / non-localhost). Returns a Promise that resolves on success,
	// rejects on failure so callers can show honest feedback. Customers
	// reach /jarvis-account over LAN HTTP where navigator.clipboard is
	// undefined, so we must NOT call it unguarded.
	function copyTextWithFallback(text) {
		if (navigator.clipboard && window.isSecureContext) {
			return navigator.clipboard.writeText(text);
		}
		return new Promise((resolve, reject) => {
			const ta = document.createElement("textarea");
			ta.value = text;
			ta.style.position = "fixed";
			ta.style.left = "-9999px";
			ta.style.top = "0";
			document.body.appendChild(ta);
			ta.focus();
			ta.select();
			try {
				const ok = document.execCommand("copy");
				document.body.removeChild(ta);
				ok ? resolve() : reject(new Error("execCommand copy returned false"));
			} catch (e) {
				document.body.removeChild(ta);
				reject(e);
			}
		});
	}

	// ---- boot --------------------------------------------------------------
	loadInitial();

	function loadInitial() {
		$body.html(`<div class="ja-loading">Loading your account…</div>`);
		frappe
			.call({ method: "jarvis.selfhost.get_status" })
			.then((st) => {
				// Self-hosted benches have no admin signup, so is_onboarded
				// is false for them - render the self-host connection view
				// instead of bouncing to the onboarding wizard.
				if ((st && st.message && st.message.deployment_mode) === "Self-Hosted") {
					renderSelfHostAccount(st.message);
					return null;
				}

				// Managed tenants: account management now lives in the Jarvis SPA.
				// Redirect there unless ?billing=1 (the billing/upgrade flow is not
				// yet in the SPA — Phase 2). Self-hosted tenants above keep this page.
				// "/jarvis/account" is a retired route (the account page's content
				// moved into the SPA settings dialog, which has no URL deep-link
				// scheme to open a specific pane) - repoint to the SPA root.
				if (!new URLSearchParams(window.location.search).get("billing")) {
					window.location.replace("/jarvis/");
					return null;
				}

				return frappe.call({ method: "jarvis.account.is_onboarded" });
			})
			.then((r) => {
				if (r === null) return; // self-hosted view already rendered
				if (!r.message || !r.message.onboarded) {
					// Not onboarded - wizard owns this customer.
					window.location.assign("/app/jarvis-onboarding");
					return;
				}
				return Promise.all([
					frappe.call({ method: "jarvis.account.get_account" }),
					frappe.db.get_doc("Jarvis Settings"),
					frappe.call({ method: "jarvis.chat.api.get_chat_ui_settings" }),
					// Preset catalog + effective pool config for the full 3-mode
					// LLM editor (mirrors the onboarding wizard). Both fall back
					// gracefully so a failure never blanks the account page.
					frappe
						.call({ method: "jarvis.onboarding.get_preset_catalog" })
						.catch(() => ({ message: [] })),
					frappe
						.call({ method: "jarvis.onboarding.get_llm_config" })
						.catch(() => ({ message: {} })),
				]).then(([acc, settings, chatUi, catalog, llmCfg]) => {
					account = (acc && acc.message) || {};
					settingsLocal = settings || {};
					const cui = (chatUi && chatUi.message) || {};
					subscriptionModels = cui.subscription_models || {};
					defaultModels = cui.default_models || {};
					apiKeyModels = cui.api_key_models || {};
					presetCatalog = (catalog && catalog.message) || [];
					// First-paint seed: pick the canonical default for the
					// initial subProvider so render() doesn't show an empty
					// model dropdown before the user has touched anything.
					ui.subModel =
						defaultModels[ui.subProvider] ||
						(subscriptionModels[ui.subProvider] || [])[0] ||
						"";
					ui.aiTab =
						settingsLocal.llm_auth_mode === "oauth" ? "subscription" : "api_key";
					// In oauth mode, seed sub-state from the saved provider/model so
					// a Re-authorize click uses THIS customer's provider (e.g.
					// "Google Gemini") instead of the hardcoded default that the
					// api_key→subscription wizard path uses.
					// Carry-over guards: only adopt llm_provider if it's still a
					// known subscription provider, and only adopt llm_model if it
					// matches a codex-CLI model for that provider. A standard-API
					// model (e.g. "gpt-4o") going through codex auth makes
					// openclaw fail every chat turn with "No API key found".
					// jarvis.oauth.api._coerce_subscription_model server-side
					// catches this too; this is just so the dropdown + the
					// "Sign in with ..." button label stay consistent.
					if (settingsLocal.llm_auth_mode === "oauth") {
						if (
							settingsLocal.llm_provider &&
							subscriptionModels[settingsLocal.llm_provider]
						) {
							ui.subProvider = settingsLocal.llm_provider;
						}
						const valid = subscriptionModels[ui.subProvider] || [];
						if (settingsLocal.llm_model && valid.includes(settingsLocal.llm_model)) {
							ui.subModel = settingsLocal.llm_model;
						} else {
							ui.subModel = valid[0] || ui.subModel;
						}
					}
					seedLlmSetupFromConfig((llmCfg && llmCfg.message) || {});
					render();
				});
			})
			.catch((e) => {
				$body.html(`<div class="ja-err">Couldn't load account info.
					${esc(e.message || "")}
					<button class="ja-btn ja-btn-ghost" id="ja-retry">Retry</button></div>`);
				$body.find("#ja-retry").on("click", loadInitial);
			});
	}

	// ---- self-hosted connection view --------------------------------------
	function renderShChecks($container, result) {
		const checks = result.checks || [];
		const rows = checks
			.map(
				(c) =>
					`<div>${c.ok ? "✅" : "❌"} <b>${esc(c.check)}</b> — ${esc(
						c.detail || ""
					)}</div>`
			)
			.join("");
		const overall = result.ok
			? `<div style="color:var(--green-700,#15803d);font-weight:600">All required checks passed.</div>`
			: `<div style="color:var(--red-600,#b91c1c);font-weight:600">Some checks failed.</div>`;
		$container.html(overall + rows);
	}

	function renderSelfHostAccount(st) {
		const row = (k, v) =>
			`<div style="display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--border-color);font-size:13px"><span style="color:var(--text-muted)">${k}</span><b>${esc(
				v || "-"
			)}</b></div>`;
		$body.html(`
			<div class="ja-card">
			  <div class="ja-eyebrow">Connection</div>
			  <div class="ja-card-head" style="margin-bottom:14px">
			    <h2 class="ja-h">Self-hosted openclaw</h2>
			    <span class="ja-pill ja-pill-ok">Self-hosted</span>
			  </div>
			  <p class="ja-sub">Jarvis is connected to <b>your own</b> openclaw server. You manage openclaw and the LLM. Switch to Aerele-managed anytime.</p>
			  ${row("openclaw URL", st.agent_url)}
			  ${row("Last validated", st.validated_at)}
			  <div id="ja-sh-results" style="margin:12px 0;font-size:12.5px;line-height:1.7"></div>
			  <div class="ja-err" id="ja-sh-err"></div>
			  <div style="display:flex;gap:10px;margin-top:16px;flex-wrap:wrap">
			    <button class="ja-btn ja-btn-ghost" id="ja-sh-test">Test connection</button>
			    <button class="ja-btn ja-btn-ghost" id="ja-sh-recfg">Reconfigure</button>
			    <button class="ja-btn ja-btn-primary" id="ja-sh-managed">Switch to managed</button>
			  </div>
			</div>`);
		const $err = $body.find("#ja-sh-err");
		const $res = $body.find("#ja-sh-results");
		$body.find("#ja-sh-test").on("click", () => {
			$err.text("");
			$res.html(`<div style="color:var(--text-muted)">Testing…</div>`);
			frappe
				.call({
					method: "jarvis.selfhost.test_connection",
					args: { base_url: st.agent_url, token: "", deep: 0 },
				})
				.then((r) => renderShChecks($res, r.message || {}))
				.catch((e) => $err.text(e.message || "Test failed."));
		});
		$body.find("#ja-sh-recfg").on("click", () => openSelfHostReconfigure(st));
		$body.find("#ja-sh-managed").on("click", () => {
			frappe.confirm(
				__(
					"Switch back to Aerele-managed openclaw? This re-syncs the managed connection."
				),
				() =>
					frappe.call({ method: "jarvis.selfhost.switch_to_managed" }).then(() => {
						frappe.show_alert({
							message: __("Switched to managed."),
							indicator: "green",
						});
						loadInitial();
					})
			);
		});
	}

	function openSelfHostReconfigure(st) {
		const d = new frappe.ui.Dialog({
			title: __("Reconfigure self-hosted openclaw"),
			fields: [
				{
					fieldtype: "Data",
					fieldname: "base_url",
					label: __("openclaw URL"),
					reqd: 1,
					default: st.agent_url || "",
				},
				{ fieldtype: "Password", fieldname: "token", label: __("Gateway token"), reqd: 1 },
				{
					fieldtype: "Check",
					fieldname: "stream",
					label: __("Stream responses token-by-token"),
					default: st.stream === false ? 0 : 1,
					description: __("Off = full reply at once; use if a proxy buffers SSE."),
				},
				{ fieldtype: "Button", fieldname: "test_btn", label: __("Test connection") },
				{ fieldtype: "HTML", fieldname: "results" },
			],
			primary_action_label: __("Save"),
			primary_action(v) {
				d.disable_primary_action();
				frappe
					.call({
						method: "jarvis.selfhost.save_self_hosted",
						args: {
							base_url: v.base_url,
							token: v.token,
							deep: 0,
							stream: v.stream ? 1 : 0,
						},
					})
					.then((r) => {
						const m = r.message || {};
						if (m.ok) {
							d.hide();
							frappe.show_alert(
								{
									message: m.warning ? __(m.warning) : __("Saved."),
									indicator: m.warning ? "orange" : "green",
								},
								m.warning ? 15 : undefined
							);
							loadInitial();
						} else {
							renderShChecks(d.fields_dict.results.$wrapper, m.result || {});
							d.enable_primary_action();
						}
					})
					.catch(() => d.enable_primary_action());
			},
		});
		d.fields_dict.test_btn.$input.on("click", () => {
			const v = d.get_values(true);
			if (!v.base_url) {
				frappe.msgprint(__("Enter the openclaw URL."));
				return;
			}
			d.fields_dict.results.$wrapper.html(`<div class="text-muted">${__("Testing…")}</div>`);
			frappe
				.call({
					method: "jarvis.selfhost.test_connection",
					args: { base_url: v.base_url, token: v.token || "", deep: 0 },
				})
				.then((r) => renderShChecks(d.fields_dict.results.$wrapper, r.message || {}));
		});
		d.show();
	}

	function render() {
		const sub = account.subscription_status || "none";
		const editable = EDITABLE_STATES.has(sub);
		$body.html(`
			${renderPlanSection()}
			${renderAiProviderCard(editable)}
			${renderBillingSection()}
		`);
		bindAiProviderCard(editable);
		bindBilling();
	}

	// ---- AI provider card (tabbed: API key | Chat subscription) ----------
	// Segmented-control style - full-width container, sliding thumb between
	// the two halves, equal-width buttons. Click handler swaps only the
	// panel body so the slide animation isn't interrupted by a full
	// re-render of the tabs DOM.
	const ICON_KEY = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>`;
	const ICON_CHAT = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;

	// Full 3-mode LLM editor — visually + behaviourally identical to the
	// onboarding wizard's "Connect AI" step:
	//   Quick   → a single model, sent DIRECT (API key or Chat subscription).
	//   Preset  → a catalog failover ladder (single- or cross-vendor).
	//   Custom  → a hand-built failover pool; each row is an API key OR a
	//             pooled chat subscription (multiple OAuth accounts + rotation).
	// The card re-renders itself (rerenderAiCard) on structural changes so the
	// three modes share one native ja-* editor across onboarding and here.
	function renderAiProviderCard(editable) {
		const setupTabs = `
			<label class="ja-tabs-label">Setup</label>
			<div class="ja-tabs ja-tabs-3 ja-setup-tabs" role="tablist" data-active="${ui.llmMode}">
				<span class="ja-tabs-thumb" aria-hidden="true"></span>
				${["quick", "preset", "custom"]
					.map(
						(m) =>
							`<button type="button" class="ja-tab ${
								ui.llmMode === m ? "ja-tab-active" : ""
							}" data-llmmode="${m}" role="tab" aria-selected="${
								ui.llmMode === m
							}"><span>${m[0].toUpperCase() + m.slice(1)}</span></button>`
					)
					.join("")}
			</div>`;
		return `<div class="ja-card" id="ja-ai-card">
			<div class="ja-eyebrow">AI provider</div>
			<div class="ja-card-head" style="margin-bottom:18px">
				<h2 class="ja-h">How Jarvis talks to your LLM</h2>
				${renderModeBadge()}
			</div>
			${setupTabs}
			<div class="ja-llm-setup">${renderLlmMode(editable)}</div>
		</div>`;
	}

	// Direct/Proxy pill in the card head, derived via the shared pool logic
	// (window.jarvis_onboarding_llm.deriveMode). Quick is always Direct; a
	// preset or a ≥2-model pool is Proxy.
	function renderModeBadge() {
		const P = window.jarvis_onboarding_llm || {};
		let mode = "direct";
		if (ui.llmMode === "preset") {
			mode = ui.selectedPreset ? "proxy" : "direct";
		} else if (ui.llmMode === "custom") {
			const valid = (ui.customRows || []).filter(
				(r) =>
					r &&
					(r.credType === "subscription"
						? (r.model || "").trim()
						: (r.provider || "").trim() && (r.model || "").trim())
			);
			mode = P.deriveMode
				? P.deriveMode(valid, null)
				: valid.length > 1
				? "proxy"
				: "direct";
		}
		return mode === "proxy"
			? `<span class="ja-pill ja-pill-ok">🔀 Proxy (failover)</span>`
			: `<span class="ja-pill ja-pill-muted">⟶ Direct</span>`;
	}

	function renderLlmMode(editable) {
		if (ui.llmMode === "preset") return renderPresetMode(editable);
		if (ui.llmMode === "custom") return renderCustomMode(editable);
		return renderQuickMode(editable);
	}

	// QUICK — the single-model DIRECT editor (unchanged behaviour): API key |
	// Chat subscription auth-mode tabs, single-account paste-back OAuth,
	// save_llm_creds. Wrapped under the "Quick" setup tab.
	function renderQuickMode(editable) {
		const inApiKeyMode = settingsLocal.llm_auth_mode !== "oauth";
		const tabs = `
			<label class="ja-tabs-label">Authentication mode</label>
			<div class="ja-tabs ja-auth-tabs" role="tablist" data-active="${ui.aiTab}">
				<span class="ja-tabs-thumb" aria-hidden="true"></span>
				<button type="button" class="ja-tab ${ui.aiTab === "api_key" ? "ja-tab-active" : ""}"
					data-tab="api_key" role="tab" aria-selected="${ui.aiTab === "api_key"}">
					${ICON_KEY}<span>API key</span>
				</button>
				<button type="button" class="ja-tab ${ui.aiTab === "subscription" ? "ja-tab-active" : ""}"
					data-tab="subscription" role="tab" aria-selected="${ui.aiTab === "subscription"}">
					${ICON_CHAT}<span>Chat subscription</span>
				</button>
			</div>`;
		const body =
			ui.aiTab === "api_key"
				? renderApiKeyPanel(editable, inApiKeyMode)
				: renderSubscriptionPanel(editable, !inApiKeyMode);
		return `
			<p class="ja-sub" style="margin-bottom:14px">A single model, sent directly to the provider. Need multiple models with failover? Use <b>Preset</b> or <b>Custom</b>.</p>
			${tabs}
			<div class="ja-tab-body">${body}</div>`;
	}

	function renderApiKeyPanel(editable, isActiveMode) {
		const provider = settingsLocal.llm_provider || "Anthropic";
		const model = settingsLocal.llm_model || providerDefaultModel(provider);
		const base =
			settingsLocal.llm_base_url || (PROVIDER_DEFAULTS[provider] || {}).baseUrl || "";
		const sync = settingsLocal.last_sync_status || "";
		const dis = editable ? "" : "disabled";
		const sel = PROVIDERS.map(
			(p) =>
				`<option value="${esc(p)}" ${p === provider ? "selected" : ""}>${esc(p)}</option>`
		).join("");
		// Sprint-4 punch-list "frappe.confirm consent for destructive
		// subscription→api-key switch bypassable via Save": when the
		// customer is on the api_key tab while still in oauth mode, show
		// a danger-styled banner that names the connected account (so
		// they can tell exactly which session is about to be cut) and
		// makes the Save button danger-coloured. The actual destructive
		// confirm fires in bindLlm at click time via frappe.warn.
		const connectedEmail = settingsLocal.llm_oauth_account_email || "";
		const emailLine = connectedEmail
			? ` Currently connected as <b>${esc(connectedEmail)}</b>.`
			: "";
		const notice = !isActiveMode
			? `<div class="ja-banner ja-banner-danger">⚠ You're currently using a chat subscription.${emailLine} Saving credentials here will switch you to API-key mode and disconnect the subscription.</div>`
			: "";
		// Save button gets the danger variant when we're about to act
		// destructively. Keeps the primary variant for the normal case
		// (already in api_key mode and just updating the key / model).
		const saveBtnClass = !isActiveMode ? "ja-btn-danger" : "ja-btn-primary";
		const saveBtnLabel = !isActiveMode
			? "Disconnect &amp; switch to API key"
			: "Save credentials";
		return `
			<p class="ja-sub">Your API key is sent directly to the provider - Jarvis only relays prompts.</p>
			${notice}
			<div class="ja-row2">
				<div class="ja-field">
					<label>Provider</label>
					<select class="ja-input" id="ja-prov" ${dis}>${sel}</select>
				</div>
				<div class="ja-field">
					<label>Model</label>
					<input class="ja-input" id="ja-model" value="${esc(model)}" ${dis}>
				</div>
			</div>
			<div class="ja-row2">
				<div class="ja-field">
					<label>API Key</label>
					<div class="ja-pwd">
						<input class="ja-input" id="ja-key" type="password" placeholder="${
							settingsLocal.llm_api_key
								? "•••••••• (unchanged)"
								: "Enter your API key"
						}" ${dis}>
						<button type="button" class="ja-pwd-toggle" id="ja-key-eye" aria-label="Show key" ${dis}>
							<svg class="ja-eye-on" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>
							<svg class="ja-eye-off" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 19c-7 0-10-7-10-7a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 10 7 10 7a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
						</button>
					</div>
				</div>
				<div class="ja-field">
					<label>Base URL <span class="ja-hint-inline">(optional)</span></label>
					<input class="ja-input" id="ja-base" value="${esc(base)}" ${dis}>
				</div>
			</div>
			<div class="ja-actions">
				<button class="ja-btn ${saveBtnClass}" id="ja-llm-save" ${dis}>${saveBtnLabel}</button>
				<span class="ja-llm-status">${sync ? "Last sync: " + esc(sync) : ""}</span>
			</div>
			<div class="ja-err" id="ja-llm-err"></div>`;
	}

	function renderSubscriptionPanel(editable, isActiveMode) {
		// Screen 2 - authorize URL shown, awaiting paste-back. Wins over
		// Screen 3 so that Re-authorize (in oauth mode) actually swaps to
		// the paste-back UI. Without this, the isActiveMode short-circuit
		// re-renders Screen 3 and the new subAuthorizeUrl is invisible.
		if (ui.subAuthorizeUrl) {
			const minsLeft = Math.max(0, Math.floor((ui.subExpiresAt - Date.now()) / 60000));
			return `
				<p class="ja-sub"><strong>Step 1</strong> - Sign in with your ${esc(
					ui.subProvider
				)} account in a new tab.</p>
				<div class="ja-actions" style="margin-bottom:10px">
					<button class="ja-btn ja-btn-primary" id="ja-sub-open-url">Open Sign-in URL →</button>
				</div>
				<div class="ja-url-row" style="margin-bottom:18px">
					<code class="ja-url-text" id="ja-sub-url-text" title="${esc(ui.subAuthorizeUrl)}">${esc(
				ui.subAuthorizeUrl
			)}</code>
					<button type="button" class="ja-btn ja-btn-ghost ja-btn-small" id="ja-sub-copy-url" title="Copy URL">Copy</button>
				</div>
				${
					isCodeOnlyPaste(ui.subProvider)
						? `<p class="ja-sub"><strong>Step 2</strong> - After clicking Authorize, ${esc(
								ui.subProvider
						  )} shows you an <strong>authorization code</strong>. Copy that code and paste it here:</p>`
						: `<p class="ja-sub"><strong>Step 2</strong> - After clicking Authorize, your browser will show a page saying <em>"This site can't be reached."</em> <strong>That's expected.</strong> Copy the URL from your browser's address bar (it'll start with <code>http://localhost:1455/auth/callback?code=…</code>) and paste it here:</p>`
				}
				<div class="ja-field">
					<textarea class="ja-input" id="ja-sub-pasted-url" rows="3" placeholder="${
						isCodeOnlyPaste(ui.subProvider)
							? "Paste the code shown after you approve"
							: "Paste the URL from the error page here"
					}"></textarea>
				</div>
				<div class="ja-actions">
					<button class="ja-btn ja-btn-ghost" id="ja-sub-cancel">Cancel</button>
					<button class="ja-btn ja-btn-primary" id="ja-sub-submit">Submit →</button>
				</div>
				<div class="ja-hint" style="margin-top:14px;font-size:12px;color:var(--text-muted)">Link valid for ~${minsLeft} minute${
				minsLeft === 1 ? "" : "s"
			}.</div>
				<div class="ja-err" id="ja-sub-err"></div>`;
		}
		// Screen 3 - already connected (oauth mode, no in-flight Re-authorize).
		if (isActiveMode) {
			const provider = settingsLocal.llm_provider || "-";
			const model = settingsLocal.llm_model || "-";
			const email = settingsLocal.llm_oauth_account_email || "-";
			// frappe.datetime.comment_when returns a <span class="frappe-timestamp">
			// HTML widget (with hover tooltip). DON'T escape - let it render
			// as live HTML. Other fields are plain strings; keep esc() on them.
			const connectedAtHtml = settingsLocal.llm_oauth_connected_at
				? frappe.datetime.comment_when(settingsLocal.llm_oauth_connected_at)
				: "-";
			return `
				<p class="ja-sub">Refresh and account state live inside your Jarvis container. If chat starts failing, click Re-authorize to mint fresh tokens.</p>
				<table class="ja-kv">
					<tr><td>Account</td><td>${esc(email)}</td></tr>
					<tr><td>Provider</td><td>${esc(provider)}</td></tr>
					<tr><td>Model</td><td>${esc(model)}</td></tr>
					<tr><td>Connected</td><td>${connectedAtHtml}</td></tr>
				</table>
				<div class="ja-actions">
					<button class="ja-btn ja-btn-ghost" id="ja-sub-disconnect">Disconnect</button>
					<button class="ja-btn ja-btn-primary" id="ja-sub-reauth">Re-authorize</button>
				</div>`;
		}
		// Screen 1 - provider + model picker (not yet started)
		const provOptions = Object.keys(subscriptionModels)
			.map(
				(p) =>
					`<option value="${esc(p)}" ${p === ui.subProvider ? "selected" : ""}>${esc(
						p
					)}</option>`
			)
			.join("");
		const modelOptions = (subscriptionModels[ui.subProvider] || [])
			.map(
				(m) =>
					`<option value="${esc(m)}" ${m === ui.subModel ? "selected" : ""}>${esc(
						m
					)}</option>`
			)
			.join("");
		const dis = editable ? "" : "disabled";
		return `
			<p class="ja-sub">Sign in with your existing ChatGPT Plus / Pro or Gemini Advanced account - no developer key needed.</p>
			<div class="ja-row2">
				<div class="ja-field">
					<label>Provider</label>
					<select id="ja-sub-provider" class="ja-input" ${dis}>${provOptions}</select>
				</div>
				<div class="ja-field">
					<label>Default model</label>
					<select id="ja-sub-model" class="ja-input" ${dis}>${modelOptions}</select>
				</div>
			</div>
			<div class="ja-actions">
				<button class="ja-btn ja-btn-primary" id="ja-sub-signin" ${dis}>Sign in with ${esc(
			ui.subProvider
		)} →</button>
			</div>
			<div class="ja-err" id="ja-sub-err"></div>`;
	}

	function bindAiProviderCard(editable) {
		// SETUP tabs (Quick | Preset | Custom). Switching modes re-renders the
		// whole AI card — matches how the onboarding wizard re-renders its LLM
		// step on a setup-mode switch.
		$body.find(".ja-setup-tabs .ja-tab[data-llmmode]").on("click", function () {
			const m = $(this).data("llmmode");
			if (m === ui.llmMode) return;
			ui.llmMode = m;
			// Leaving Quick abandons any in-flight single-account paste-back.
			if (m !== "quick") cancelSubscriptionFlow();
			rerenderAiCard(editable);
		});
		bindLlmMode(editable);
	}

	// Re-render only the AI provider card (not the plan/billing sections) and
	// re-wire it. Used on every structural change inside the LLM editor.
	function rerenderAiCard(editable) {
		$body.find("#ja-ai-card").replaceWith(renderAiProviderCard(editable));
		bindAiProviderCard(editable);
	}

	function bindLlmMode(editable) {
		if (ui.llmMode === "preset") return bindPresetMode(editable);
		if (ui.llmMode === "custom") return bindCustomMode(editable);
		return bindQuickMode(editable);
	}

	function bindQuickMode(editable) {
		// Auth-mode tabs are scoped to .ja-auth-tabs so they don't collide
		// with the .ja-setup-tabs above them (both use .ja-tab).
		$body.find(".ja-auth-tabs .ja-tab[data-tab]").on("click", function () {
			const tab = $(this).data("tab");
			if (tab === ui.aiTab) return;
			handleTabSwitch(tab);
		});
		if (ui.aiTab === "api_key") {
			bindLlm(editable);
		} else {
			bindSubscriptionPanel(editable);
		}
	}

	function handleTabSwitch(targetTab) {
		// Sprint-4 punch-list "frappe.confirm consent for destructive
		// subscription→api-key switch bypassable via Save": the previous
		// shape fired a destructive frappe.confirm here at tab-switch
		// time, BUT bindLlm's save handler unconditionally called
		// save_llm_creds with auth_mode='api_key' regardless of how the
		// user got onto the API-key panel. A customer could cancel the
		// switch confirm, accept the panel preview, then click Save and
		// silently lose their OAuth connection.
		//
		// Now: tab switching is preview-only, no confirm. The destructive
		// consent gate moved to the Save click handler (bindLlm below),
		// uses frappe.warn for danger-styled UI, and shows the previously
		// connected email so the customer knows exactly what's about to
		// be disconnected.
		ui.aiTab = targetTab;
		if (targetTab !== "subscription") cancelSubscriptionFlow();

		// Drive the slide animation by updating data-active on the
		// persistent tabs node. Swap only the panel body (fade out → swap
		// → fade in) instead of re-rendering the whole card, so the slide
		// isn't interrupted by a DOM rebuild.
		const sub = account.subscription_status || "none";
		const editable = EDITABLE_STATES.has(sub);
		const $tabs = $body.find(".ja-auth-tabs");
		$tabs.attr("data-active", targetTab);
		$tabs.find(".ja-tab").each(function () {
			const isActive = $(this).data("tab") === targetTab;
			$(this).toggleClass("ja-tab-active", isActive).attr("aria-selected", isActive);
		});
		const inApiKeyMode = settingsLocal.llm_auth_mode !== "oauth";
		const newBody =
			targetTab === "api_key"
				? renderApiKeyPanel(editable, inApiKeyMode)
				: renderSubscriptionPanel(editable, !inApiKeyMode);
		const $panel = $body.find(".ja-tab-body");
		$panel.addClass("ja-tab-body-swap");
		setTimeout(() => {
			$panel.html(newBody);
			$panel.removeClass("ja-tab-body-swap");
			if (targetTab === "api_key") bindLlm(editable);
			else bindSubscriptionPanel(editable);
		}, 160);
	}

	function bindSubscriptionPanel(editable) {
		const isActiveMode = settingsLocal.llm_auth_mode === "oauth";
		// Screen 2 wiring must come BEFORE the Screen 3 short-circuit. When
		// Re-authorize is clicked in oauth mode, ui.subAuthorizeUrl gets set,
		// render() emits Screen 2 HTML, and we need to bind its open/copy/
		// cancel/submit handlers - the Screen 3 path returns early, so we
		// have to fall through here when subAuthorizeUrl is set.
		if (ui.subAuthorizeUrl) {
			bindSubscriptionScreen2();
			return;
		}
		if (isActiveMode) {
			$body.find("#ja-sub-disconnect").on("click", () => {
				frappe.confirm(
					__(
						"Disconnect the LLM subscription? Jarvis chat will stop working until you reconnect."
					),
					() => {
						frappe.call({ method: "jarvis.oauth.api.disconnect" }).then((r) => {
							const m = r.message || {};
							if (m.ok) {
								frappe.show_alert({
									message: __("Disconnected."),
									indicator: "orange",
								});
								loadInitial();
							} else {
								frappe.show_alert({
									message:
										(m.error && m.error.message) || __("Disconnect failed."),
									indicator: "red",
								});
							}
						});
					}
				);
			});
			$body.find("#ja-sub-reauth").on("click", () => {
				cancelSubscriptionFlow();
				startCodexSignin();
			});
			return;
		}
		if (!editable) return;
		// Screen 1 wiring
		$body.find("#ja-sub-provider").on("change", (e) => {
			ui.subProvider = e.target.value;
			ui.subModel = (subscriptionModels[ui.subProvider] || [])[0] || "";
			render();
		});
		$body.find("#ja-sub-model").on("change", (e) => {
			ui.subModel = e.target.value;
		});
		$body.find("#ja-sub-signin").on("click", startCodexSignin);
		// Screen 2 wiring - also reachable from Screen 1 -> startCodexSignin.
		bindSubscriptionScreen2();
	}

	function bindSubscriptionScreen2() {
		$body.find("#ja-sub-open-url").on("click", () => {
			if (ui.subAuthorizeUrl) {
				window.open(ui.subAuthorizeUrl, "_blank", "noopener,noreferrer");
			}
		});
		$body.find("#ja-sub-copy-url").on("click", function () {
			if (!ui.subAuthorizeUrl) return;
			const $btn = $(this);
			copyTextWithFallback(ui.subAuthorizeUrl)
				.then(() => {
					const orig = $btn.text();
					$btn.text("Copied ✓");
					setTimeout(() => $btn.text(orig), 1400);
					frappe.show_alert({ indicator: "green", message: __("Sign-in URL copied") });
				})
				.catch(() => {
					frappe.show_alert({
						indicator: "red",
						message: __("Could not copy - select the URL above and copy manually"),
					});
				});
		});
		$body.find("#ja-sub-cancel").on("click", () => {
			cancelSubscriptionFlow();
			render();
		});
		$body.find("#ja-sub-submit").on("click", submitPastedUrl);
	}

	function startCodexSignin() {
		// $err only exists on Screen 1 and Screen 2. When called from the
		// Screen 3 Re-authorize handler, $err is an empty jQuery set so
		// .text() is a no-op - errors would be silent. surfaceErr() falls
		// back to a frappe.show_alert toast when the in-form error div is
		// missing, so the customer always sees what went wrong.
		const $err = $body.find("#ja-sub-err");
		$err.text("");
		setBusy("#ja-sub-signin", true);
		frappe
			.call({
				method: "jarvis.oauth.api.begin_paste_signin",
				args: { provider: ui.subProvider, model: ui.subModel },
			})
			.then((r) => {
				setBusy("#ja-sub-signin", false);
				const m = r.message || {};
				if (!m.ok) {
					surfaceErr($err, (m.error && m.error.message) || "Couldn't start sign-in.");
					return;
				}
				ui.subNonce = m.data.nonce;
				ui.subAuthorizeUrl = m.data.authorize_url;
				ui.subExpiresAt = Date.now() + m.data.expires_in * 1000;
				render();
			})
			.catch((e) => {
				setBusy("#ja-sub-signin", false);
				surfaceErr($err, e.message || "Couldn't reach Jarvis.");
			});
	}

	function surfaceErr($err, message) {
		if ($err && $err.length) {
			$err.text(message);
		} else {
			frappe.show_alert({ indicator: "red", message: message });
		}
	}

	function submitPastedUrl() {
		const $err = $body.find("#ja-sub-err");
		$err.text("");
		const pasted = ($body.find("#ja-sub-pasted-url").val() || "").trim();
		if (!pasted) {
			$err.text(
				isCodeOnlyPaste(ui.subProvider)
					? "Paste the code you were shown first."
					: "Paste the URL from your browser's address bar first."
			);
			return;
		}
		setBusy("#ja-sub-submit", true);
		frappe
			.call({
				method: "jarvis.oauth.api.complete_paste_signin",
				args: { nonce: ui.subNonce, redirected_url: pasted },
			})
			.then((r) => {
				setBusy("#ja-sub-submit", false);
				const m = r.message || {};
				if (!m.ok) {
					const errCode = (m.error && m.error.code) || "";
					const errMsg = (m.error && m.error.message) || "Sign-in failed.";
					$err.text(`${errCode}: ${errMsg}`);
					if (errCode === "expired" || errCode === "unknown_nonce") {
						setTimeout(() => {
							cancelSubscriptionFlow();
							render();
						}, 2500);
					}
					return;
				}
				frappe.show_alert({
					message: "Connected to chat subscription.",
					indicator: "green",
				});
				cancelSubscriptionFlow();
				loadInitial();
			})
			.catch((e) => {
				setBusy("#ja-sub-submit", false);
				$err.text(e.message || "Couldn't reach Jarvis.");
			});
	}

	function cancelSubscriptionFlow() {
		ui.subNonce = null;
		ui.subAuthorizeUrl = null;
		ui.subExpiresAt = null;
	}

	// ======================================================================
	// LLM SETUP — PRESET + CUSTOM modes (proxy pool via save_llm_pool)
	// Mirrors jarvis_onboarding.js renderLlmPreset/renderLlmCustom and the
	// per-row API-key⇄Chat-subscription editor from frontend AiView.vue.
	// Shared pure logic comes from window.jarvis_onboarding_llm.* (loaded on
	// every desk page via the jarvis_onboarding_llm.bundle in hooks.py).
	// ======================================================================

	// Transient per-row state for the inline paste-back connect flow. Never
	// emitted on save.
	function blankConnect() {
		return {
			open: false,
			loading: false,
			error: "",
			nonce: "",
			authorizeUrl: "",
			pastedUrl: "",
		};
	}

	function blankCustomRow() {
		return {
			credType: "api_key",
			provider: PROVIDERS[0] || "Anthropic",
			model: "",
			apiKey: "",
			baseUrl: "",
			has_key: false,
			rotation: "sticky",
			upstream: "openai",
			accounts: [],
			connect: blankConnect(),
		};
	}

	// Build the editor's Preset/Custom state from the effective pool config
	// (jarvis.onboarding.get_llm_config). Subscription-account detail (labels,
	// upstream, account_ref, rotation) comes straight from get_llm_config's
	// model.accounts. It must NOT be sourced from settingsLocal.models[].accounts:
	// accounts used to be a grandchild Table (Jarvis Settings -> models[] ->
	// accounts[]) which Frappe never persists/loads, so those child rows carry
	// no accounts. The server now returns display-only accounts (no oauth_blob).
	function seedLlmSetupFromConfig(cfg) {
		const models = (cfg && cfg.models) || [];
		const preset = (cfg && cfg.preset) || "";
		ui.savedPreset = preset;
		ui.selectedPreset = preset || null;
		ui.presetKeys = {};
		ui.customRows = models.map((m) => {
			if ((m.credential_type || "api_key") === "subscription") {
				const accounts = (m.accounts || []).map((a) => ({
					upstream: a.upstream || "openai",
					account_ref: a.account_ref,
					label: a.label || a.account_ref,
					oauth_blob: "", // never returned by the server; reconnect to change
				}));
				return {
					credType: "subscription",
					provider: "",
					model: m.model || "",
					apiKey: "",
					baseUrl: "",
					has_key: !!m.has_key,
					rotation: m.rotation || "sticky",
					upstream: (accounts[0] && accounts[0].upstream) || "openai",
					accounts,
					connect: blankConnect(),
				};
			}
			return {
				credType: "api_key",
				provider: providerLabel(m.provider),
				model: m.model || "",
				apiKey: "",
				baseUrl: m.base_url || "",
				has_key: !!m.has_key,
				rotation: "sticky",
				upstream: "openai",
				accounts: [],
				connect: blankConnect(),
			};
		});
		// Open on the tab that matches what's stored: a preset → Preset; a
		// multi-model or subscription pool → Custom; otherwise the single-model
		// Quick editor (legacy llm_* mirror).
		if (preset) ui.llmMode = "preset";
		else if (
			models.length >= 2 ||
			models.some((m) => (m.credential_type || "api_key") === "subscription")
		)
			ui.llmMode = "custom";
		else ui.llmMode = "quick";
	}

	// ---- PRESET mode ------------------------------------------------------
	function renderPresetMode(editable) {
		const P = window.jarvis_onboarding_llm || {};
		if (!presetCatalog.length) {
			return `<p class="ja-sub" style="margin-top:8px">Couldn't load presets — use <b>Quick</b> or <b>Custom</b>.</p>`;
		}
		const dis = editable ? "" : "disabled";
		const entry = presetCatalog.find((e) => e.key === ui.selectedPreset) || null;
		const missing =
			entry && P.missingVendorKeys ? P.missingVendorKeys(entry, ui.presetKeys) : [];
		const saveDisabled = !editable || !entry || missing.length > 0 ? "disabled" : "";

		const cardGroup = (kind) =>
			presetCatalog
				.filter((e) => e.kind === kind)
				.map((e) => {
					const sel = ui.selectedPreset === e.key ? "ja-preset-selected" : "";
					const ladder = (e.models || [])
						.slice()
						.sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
						.map(
							(m, i) =>
								`<div class="ja-preset-model"><span class="ja-hint-inline">${
									i === 0 ? "Runs every turn" : "Backup " + i
								}</span> <b>${esc(m.model)}</b></div>`
						)
						.join("");
					return `<div class="ja-preset-card ${sel}" data-pkey="${esc(e.key)}">
					<div class="ja-preset-label">${esc(e.label)}</div>
					<div class="ja-hint-inline" style="display:block;margin-bottom:8px">${esc(e.blurb || "")}</div>
					${ladder}
				</div>`;
				})
				.join("");

		const singleVendorCards = cardGroup("single_vendor");
		const crossVendorCards = cardGroup("cross_vendor");

		let keyFieldsHtml = "";
		if (entry) {
			const vendors = P.uniqueVendors ? P.uniqueVendors(entry) : [];
			const storedNote =
				entry.key === ui.savedPreset
					? `<div class="ja-hint-inline" style="display:block;margin-bottom:10px">This preset is active. Keys are stored — re-enter every vendor's key to re-save the ladder.</div>`
					: "";
			keyFieldsHtml =
				storedNote +
				vendors
					.map(
						(v) => `
				<div class="ja-field">
					<label>${esc(v)} API key</label>
					<input type="password" class="ja-input ja-preset-key" data-vendor="${esc(v)}" value="${esc(
							ui.presetKeys[v] || ""
						)}" placeholder="sk-..." autocomplete="off" ${dis}>
				</div>`
					)
					.join("");
		}

		const sync = settingsLocal.last_sync_status || "";
		return `
			<p class="ja-sub" style="margin-bottom:14px">Pick a preset failover ladder. Keys are stored encrypted; only your agent container sees the plaintext.</p>
			<div class="ja-preset-scroll">
				${
					singleVendorCards
						? `<p class="ja-tabs-label" style="margin-top:8px">Single-vendor ladders</p><div class="ja-preset-cards">${singleVendorCards}</div>`
						: ""
				}
				${
					crossVendorCards
						? `<p class="ja-tabs-label" style="margin-top:14px">Cross-vendor presets</p><div class="ja-preset-cards">${crossVendorCards}</div>`
						: ""
				}
			</div>
			${entry ? `<div style="margin-top:18px">${keyFieldsHtml}</div>` : ""}
			<div class="ja-actions">
				<button class="ja-btn ja-btn-primary" id="ja-preset-save" ${saveDisabled}>Save configuration</button>
				<span class="ja-llm-status">${sync ? "Last sync: " + esc(sync) : ""}</span>
			</div>
			<div class="ja-err" id="ja-llm-err"></div>`;
	}

	function bindPresetMode(editable) {
		if (!editable) return;
		const P = window.jarvis_onboarding_llm || {};
		$body.find(".ja-preset-card").on("click", function () {
			ui.selectedPreset = $(this).data("pkey");
			ui.presetKeys = {}; // reset keys when switching presets
			rerenderAiCard(editable);
		});
		$body.find(".ja-preset-key").on("input", function () {
			const vendor = $(this).data("vendor");
			ui.presetKeys[vendor] = $(this).val();
			const e2 = presetCatalog.find((e) => e.key === ui.selectedPreset);
			const m2 = e2 && P.missingVendorKeys ? P.missingVendorKeys(e2, ui.presetKeys) : ["?"];
			$body.find("#ja-preset-save").prop("disabled", m2.length > 0);
		});
		$body.find("#ja-preset-save").on("click", () => savePreset(editable));
	}

	function savePreset(editable) {
		const P = window.jarvis_onboarding_llm || {};
		const $err = $body.find("#ja-llm-err");
		$err.text("");
		const entry = presetCatalog.find((e) => e.key === ui.selectedPreset);
		if (!entry || (P.missingVendorKeys && P.missingVendorKeys(entry, ui.presetKeys).length)) {
			$err.text(
				"Presets need every vendor's key. Enter all keys, or use Quick / Custom to finish with fewer keys."
			);
			return;
		}
		const models = P.presetToModels(entry, ui.presetKeys);
		const check = P.validatePool(models, entry.key);
		if (!check.ok) {
			$err.text(check.error);
			return;
		}
		setBusy("#ja-preset-save", true);
		frappe
			.call({
				method: "jarvis.onboarding.save_llm_pool",
				args: {
					models: JSON.stringify(models),
					preset: entry.key,
					routing_mode: "failover",
				},
			})
			.then((r) => afterPoolSave(r, "#ja-preset-save"))
			.catch((e) => {
				setBusy("#ja-preset-save", false);
				$err.text(e.message || "Couldn't save preset.");
			});
	}

	// ---- CUSTOM mode ------------------------------------------------------
	function renderCustomMode(editable) {
		const dis = editable ? "" : "disabled";
		const rows = ui.customRows || [];
		const rowsHtml = rows.map((r, i) => renderCustomRow(r, i, rows.length, dis)).join("");
		const sync = settingsLocal.last_sync_status || "";
		return `
			<p class="ja-sub" style="margin-bottom:14px">Build your own failover pool. The first model runs every turn; the rest are backups. Each row is an <b>API key</b> or a <b>Chat subscription</b> (multiple pooled accounts).</p>
			<div id="ja-custom-rows">${
				rowsHtml ||
				`<div class="ja-empty" style="padding:8px 0">No models yet — add one below.</div>`
			}</div>
			<div class="ja-actions" style="justify-content:flex-start;margin-top:4px">
				<button class="ja-btn ja-btn-ghost ja-btn-small" id="ja-custom-add" ${dis}>+ Add model</button>
			</div>
			<div class="ja-actions">
				<button class="ja-btn ja-btn-primary" id="ja-custom-save" ${dis}>Save configuration</button>
				<span class="ja-llm-status">${sync ? "Last sync: " + esc(sync) : ""}</span>
			</div>
			<div class="ja-err" id="ja-llm-err"></div>`;
	}

	function renderCustomRow(r, i, n, dis) {
		const credType = r.credType || "api_key";
		const head = `
			<div class="ja-crow-head">
				<div class="ja-cred-toggle">
					${["api_key", "subscription"]
						.map(
							(t) =>
								`<button type="button" class="ja-cred-btn ${
									credType === t ? "ja-cred-active" : ""
								}" data-i="${i}" data-cred="${t}" ${dis}>${
									t === "api_key" ? "API key" : "Chat subscription"
								}</button>`
						)
						.join("")}
				</div>
				<div class="ja-crow-move">
					<button type="button" class="ja-btn ja-btn-ghost ja-btn-small ja-row-up" data-i="${i}" title="Move up" ${
			i === 0 ? "disabled" : ""
		} ${dis}>↑</button>
					<button type="button" class="ja-btn ja-btn-ghost ja-btn-small ja-row-down" data-i="${i}" title="Move down" ${
			i === n - 1 ? "disabled" : ""
		} ${dis}>↓</button>
					<button type="button" class="ja-btn ja-btn-ghost ja-btn-small ja-row-rm" data-i="${i}" title="Remove" ${dis}>✕</button>
				</div>
			</div>`;
		let bodyHtml;
		if (credType === "subscription") {
			bodyHtml = renderCustomSubBody(r, i, dis);
		} else {
			const provOpts = PROVIDERS.map(
				(p) =>
					`<option value="${esc(p)}" ${p === (r.provider || "") ? "selected" : ""}>${esc(
						p
					)}</option>`
			).join("");
			const dlId = `ja-dl-model-${i}`;
			bodyHtml = `
				<div class="ja-crow-grid">
					<select class="ja-input ja-custom-prov" data-i="${i}" ${dis}>${provOpts}</select>
					<input type="text" list="${dlId}" class="ja-input ja-custom-model" data-i="${i}" value="${esc(
				r.model || ""
			)}" placeholder="model id" ${dis}>
					<input type="password" class="ja-input ja-custom-key" data-i="${i}" value="${esc(
				r.apiKey || ""
			)}" placeholder="${
				r.has_key ? "key set — re-enter to change" : "API key"
			}" autocomplete="off" ${dis}>
					<input type="text" class="ja-input ja-custom-base" data-i="${i}" value="${esc(
				r.baseUrl || ""
			)}" placeholder="base URL (optional)" ${dis}>
				</div>
				${renderModelDatalist(dlId, modelSuggestionsForProvider(r.provider || ""))}`;
		}
		const runsLabel = i === 0 ? "Runs every turn" : "Backup " + i;
		return `<div class="ja-crow" data-i="${i}"><div class="ja-crow-tag">${runsLabel}</div>${head}${bodyHtml}</div>`;
	}

	function renderCustomSubBody(r, i, dis) {
		const rotation = r.rotation || "sticky";
		const upstream = r.upstream || "openai";
		const rotOpts = [
			["sticky", "Sticky"],
			["round_robin", "Round robin"],
			["least_used", "Least used"],
		]
			.map(
				([v, l]) =>
					`<option value="${v}" ${rotation === v ? "selected" : ""}>${l}</option>`
			)
			.join("");
		const upOpts = [
			["openai", "OpenAI"],
			["google", "Google"],
		]
			.map(
				([v, l]) =>
					`<option value="${v}" ${upstream === v ? "selected" : ""}>${l}</option>`
			)
			.join("");
		const accounts = r.accounts || [];
		const accountsHtml = accounts.length
			? accounts
					.map(
						(a, ai) => `<div class="ja-acct">
					<span class="ja-acct-dot">connected</span>
					<span class="ja-acct-label">${esc(a.label || a.account_ref)}</span>
					<span class="ja-hint-inline">${esc(a.upstream || "")}</span>
					<button type="button" class="ja-acct-rm ja-row-acct-rm" data-i="${i}" data-ai="${ai}" title="Remove account" ${dis}>✕</button>
				</div>`
					)
					.join("")
			: `<div class="ja-hint-inline" style="display:block;margin:4px 0 8px">No accounts connected yet.</div>`;

		const c = r.connect || {};
		let connectHtml = "";
		if (c.open) {
			if (c.authorizeUrl) {
				connectHtml = `
					<div class="ja-connect">
						<p class="ja-hint-inline" style="display:block;margin-bottom:8px">Open the sign-in URL, authorize, then paste the redirected URL (it starts with <code>http://localhost:1455/…</code>).</p>
						<div class="ja-url-row" style="margin-bottom:8px">
							<code class="ja-url-text" title="${esc(c.authorizeUrl)}">${esc(c.authorizeUrl)}</code>
							<button type="button" class="ja-btn ja-btn-ghost ja-btn-small ja-conn-copy" data-i="${i}" title="Copy URL">Copy</button>
							<button type="button" class="ja-btn ja-btn-ghost ja-btn-small ja-conn-open" data-i="${i}">Open ↗</button>
						</div>
						<textarea class="ja-input ja-conn-url" data-i="${i}" rows="2" placeholder="Paste the URL after you sign in">${esc(
					c.pastedUrl || ""
				)}</textarea>
						<div class="ja-actions" style="margin-top:8px">
							<button type="button" class="ja-btn ja-btn-ghost ja-conn-cancel" data-i="${i}">Cancel</button>
							<button type="button" class="ja-btn ja-btn-primary ja-conn-finish" data-i="${i}" ${
					c.loading ? "disabled" : ""
				}>${c.loading ? "Connecting…" : "Connect"}</button>
						</div>
						${c.error ? `<div class="ja-err">${esc(c.error)}</div>` : ""}
					</div>`;
			} else {
				connectHtml = `<div class="ja-connect"><div class="ja-hint-inline">${
					c.error ? "" : "Starting sign-in…"
				}</div>${c.error ? `<div class="ja-err">${esc(c.error)}</div>` : ""}</div>`;
			}
		}
		const dlId = `ja-dl-submodel-${i}`;
		return `
			<div class="ja-crow-grid ja-crow-grid-sub">
				<input type="text" list="${dlId}" class="ja-input ja-custom-model" data-i="${i}" value="${esc(
			r.model || ""
		)}" placeholder="model id (e.g. gpt-5.5)" ${dis}>
				<select class="ja-input ja-custom-upstream" data-i="${i}" title="Upstream" ${dis}>${upOpts}</select>
				<select class="ja-input ja-custom-rotation" data-i="${i}" title="Account rotation" ${dis}>${rotOpts}</select>
			</div>
			${renderModelDatalist(dlId, subModelSuggestions(upstream))}
			<div class="ja-accts">${accountsHtml}</div>
			${connectHtml}
			<button type="button" class="ja-btn ja-btn-ghost ja-btn-small ja-conn-start" data-i="${i}" ${dis}>+ Connect account</button>`;
	}

	function bindCustomMode(editable) {
		if (!editable) return;
		const P = window.jarvis_onboarding_llm || {};
		// Credential-type toggle (API key ⇄ Chat subscription).
		$body.find(".ja-cred-btn").on("click", function () {
			const i = +$(this).data("i");
			const cred = $(this).data("cred");
			const row = ui.customRows[i];
			if (!row || row.credType === cred) return;
			row.credType = cred;
			if (cred === "subscription") {
				if (!row.rotation) row.rotation = "sticky";
				if (!row.upstream) row.upstream = "openai";
				if (!Array.isArray(row.accounts)) row.accounts = [];
				row.connect = blankConnect();
			}
			rerenderAiCard(editable);
		});
		// Reorder / remove rows.
		$body.find(".ja-row-up").on("click", function () {
			const i = +$(this).data("i");
			if (i > 0) {
				ui.customRows = P.reorder(ui.customRows, i, i - 1);
				rerenderAiCard(editable);
			}
		});
		$body.find(".ja-row-down").on("click", function () {
			const i = +$(this).data("i");
			if (i < ui.customRows.length - 1) {
				ui.customRows = P.reorder(ui.customRows, i, i + 1);
				rerenderAiCard(editable);
			}
		});
		$body.find(".ja-row-rm").on("click", function () {
			const i = +$(this).data("i");
			ui.customRows = ui.customRows.filter((_, idx) => idx !== i);
			rerenderAiCard(editable);
		});
		// API-key row fields. Provider change re-renders (autofill defaults +
		// mode badge); text inputs update state in place to preserve focus.
		$body.find(".ja-custom-prov").on("change", function () {
			const i = +$(this).data("i");
			const p = $(this).val();
			const d = PROVIDER_DEFAULTS[p] || {};
			const defaultModel = providerDefaultModel(p);
			const row = ui.customRows[i] || {};
			// A stored key (has_key) belongs to the OLD provider's key_ref - carry it
			// forward and a switch to Ollama/vLLM with no retyped key would still send
			// a blank api_key while has_key=true, hitting the exact "blank api_key"
			// rejection this whole editor exists to avoid for local providers.
			const patch = { provider: p, has_key: false, apiKey: "" };
			if (!(row.model || "").trim() && defaultModel) patch.model = defaultModel;
			if (!(row.baseUrl || "").trim() && d.baseUrl) patch.baseUrl = d.baseUrl;
			ui.customRows[i] = Object.assign({}, row, patch);
			rerenderAiCard(editable);
		});
		$body.find(".ja-custom-model").on("input", function () {
			const i = +$(this).data("i");
			if (ui.customRows[i]) ui.customRows[i].model = $(this).val();
		});
		$body.find(".ja-custom-key").on("input", function () {
			const i = +$(this).data("i");
			if (ui.customRows[i]) ui.customRows[i].apiKey = $(this).val();
		});
		$body.find(".ja-custom-base").on("input", function () {
			const i = +$(this).data("i");
			if (ui.customRows[i]) ui.customRows[i].baseUrl = $(this).val();
		});
		// Subscription row fields. Upstream change re-renders so the model
		// datalist suggestions follow the chosen upstream (openai/google).
		$body.find(".ja-custom-upstream").on("change", function () {
			const i = +$(this).data("i");
			if (ui.customRows[i]) ui.customRows[i].upstream = $(this).val();
			rerenderAiCard(editable);
		});
		$body.find(".ja-custom-rotation").on("change", function () {
			const i = +$(this).data("i");
			if (ui.customRows[i]) ui.customRows[i].rotation = $(this).val();
		});
		$body.find(".ja-row-acct-rm").on("click", function () {
			const i = +$(this).data("i");
			const ai = +$(this).data("ai");
			const row = ui.customRows[i];
			if (row) {
				row.accounts = (row.accounts || []).filter((_, j) => j !== ai);
				rerenderAiCard(editable);
			}
		});
		// Per-account connect flow (begin/complete_pool_account_signin).
		$body.find(".ja-conn-start").on("click", function () {
			startPoolConnect(+$(this).data("i"), editable);
		});
		$body.find(".ja-conn-open").on("click", function () {
			const i = +$(this).data("i");
			const url = (ui.customRows[i].connect || {}).authorizeUrl;
			if (url) window.open(url, "_blank", "noopener,noreferrer");
		});
		// Copy the authorize URL — same helper + "Copied ✓" feedback as the
		// Quick-mode subscription copy button (#ja-sub-copy-url).
		$body.find(".ja-conn-copy").on("click", function () {
			const i = +$(this).data("i");
			const url = (ui.customRows[i].connect || {}).authorizeUrl;
			if (!url) return;
			const $btn = $(this);
			copyTextWithFallback(url)
				.then(() => {
					const orig = $btn.text();
					$btn.text("Copied ✓");
					setTimeout(() => $btn.text(orig), 1400);
					frappe.show_alert({ indicator: "green", message: __("Sign-in URL copied") });
				})
				.catch(() => {
					frappe.show_alert({
						indicator: "red",
						message: __("Could not copy - select the URL above and copy manually"),
					});
				});
		});
		$body.find(".ja-conn-url").on("input", function () {
			const i = +$(this).data("i");
			if (ui.customRows[i].connect) ui.customRows[i].connect.pastedUrl = $(this).val();
		});
		$body.find(".ja-conn-cancel").on("click", function () {
			const i = +$(this).data("i");
			if (ui.customRows[i]) ui.customRows[i].connect = blankConnect();
			rerenderAiCard(editable);
		});
		$body.find(".ja-conn-finish").on("click", function () {
			finishPoolConnect(+$(this).data("i"), editable);
		});
		// Add row / save.
		$body.find("#ja-custom-add").on("click", function () {
			ui.customRows = ui.customRows.concat(blankCustomRow());
			rerenderAiCard(editable);
		});
		$body.find("#ja-custom-save").on("click", () => saveCustom(editable));
	}

	function startPoolConnect(i, editable) {
		const row = ui.customRows[i];
		if (!row) return;
		if (!(row.model || "").trim()) {
			row.connect = Object.assign(blankConnect(), {
				open: true,
				error: "Enter a model id before connecting an account.",
			});
			rerenderAiCard(editable);
			return;
		}
		row.connect = Object.assign(blankConnect(), { open: true, loading: true });
		rerenderAiCard(editable);
		const provider = row.upstream === "google" ? "Google" : "OpenAI";
		frappe
			.call({
				method: "jarvis.oauth.api.begin_pool_account_signin",
				args: { provider, model: row.model.trim() },
			})
			.then((r) => {
				const m = r.message || {};
				const cur = ui.customRows[i];
				if (!cur) return;
				if (!m.ok) {
					cur.connect = Object.assign(blankConnect(), {
						open: true,
						error: (m.error && m.error.message) || "Couldn't start sign-in.",
					});
				} else {
					cur.connect = Object.assign(blankConnect(), {
						open: true,
						loading: false,
						nonce: m.data.nonce,
						authorizeUrl: m.data.authorize_url,
					});
				}
				rerenderAiCard(editable);
			})
			.catch((e) => {
				const cur = ui.customRows[i];
				if (!cur) return;
				cur.connect = Object.assign(blankConnect(), {
					open: true,
					error: e.message || "Couldn't reach Jarvis.",
				});
				rerenderAiCard(editable);
			});
	}

	function finishPoolConnect(i, editable) {
		const row = ui.customRows[i];
		if (!row || !row.connect || !row.connect.nonce) return;
		const pasted = (row.connect.pastedUrl || "").trim();
		if (!pasted) {
			row.connect.error = "Paste the URL you were redirected to.";
			rerenderAiCard(editable);
			return;
		}
		row.connect.loading = true;
		row.connect.error = "";
		rerenderAiCard(editable);
		frappe
			.call({
				method: "jarvis.oauth.api.complete_pool_account_signin",
				args: { nonce: row.connect.nonce, redirected_url: pasted },
			})
			.then((r) => {
				const m = r.message || {};
				const cur = ui.customRows[i];
				if (!cur) return;
				if (!m.ok) {
					cur.connect.loading = false;
					const code = m.error && m.error.code ? m.error.code + ": " : "";
					cur.connect.error = code + ((m.error && m.error.message) || "Sign-in failed.");
					rerenderAiCard(editable);
					return;
				}
				const d = m.data || {};
				if (!Array.isArray(cur.accounts)) cur.accounts = [];
				cur.accounts.push({
					upstream: cur.upstream || "openai",
					account_ref: d.account_ref,
					label: d.label || d.account_email || d.account_ref,
					oauth_blob: d.oauth_blob || "",
				});
				cur.connect = blankConnect();
				frappe.show_alert({ message: __("Account connected."), indicator: "green" });
				rerenderAiCard(editable);
			})
			.catch((e) => {
				const cur = ui.customRows[i];
				if (!cur) return;
				cur.connect.loading = false;
				cur.connect.error = e.message || "Couldn't reach Jarvis.";
				rerenderAiCard(editable);
			});
	}

	// Emit the per-row backend shape save_llm_pool expects: API-key rows carry
	// {provider, model, api_key, base_url, order}; subscription rows carry
	// {model, order, subscription:{rotation, accounts:[{upstream, account_ref,
	// label, oauth_blob}]}}. Matches AiView.vue's save().
	function buildCustomSaveModels() {
		return (ui.customRows || []).map((r, i) => {
			if (r.credType === "subscription") {
				return {
					model: (r.model || "").trim(),
					order: i,
					subscription: {
						rotation: r.rotation || "sticky",
						accounts: (r.accounts || []).map((a) => ({
							upstream: a.upstream || "openai",
							account_ref: a.account_ref,
							label: a.label,
							oauth_blob: a.oauth_blob || "",
						})),
					},
				};
			}
			const P = window.jarvis_onboarding_llm || {};
			const m = {
				provider: (r.provider || "").trim(),
				model: (r.model || "").trim(),
				// Local providers (Ollama, vLLM) take no key; effectiveApiKey fills a
				// harmless placeholder instead of an empty string so this still clears
				// the backend's blank-api_key guard, matching validatePool above.
				api_key: P.effectiveApiKey
					? P.effectiveApiKey(r.provider, r.apiKey, r.has_key)
					: (r.apiKey || "").trim(),
				order: i,
			};
			const b = (r.baseUrl || "").trim();
			if (b) m.base_url = b;
			return m;
		});
	}

	function saveCustom(editable) {
		const P = window.jarvis_onboarding_llm || {};
		const $err = $body.find("#ja-llm-err");
		$err.text("");
		const models = buildCustomSaveModels();
		const check = P.validatePool(models, null);
		if (!check.ok) {
			$err.text(check.error);
			return;
		}
		setBusy("#ja-custom-save", true);
		frappe
			.call({
				method: "jarvis.onboarding.save_llm_pool",
				args: { models: JSON.stringify(models), preset: null, routing_mode: "failover" },
			})
			.then((r) => afterPoolSave(r, "#ja-custom-save"))
			.catch((e) => {
				setBusy("#ja-custom-save", false);
				$err.text(e.message || "Couldn't save models.");
			});
	}

	// Shared post-save handler for the pool paths (Preset + Custom). Polls the
	// async provisioning status, then reloads so the editor reflects the saved
	// pool (and stays on the matching tab).
	function afterPoolSave(r, btnSel) {
		const m = r.message || {};
		const status = (m.last_sync_status || "").trim();
		settingsLocal.last_sync_status = status;
		if (status.startsWith("pending:")) {
			$body
				.find(".ja-llm-status")
				.text("Provisioning... " + status.replace(/^pending:\s*/i, ""));
			pollPoolSyncStatus(btnSel);
		} else {
			setBusy(btnSel, false);
			frappe.show_alert({ message: __("LLM configuration saved."), indicator: "green" });
			loadInitial();
		}
	}

	function pollPoolSyncStatus(btnSel) {
		const startedAt = Date.now();
		const TIMEOUT_MS = 150 * 1000;
		const tick = () => {
			frappe
				.call({ method: "jarvis.onboarding.get_llm_sync_status" })
				.then((r) => {
					const m = r.message || {};
					const status = (m.last_sync_status || "").trim();
					settingsLocal.last_sync_status = status;
					if (!m.pending) {
						setBusy(btnSel, false);
						frappe.show_alert({
							message: __("LLM configuration saved."),
							indicator: "green",
						});
						loadInitial();
						return;
					}
					if (Date.now() - startedAt > TIMEOUT_MS) {
						setBusy(btnSel, false);
						$body
							.find(".ja-llm-status")
							.text("Still provisioning — check back in a moment.");
						return;
					}
					$body
						.find(".ja-llm-status")
						.text("Provisioning... " + status.replace(/^pending:\s*/i, ""));
					setTimeout(tick, 2500);
				})
				.catch(() => {
					if (Date.now() - startedAt > TIMEOUT_MS) {
						setBusy(btnSel, false);
						$body
							.find(".ja-llm-status")
							.text("Lost contact while provisioning — refresh later.");
						return;
					}
					setTimeout(tick, 2500);
				});
		};
		setTimeout(tick, 2500);
	}

	// ---- plan & validity section ------------------------------------------
	function renderPlanSection() {
		const sub = account.subscription_status || "none";
		const plan = account.plan || {};
		if (!plan || !plan.name) {
			return `<div class="ja-card">
				<h2 class="ja-h">No plan</h2>
				<p class="ja-sub">You don't have an active subscription yet.</p>
			</div>`;
		}
		const banner = renderPlanBanner(sub);
		return `<div class="ja-card">
			<div class="ja-card-head">
				<div>
					<div class="ja-eyebrow">Plan</div>
					<h2 class="ja-h">${esc(plan.plan_name || plan.name)}</h2>
					<div class="ja-plan-meta">${inr(plan.price_inr)} <span class="jo-plan-cycle">${cycleLabel(
			plan.billing_cycle
		)}</span></div>
				</div>
				${renderStatusPill(sub)}
			</div>
			${banner}
		</div>`;
	}

	function renderStatusPill(sub) {
		const tone =
			{
				Active: "ok",
				Cancelled: "warn",
				"Past Due": "warn",
				Expired: "bad",
				"Pending Payment": "warn",
			}[sub] || "muted";
		return `<span class="ja-pill ja-pill-${tone}">${esc(sub)}</span>`;
	}

	function renderPlanBanner(sub) {
		const end = account.current_period_end
			? frappe.datetime.str_to_user(account.current_period_end)
			: "";
		const days = account.days_remaining || 0;
		switch (sub) {
			case "Active":
				return `<div class="ja-banner ja-banner-ok">Renews on <b>${esc(
					end
				)}</b> · ${days} day${days === 1 ? "" : "s"} left.</div>`;
			case "Cancelled":
				return `<div class="ja-banner ja-banner-warn">Cancelled · runs until <b>${esc(
					end
				)}</b>.</div>`;
			case "Past Due":
				return `<div class="ja-banner ja-banner-warn">Plan past due - expired on <b>${esc(
					end
				)}</b>. Reactivate to resume service.</div>`;
			case "Expired":
				return `<div class="ja-banner ja-banner-bad">Plan expired on <b>${esc(
					end
				)}</b>. Reactivate to resume service.</div>`;
			case "Pending Payment":
				return `<div class="ja-banner ja-banner-warn">Payment incomplete · finish to activate.</div>`;
			default:
				return "";
		}
	}

	// ---- LLM credentials binding (form rendered by renderApiKeyPanel) ----
	function bindLlm(editable) {
		if (!editable) return;
		$body.find("#ja-key-eye").on("click", function () {
			const $btn = $(this);
			$btn.toggleClass("shown");
			const shown = $btn.hasClass("shown");
			$body.find("#ja-key").attr("type", shown ? "text" : "password");
			$btn.attr("aria-label", shown ? "Hide key" : "Show key");
		});
		const $prov = $body.find("#ja-prov");
		$prov.on("change", () => {
			const p = $prov.val();
			const d = PROVIDER_DEFAULTS[p] || {};
			$body.find("#ja-model").val(providerDefaultModel(p));
			$body.find("#ja-base").val(d.baseUrl || "");
		});
		$body.find("#ja-llm-save").on("click", () => {
			const provider = $prov.val();
			const model = $body.find("#ja-model").val().trim();
			const key = $body.find("#ja-key").val().trim();
			const base = $body.find("#ja-base").val().trim();
			if (!provider || !model) {
				return $body.find("#ja-llm-err").text("Provider and model are required.");
			}
			if (!key && !settingsLocal.llm_api_key) {
				return $body.find("#ja-llm-err").text("API key is required.");
			}
			$body.find("#ja-llm-err").text("");

			// Sprint-4 punch-list "frappe.confirm consent for destructive
			// subscription→api-key switch bypassable via Save": when the
			// customer is currently in oauth mode and about to switch to
			// api_key, fire frappe.warn here at click time (the
			// authoritative destructive gate). frappe.warn is the
			// danger-styled variant of frappe.confirm - red Proceed
			// button + warning iconography. Names the connected account
			// so the customer can see exactly which session is about to
			// be cut. If the customer cancels, no save happens; the
			// previous shape moved the confirm to tab-switch time and
			// the Save handler ran unconditionally - a click-cancel-on-
			// tab-switch then click-Save sequence silently dropped the
			// subscription.
			const wasOauth = settingsLocal.llm_auth_mode === "oauth";
			const proceedSave = () => doSaveApiKeyCreds({ provider, model, key, base, wasOauth });
			if (wasOauth) {
				const connectedEmail = settingsLocal.llm_oauth_account_email || "";
				const emailLine = connectedEmail
					? `<p>This will disconnect your chat subscription connected as <b>${frappe.utils.escape_html(
							connectedEmail
					  )}</b>.</p>`
					: "<p>This will disconnect your chat subscription.</p>";
				frappe.warn(
					__("Disconnect chat subscription?"),
					emailLine +
						"<p>Your saved API key will be used for chat instead. " +
						"Reconnecting later means signing in again from the Subscription tab.</p>",
					proceedSave,
					__("Disconnect and switch")
				);
				return;
			}
			proceedSave();
		});
	}

	function doSaveApiKeyCreds({ provider, model, key, base, wasOauth }) {
		setBusy("#ja-llm-save", true);
		frappe
			.call({
				method: "jarvis.onboarding.save_llm_creds",
				args: {
					provider,
					model,
					api_key: key || "",
					base_url: base,
					auth_mode: "api_key",
				},
			})
			.then((r) => {
				const status = (r.message && r.message.last_sync_status) || "";
				// wasOauth was captured in the click handler before frappe.call
				// fired and threaded in via doSaveApiKeyCreds's destructured
				// param so the post-save branch below correctly knows whether
				// to re-render the card.
				settingsLocal.llm_provider = provider;
				settingsLocal.llm_model = model;
				settingsLocal.llm_base_url = base;
				settingsLocal.llm_api_key = key || settingsLocal.llm_api_key;
				settingsLocal.llm_auth_mode = "api_key";
				settingsLocal.last_sync_status = status;
				$body.find("#ja-key").val("");
				// 2026-06-09: on_update is async - the save returns
				// "pending: ..." while the admin restart runs in the
				// background. Poll until the status flips to ok/failed.
				if (status.startsWith("pending:")) {
					$body
						.find(".ja-llm-status")
						.text("Provisioning... " + status.replace(/^pending:\s*/i, ""));
					pollLlmSyncStatus({ wasOauth });
				} else {
					setBusy("#ja-llm-save", false);
					$body.find(".ja-llm-status").text(status ? "Last sync: " + status : "Saved.");
					// If we just flipped out of oauth mode, re-render so the
					// "current mode" pill and notice banner update correctly.
					if (wasOauth) render();
				}
			})
			.catch((e) => {
				setBusy("#ja-llm-save", false);
				$body.find("#ja-llm-err").text(e.message || "Save failed.");
			});
	}

	function pollLlmSyncStatus({ wasOauth }) {
		// On_update is async - poll get_llm_sync_status until the status
		// flips from "pending: ..." to "ok ..." / "failed: ...". Admin's
		// healthz timeout is 60s; we cap at 150s for slack.
		const startedAt = Date.now();
		const TIMEOUT_MS = 150 * 1000;
		const tick = () => {
			frappe
				.call({ method: "jarvis.onboarding.get_llm_sync_status" })
				.then((r) => {
					const m = r.message || {};
					const status = (m.last_sync_status || "").trim();
					settingsLocal.last_sync_status = status;
					if (!m.pending) {
						setBusy("#ja-llm-save", false);
						$body
							.find(".ja-llm-status")
							.text(status ? "Last sync: " + status : "Saved.");
						if (wasOauth) render();
						return;
					}
					if (Date.now() - startedAt > TIMEOUT_MS) {
						setBusy("#ja-llm-save", false);
						$body
							.find(".ja-llm-status")
							.text("Still provisioning - check back in a moment.");
						return;
					}
					$body
						.find(".ja-llm-status")
						.text("Provisioning... " + status.replace(/^pending:\s*/i, ""));
					setTimeout(tick, 2500);
				})
				.catch(() => {
					if (Date.now() - startedAt > TIMEOUT_MS) {
						setBusy("#ja-llm-save", false);
						$body
							.find(".ja-llm-status")
							.text("Lost contact while provisioning - refresh later.");
						return;
					}
					setTimeout(tick, 2500);
				});
		};
		setTimeout(tick, 2500);
	}

	// ---- billing section --------------------------------------------------
	function renderBillingSection() {
		const sub = account.subscription_status || "none";
		const upgrade = (account.upgrade_plans || []).length > 0;
		const ctaPrimary = primaryCta(sub);
		const ctaSecondary = secondaryCta(sub, upgrade);
		return `<div class="ja-card">
			<div class="ja-eyebrow">Billing</div>
			<h2 class="ja-h">${esc(ctaPrimary.heading)}</h2>
			<p class="ja-sub">${esc(ctaPrimary.subtitle)}</p>
			<div class="ja-actions">
				${
					ctaPrimary && ctaPrimary.action
						? `<button class="ja-btn ja-btn-primary" id="ja-cta-primary" data-action="${
								ctaPrimary.action
						  }">${esc(ctaPrimary.label)}</button>`
						: ""
				}
				${
					ctaSecondary
						? `<button class="ja-btn ja-btn-ghost" id="ja-cta-secondary" data-action="${
								ctaSecondary.action
						  }">${esc(ctaSecondary.label)}</button>`
						: ""
				}
				${
					// Autopay is off and re-armable. Without this the customer is
					// told to "set up payment again" with nothing to click:
					// releasing a mandate is terminal at Razorpay, so cancel->resume
					// (and one-shot renew) can never restore auto-renewal by itself.
					account.can_reauthorize
						? `<button class="ja-btn ja-btn-ghost" id="ja-cta-reauth" data-action="reauth">Set up auto-renewal</button>`
						: ""
				}
				${
					// A cheaper plan is available and none is scheduled yet. Applies
					// at the next cycle, so it's a quiet ghost action, not a headline.
					(account.downgrade_plans || []).length && !account.scheduled_plan
						? `<button class="ja-btn ja-btn-ghost" id="ja-cta-downgrade" data-action="downgrade">Switch to a smaller plan</button>`
						: ""
				}
			</div>
			${renderScheduledSwitch()}
			<div class="ja-err" id="ja-billing-err"></div>
		</div>`;
	}

	// Both shapes are undoable, but they cost differently: an Annual switch is a
	// plain flag we clear, while a Monthly one already moved the mandate to the
	// cheaper plan (the old mandate is released and terminal at Razorpay), so
	// keeping the current plan means authorising a replacement at its price.
	// Say which one they're in rather than springing the Checkout on them.
	function renderScheduledSwitch() {
		if (!account.scheduled_plan) return "";
		const name = esc(account.scheduled_plan_name || account.scheduled_plan);
		const on = account.scheduled_plan_on
			? " on " + esc(String(account.scheduled_plan_on).split(" ")[0])
			: "";
		const cost = account.scheduled_downgrade_revocable
			? ""
			: " Keeping it means setting up auto-renewal again at your current price.";
		return `<p class="ja-sub" style="margin-top:8px">Switching to ${name}${on}. You keep your current plan until then.${cost}</p><button class="ja-btn ja-btn-ghost" id="ja-cta-keep" data-action="keep-plan">Keep current plan</button>`;
	}

	// Only suppress renewal when the server explicitly refuses it. An older
	// admin that does not send can_renew keeps the previous behaviour, so a
	// bench deployed ahead of admin never hides a CTA that would have worked.
	function canRenew() {
		return account.can_renew !== false;
	}

	// The autopay wording names the actual renewal date, which the customer
	// otherwise has to hunt for in the plan card above.
	function autoRenewNote() {
		const end = account.current_period_end
			? String(frappe.datetime.str_to_user(account.current_period_end)).split(" ")[0]
			: "";
		return end
			? `Your plan renews automatically on ${end} using your saved payment method. There's nothing to pay now.`
			: "Your plan renews automatically using your saved payment method. There's nothing to pay now.";
	}

	function primaryCta(sub) {
		const upgrade = (account.upgrade_plans || []).length > 0;
		switch (sub) {
			case "Active":
				if (upgrade)
					return {
						action: "upgrade",
						label: "Upgrade plan",
						heading: "Want more capacity?",
						subtitle:
							"Move to a higher plan - you only pay the prorated difference for the remaining period.",
					};
				if (canRenew())
					return {
						action: "renew",
						label: "Renew now",
						heading: "Renew early",
						subtitle: "Add another billing cycle to your current plan.",
					};
				// renew() refuses a manual order alongside a live mandate (double
				// charge). Nothing to sell here - say when it renews instead.
				return {
					action: "",
					label: "",
					heading: "Auto-renewal is on",
					subtitle: autoRenewNote(),
				};
			case "Cancelled":
				return {
					action: "renew",
					label: "Resume / Renew",
					heading: "Resume your plan",
					subtitle: "Pay now to keep service running past the current period end.",
				};
			case "Past Due":
				if (!canRenew())
					// The mandate is live and Razorpay retries on its own schedule;
					// a manual payment here would stack on top of that retry.
					return {
						action: "",
						heading: "Payment retrying",
						subtitle:
							"We'll charge your saved payment method again automatically. No manual payment is needed.",
					};
			// falls through - a dead mandate means this is a normal reactivation
			case "Expired":
				return {
					action: "renew",
					label: "Reactivate",
					heading: "Reactivate your plan",
					subtitle: "Pay now to restore service and bring your container back online.",
				};
			case "Pending Payment":
				return {
					action: "renew",
					label: "Complete payment",
					heading: "Finish signing up",
					subtitle: "Complete payment to activate your plan.",
				};
			default:
				return { action: "renew", label: "Subscribe", heading: "Subscribe", subtitle: "" };
		}
	}

	function secondaryCta(sub, hasUpgrade) {
		// Same rule as the primary: never offer a renewal the server refuses.
		if (sub === "Active" && hasUpgrade && canRenew())
			return { action: "renew", label: "Renew now" };
		if (sub === "Cancelled" && hasUpgrade) return { action: "upgrade", label: "Upgrade plan" };
		return null;
	}

	function bindBilling() {
		$body
			.find(
				"#ja-cta-primary, #ja-cta-secondary, #ja-cta-reauth, #ja-cta-downgrade, #ja-cta-keep"
			)
			.on("click", function () {
				const action = $(this).data("action");
				if (action === "upgrade") return openUpgradeModal();
				if (action === "renew") return startRenew();
				if (action === "reauth") return startReauthorize();
				if (action === "downgrade") return openDowngradeModal();
				if (action === "keep-plan") return cancelScheduledSwitch();
			});
	}

	function cancelScheduledSwitch() {
		setBusy("#ja-cta-keep", true);
		frappe
			.call({ method: "jarvis.account.cancel_scheduled_downgrade" })
			.then((r) => {
				setBusy("#ja-cta-keep", false);
				const data = (r.message && r.message.data) || r.message || {};
				if (data.razorpay_subscription_id) {
					// Monthly: the cheaper mandate is already dropped, so re-arm the
					// current plan in the same step (mandate-only, nothing charged).
					return openCheckout(data, /*upgrade=*/ false);
				}
				// loadInitial() re-renders the card, which drops the busy button.
				frappe.show_alert({
					message: __("Plan switch cancelled - you're staying on your current plan."),
					indicator: "green",
				});
				loadInitial();
			})
			.catch((e) => {
				setBusy("#ja-cta-keep", false);
				$body
					.find("#ja-billing-err")
					.text(e.message || "Couldn't cancel the plan switch.");
			});
	}

	// What the plan actually provisions - the real trade-off in a downgrade.
	// A plan stores 0/NULL when a limit was never configured (the fleet agent
	// rejects cpu_limit <= 0, so 0 is "unset", never "unlimited"), so say
	// nothing rather than advertise "0 vCPU".
	function planCapacity(p) {
		const parts = [];
		const cpu = parseFloat(p.cpu_limit);
		if (cpu > 0) parts.push(`${+cpu.toFixed(2)} vCPU`);
		const mb = Number(p.memory_limit_mb);
		if (mb > 0) parts.push(mb >= 1024 ? `${+(mb / 1024).toFixed(1)} GB RAM` : `${mb} MB RAM`);
		return parts.join(" · ");
	}

	// ---- downgrade (applies next cycle) -----------------------------------
	function openDowngradeModal() {
		const plans = account.downgrade_plans || [];
		const cards = plans
			.map((p) => {
				const cap = planCapacity(p);
				const capLine = cap ? `<div class="ja-dn-cap">${esc(cap)}</div>` : "";
				return `
			<div class="ja-dn-row" data-plan="${esc(p.name)}">
				<div class="ja-dn-info">
					<div class="ja-dn-name">${esc(p.plan_name || p.name)}</div>
					${capLine}
				</div>
				<div class="ja-dn-price">${inr(p.price_inr)}<span class="jo-plan-cycle">${cycleLabel(
					p.billing_cycle
				)}</span></div>
				<button class="ja-btn ja-btn-ghost ja-dn-pick" data-plan="${esc(p.name)}">Switch</button>
			</div>`;
			})
			.join("");
		// The cutover date is shared by every option, so state it once here
		// rather than repeating it on each row.
		const endNice = account.current_period_end
			? String(frappe.datetime.str_to_user(account.current_period_end)).split(" ")[0]
			: "";
		const until = endNice ? `<b>${esc(endNice)}</b>` : "your next billing cycle";
		// Monthly switches open a Checkout to authorise auto-renewal at the new
		// price, so say that up front rather than springing a payment sheet.
		const monthly =
			String((account.plan || {}).billing_cycle || "").toLowerCase() === "monthly";
		const intro = monthly
			? `You keep your current plan until ${until}. Switching sets up auto-renewal at the new price - nothing is charged today.`
			: `You keep your current plan until ${until}, then move to the one you pick. Nothing is charged now.`;
		const html = `<div class="ja-modal-bg">
			<div class="ja-modal">
				<div class="ja-modal-head">
					<h2 class="ja-h">Switch to a smaller plan</h2>
					<button class="ja-modal-close" id="ja-modal-close">✕</button>
				</div>
				<p class="ja-sub">${intro}</p>
				<div class="ja-up-list">${cards || `<div class="ja-empty">No smaller plans available.</div>`}</div>
				<div class="ja-err" id="ja-dn-err"></div>
			</div>
		</div>`;
		const $m = $(html).appendTo(document.body);
		$m.find("#ja-modal-close, .ja-modal-bg").on("click", function (e) {
			if (e.target === this) $m.remove();
		});
		$m.find(".ja-dn-pick").on("click", function () {
			startDowngrade($(this).data("plan"), $m);
		});
	}

	function startDowngrade(target_plan, $modal) {
		const $btn = $modal.find(`.ja-dn-pick[data-plan="${target_plan}"]`);
		setBusy($btn, true);
		frappe
			.call({ method: "jarvis.account.start_downgrade", args: { target_plan } })
			.then((r) => {
				setBusy($btn, false);
				const data = (r.message && r.message.data) || r.message || {};
				$modal.remove();
				if (data.razorpay_subscription_id) {
					// Monthly autopay: a ₹0 mandate-auth Checkout for the cheaper
					// plan (openCheckout takes the subscription_id branch, no amount).
					openCheckout(data, /*upgrade=*/ false);
				} else {
					// Annual / no mandate: scheduled server-side, nothing to pay.
					frappe.show_alert({
						message: __("Downgrade scheduled for your next billing cycle."),
						indicator: "green",
					});
					loadInitial();
				}
			})
			.catch((e) => {
				setBusy($btn, false);
				$modal.find("#ja-dn-err").text(e.message || "Couldn't schedule the downgrade.");
			});
	}

	// ---- re-authorize autopay ---------------------------------------------
	function startReauthorize() {
		setBusy("#ja-cta-reauth", true);
		frappe
			.call({ method: "jarvis.account.reauthorize_autopay" })
			.then((r) => {
				setBusy("#ja-cta-reauth", false);
				const data = (r.message && r.message.data) || r.message || {};
				// Mandate-only Checkout: nothing is charged now (the period is
				// already paid for), so this always takes the subscription_id
				// branch and never an amount.
				openCheckout(data, /*upgrade=*/ false);
			})
			.catch((e) => {
				setBusy("#ja-cta-reauth", false);
				$body
					.find("#ja-billing-err")
					.text(e.message || "Couldn't start auto-renewal setup.");
			});
	}

	// ---- renew flow -------------------------------------------------------
	function startRenew() {
		setBusy("#ja-cta-primary", true);
		setBusy("#ja-cta-secondary", true);
		frappe
			.call({ method: "jarvis.onboarding.renew" })
			.then((r) => {
				setBusy("#ja-cta-primary", false);
				setBusy("#ja-cta-secondary", false);
				const data = (r.message && r.message.data) || r.message || {};
				openCheckout(data, /*upgrade=*/ false);
			})
			.catch((e) => {
				setBusy("#ja-cta-primary", false);
				setBusy("#ja-cta-secondary", false);
				// Frappe already surfaces server messages in its own dialog; adding a
				// generic line here just gave one failure two surfaces.
				$body.find("#ja-billing-err").text((e && e.message) || "");
			});
	}

	// ---- upgrade flow + modal --------------------------------------------
	function openUpgradeModal() {
		const plans = account.upgrade_plans || [];
		const cards = plans
			.map(
				(p) => `
			<div class="ja-up-card" data-plan="${esc(p.name)}">
				<div class="ja-up-card-head">
					<div class="ja-up-card-name">${esc(p.plan_name || p.name)}</div>
					<div class="ja-up-card-price">${inr(p.price_inr)} <span class="jo-plan-cycle">${cycleLabel(
					p.billing_cycle
				)}</span></div>
				</div>
				<div class="ja-up-card-prorate" data-plan-prorate="${esc(
					p.name
				)}">Calculating prorated amount…</div>
				<button class="ja-btn ja-btn-primary ja-up-pick" data-plan="${esc(
					p.name
				)}" disabled>Loading…</button>
			</div>`
			)
			.join("");
		const html = `<div class="ja-modal-bg">
			<div class="ja-modal">
				<div class="ja-modal-head">
					<h2 class="ja-h">Choose an upgrade</h2>
					<button class="ja-modal-close" id="ja-modal-close">✕</button>
				</div>
				<p class="ja-sub">You only pay the prorated difference for the days remaining in your current billing period.</p>
				<div class="ja-up-list">${cards || `<div class="ja-empty">No upgrade plans available.</div>`}</div>
				<div class="ja-err" id="ja-up-err"></div>
			</div>
		</div>`;
		const $m = $(html).appendTo(document.body);
		$m.find("#ja-modal-close, .ja-modal-bg").on("click", function (e) {
			if (e.target === this) $m.remove();
		});
		// Fetch prorated amount per plan in parallel.
		plans.forEach((p) => {
			frappe
				.call({ method: "jarvis.account.preview_upgrade", args: { target_plan: p.name } })
				.then((r) => {
					const d = (r.message && r.message.data) || r.message || {};
					$m.find(`[data-plan-prorate="${p.name}"]`).text(
						`Pay ${inr(d.prorated_inr)} now`
					);
					$m.find(`.ja-up-pick[data-plan="${p.name}"]`)
						.text(`Pay ${inr(d.prorated_inr)}`)
						.prop("disabled", false)
						.on("click", () => startUpgrade(p.name, $m));
				})
				.catch((e) => {
					$m.find(`[data-plan-prorate="${p.name}"]`).text(
						e.message || "Couldn't compute amount."
					);
				});
		});
	}

	function startUpgrade(target_plan, $modal) {
		const $btn = $modal.find(`.ja-up-pick[data-plan="${target_plan}"]`);
		setBusy($btn, true);
		frappe
			.call({ method: "jarvis.account.start_upgrade", args: { target_plan } })
			.then((r) => {
				setBusy($btn, false);
				const data = (r.message && r.message.data) || r.message || {};
				$modal.remove();
				openCheckout(data, /*upgrade=*/ true);
			})
			.catch((e) => {
				setBusy($btn, false);
				$modal.find("#ja-up-err").text(e.message || "Couldn't start upgrade.");
			});
	}

	// ---- Razorpay Checkout (shared) ---------------------------------------
	function openCheckout(handles, isUpgrade) {
		const opts = {
			key: handles.razorpay_key_id,
			name: "Jarvis",
			description: isUpgrade ? "Plan upgrade" : "Subscription payment",
			handler: function (res) {
				confirmAndRefresh({
					razorpay_payment_id: res.razorpay_payment_id,
					razorpay_order_id: res.razorpay_order_id || null,
					razorpay_subscription_id: res.razorpay_subscription_id || null,
					razorpay_signature: res.razorpay_signature,
				});
			},
		};
		if (handles.razorpay_subscription_id) {
			// Recurring (Monthly) upgrade: subscription-mode Checkout collects the
			// mandate; the prorated amount rides on the Subscription as an upfront
			// add-on, so amount/order_id must NOT be set here (an order_id of
			// undefined made Checkout charge a stray standalone payment).
			opts.subscription_id = handles.razorpay_subscription_id;
		} else {
			opts.order_id = handles.razorpay_order_id;
			opts.amount = Math.round((handles.amount_inr || 0) * 100);
			opts.currency = "INR";
		}
		new Razorpay(opts).open();
	}

	// finish_payment + self-healing fallback (idempotent confirm + polling)
	function confirmAndRefresh(payload) {
		showOverlay("Finalizing your payment…");
		frappe
			.call({ method: "jarvis.onboarding.finish_payment", args: { payload } })
			.then(() => {
				hideOverlay();
				loadInitial();
			})
			.catch(() => pollForApplied());
	}

	function pollForApplied() {
		showOverlay("Payment received - finalizing your account…");
		const before = JSON.stringify({
			plan: (account.plan || {}).name || "",
			end: account.current_period_end || "",
			status: account.subscription_status || "",
		});
		let elapsed = 0;
		const tick = setInterval(() => {
			elapsed += 3;
			frappe
				.call({ method: "jarvis.account.get_account" })
				.then((r) => {
					const a = (r && r.message) || {};
					const now = JSON.stringify({
						plan: (a.plan || {}).name || "",
						end: a.current_period_end || "",
						status: a.subscription_status || "",
					});
					if (now !== before) {
						clearInterval(tick);
						hideOverlay();
						loadInitial();
					} else if (elapsed >= 30) {
						clearInterval(tick);
						hideOverlay();
						frappe.msgprint({
							title: "Plan update pending",
							message:
								"Your payment was received. The page should update within a minute - refresh to view, or contact support if it doesn't.",
							indicator: "blue",
						});
					}
				})
				.catch(() => {
					if (elapsed >= 30) {
						clearInterval(tick);
						hideOverlay();
					}
				});
		}, 3000);
	}

	// ---- small helpers ----------------------------------------------------
	function setBusy(sel, on) {
		const $b = typeof sel === "string" ? $body.find(sel) : sel;
		if (on) {
			$b.prop("disabled", true)
				.attr("data-label", $b.html())
				.html(`<span class="ja-spin"></span> Working…`);
		} else if ($b.attr("data-label")) {
			$b.prop("disabled", false).html($b.attr("data-label"));
		}
	}

	function showOverlay(text) {
		hideOverlay();
		$(
			`<div class="ja-overlay"><div class="ja-overlay-card"><span class="ja-spin"></span> ${esc(
				text
			)}</div></div>`
		).appendTo(document.body);
	}
	function hideOverlay() {
		$(".ja-overlay").remove();
	}

	function injectStyles() {
		if (document.getElementById("ja-styles")) return;
		const css = `
		.ja-bg{position:fixed;inset:0;z-index:1;display:flex;align-items:flex-start;justify-content:center;
			overflow:hidden auto;padding:48px 20px;background-color:var(--bg-color)}
		.ja-bg::before{content:"";position:absolute;inset:-25%;z-index:0;will-change:transform;
			animation:ja-aurora 26s ease-in-out infinite alternate;
			background-image:
			  radial-gradient(at 16% 20%, color-mix(in srgb, var(--jarvis-primary) 26%, transparent) 0, transparent 46%),
			  radial-gradient(at 84% 16%, color-mix(in srgb, var(--jarvis-primary-dark) 22%, transparent) 0, transparent 46%),
			  radial-gradient(at 78% 84%, color-mix(in srgb, var(--jarvis-primary-light) 18%, transparent) 0, transparent 44%),
			  radial-gradient(at 22% 82%, color-mix(in srgb, var(--jarvis-primary) 16%, transparent) 0, transparent 46%)}
		@keyframes ja-aurora{0%{transform:translate3d(0,0,0) scale(1) rotate(0deg)}
			50%{transform:translate3d(2.5%,-2%,0) scale(1.08) rotate(1.2deg)}
			100%{transform:translate3d(-2%,2.5%,0) scale(1.05) rotate(-1.2deg)}}
		@media(prefers-reduced-motion:reduce){.ja-bg::before{animation:none}}
		.ja{position:relative;z-index:1;display:flex;gap:0;width:100%;max-width:1080px;margin:auto;border:1px solid var(--border-color);
			border-radius:var(--border-radius-lg,14px);overflow:hidden;box-shadow:0 20px 50px -20px rgba(20,20,50,.45),var(--shadow-md);background:var(--card-bg)}
		.ja-brand{flex:0 0 34%;padding:36px 32px;color:#fff;background:linear-gradient(160deg,var(--jarvis-primary-light) 0%,var(--jarvis-primary-dark) 100%);display:flex;flex-direction:column}
		.ja-logo{font-size:34px;line-height:1}
		.ja-brand-name{font-size:30px;font-weight:700;margin-top:10px;letter-spacing:-.5px}
		.ja-brand-tag{font-size:15px;opacity:.92;margin-top:6px;line-height:1.45}
		.ja-brand-foot{margin-top:auto;padding-top:24px;font-size:12px;opacity:.8}
		.ja-panel{flex:1;padding:34px 36px;min-width:0;display:flex;flex-direction:column;gap:18px}
		.ja-loading,.ja-empty{color:var(--text-muted);padding:18px 0}
		.ja-card{border:1px solid var(--border-color);border-radius:12px;padding:20px;background:var(--card-bg)}
		/* Segmented control - full-width two-up tab picker with sliding thumb */
		.ja-tabs-label{display:block;font-size:11.5px;letter-spacing:.6px;font-weight:600;
			color:var(--text-muted);text-transform:uppercase;margin-bottom:8px}
		.ja-tabs{position:relative;display:flex;width:100%;padding:4px;margin-bottom:18px;
			background:var(--bg-color);border:1px solid var(--border-color);border-radius:10px;
			user-select:none}
		.ja-tabs-thumb{position:absolute;top:4px;bottom:4px;left:4px;width:calc(50% - 4px);
			background:var(--card-bg,#fff);border-radius:8px;
			box-shadow:0 1px 2px rgba(0,0,0,.06), 0 0 0 1px rgba(0,0,0,.04);
			transition:transform .28s cubic-bezier(.4,0,.2,1);pointer-events:none}
		.ja-tabs[data-active="subscription"] .ja-tabs-thumb{transform:translateX(100%)}
		.ja-tab{position:relative;z-index:1;flex:1;appearance:none;display:inline-flex;
			align-items:center;justify-content:center;gap:8px;background:transparent;border:0;
			padding:11px 12px;font-size:13.5px;font-weight:500;color:var(--text-muted);
			border-radius:8px;cursor:pointer;transition:color .2s ease;white-space:nowrap}
		.ja-tab svg{opacity:.7;transition:opacity .2s ease}
		.ja-tab:hover{color:var(--text-color)}
		.ja-tab:hover svg{opacity:.95}
		.ja-tab-active{color:var(--jarvis-primary);font-weight:600}
		.ja-tab-active svg{opacity:1}
		.ja-tab-body{transition:opacity .14s ease,transform .14s ease}
		.ja-tab-body-swap{opacity:0;transform:translateY(4px)}
		.ja-url-row{display:flex;gap:8px;align-items:center}
		.ja-url-text{flex:1;font-family:'Menlo','Monaco',monospace;font-size:11.5px;line-height:1.4;
			padding:8px 10px;background:var(--bg-color);border:1px solid var(--border-color);
			border-radius:6px;white-space:nowrap;overflow-x:auto;color:var(--text-muted);
			display:block;min-width:0}
		.ja-btn-small{padding:6px 12px;font-size:12px;flex:0 0 auto}
		.ja-card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
		.ja-eyebrow{font-size:11.5px;letter-spacing:.6px;font-weight:600;color:var(--text-muted);text-transform:uppercase}
		.ja-h{font-size:20px;font-weight:700;margin:2px 0 4px;color:var(--text-color)}
		.ja-sub{font-size:13.5px;color:var(--text-muted);margin:0 0 14px}
		.ja-plan-meta{font-size:14px;color:var(--text-color);margin-top:4px}
		.ja-pill{padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600;letter-spacing:.3px;border:1px solid transparent;white-space:nowrap}
		.ja-pill-ok{background:rgba(46,189,89,.12);color:var(--green-700,#1f8d3a);border-color:rgba(46,189,89,.25)}
		.ja-pill-warn{background:rgba(245,159,0,.14);color:var(--yellow-700,#a06200);border-color:rgba(245,159,0,.28)}
		.ja-pill-bad{background:rgba(226,76,76,.12);color:var(--red-600,#c0392b);border-color:rgba(226,76,76,.25)}
		.ja-pill-muted{background:var(--bg-color);color:var(--text-muted);border-color:var(--border-color)}
		.ja-banner{margin-top:14px;padding:10px 12px;border-radius:8px;font-size:13px}
		.ja-banner-ok{background:rgba(46,189,89,.08);color:var(--text-color)}
		.ja-banner-warn{background:rgba(245,159,0,.10);color:var(--text-color)}
		.ja-banner-bad{background:rgba(226,76,76,.10);color:var(--text-color)}
		.ja-row2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
		.ja-field{margin-bottom:6px}
		.ja-field label{display:block;font-size:12.5px;font-weight:600;color:var(--text-color);margin-bottom:6px}
		.ja-input{width:100%;padding:10px 12px;font-size:14px;border:1px solid var(--border-color);border-radius:var(--border-radius,8px);background:var(--control-bg,var(--bg-color));color:var(--text-color)}
		.ja-input:focus{outline:none;border-color:var(--jarvis-primary);box-shadow:0 0 0 2px var(--jarvis-primary-faint)}
		.ja-input:disabled{opacity:.6;cursor:not-allowed}
		.ja-pwd{position:relative}
		.ja-pwd .ja-input{padding-right:38px}
		.ja-pwd-toggle{position:absolute;top:50%;right:6px;transform:translateY(-50%);background:transparent;border:0;cursor:pointer;
			color:var(--text-muted);padding:6px;line-height:0;border-radius:6px}
		.ja-pwd-toggle:hover{color:var(--text-color);background:var(--bg-color)}
		.ja-pwd-toggle:disabled{opacity:.4;cursor:not-allowed}
		.ja-pwd-toggle .ja-eye-off{display:none}
		.ja-pwd-toggle.shown .ja-eye-on{display:none}
		.ja-pwd-toggle.shown .ja-eye-off{display:inline}
		.ja-actions{margin-top:14px;display:flex;align-items:center;gap:10px}
		.ja-btn{padding:9px 18px;font-size:14px;font-weight:600;border-radius:var(--border-radius,8px);border:1px solid transparent;cursor:pointer}
		.ja-btn-primary{background:var(--jarvis-primary);color:#fff}
		.ja-btn-primary:hover{filter:brightness(1.06)}
		.ja-btn-primary:disabled{opacity:.5;cursor:not-allowed}
		.ja-btn-ghost{background:transparent;border-color:var(--border-color);color:var(--text-color)}
		.ja-btn-ghost:hover{border-color:var(--jarvis-primary);color:var(--jarvis-primary)}
		/* Sprint-4 punch-list: danger variant signals destructive action.
		   Used on the api_key Save button when the customer is still in
		   oauth mode (Save will disconnect the subscription). Same shape
		   as ja-btn-primary so layout doesn't jump; red background +
		   white text + accessible contrast on hover.   */
		.ja-btn-danger{background:var(--red-500,#e24c4c);color:#fff}
		.ja-btn-danger:hover{filter:brightness(.92)}
		.ja-btn-danger:disabled{opacity:.5;cursor:not-allowed}
		.ja-banner-danger{background:rgba(226,76,76,.12);color:var(--text-color);border:1px solid rgba(226,76,76,.30);border-radius:var(--border-radius,8px);padding:10px 12px;margin:8px 0 14px}
		.ja-err{color:var(--red-500,#e24c4c);font-size:12.5px;margin-top:8px;min-height:1px}
		.ja-llm-status{color:var(--text-muted);font-size:12.5px;margin-left:8px}
		.ja-spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.5);border-top-color:#fff;border-radius:50%;animation:ja-spin .6s linear infinite;vertical-align:-1px}
		@keyframes ja-spin{to{transform:rotate(360deg)}}
		.ja-modal-bg{position:fixed;inset:0;background:rgba(20,20,40,.55);z-index:1000;display:flex;align-items:center;justify-content:center;padding:20px}
		.ja-modal{background:var(--card-bg);border-radius:14px;max-width:600px;width:100%;padding:28px;box-shadow:0 20px 60px -20px rgba(0,0,0,.5)}
		.ja-modal-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px}
		.ja-modal-close{background:transparent;border:0;font-size:18px;cursor:pointer;color:var(--text-muted)}
		.ja-up-list{display:flex;flex-direction:column;gap:10px;margin-top:12px}
		.ja-up-card{border:1px solid var(--border-color);border-radius:10px;padding:14px 16px}
		.ja-up-card-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
		.ja-up-card-name{font-size:14px;font-weight:600;color:var(--text-color)}
		.ja-up-card-price{font-size:16px;font-weight:700;color:var(--text-color)}
		.ja-up-card-prorate{font-size:13px;color:var(--text-muted);margin-bottom:10px}
		/* Downgrade rows: compact, one line per plan. Name + the capacity it
		   provisions on the left, price and a quiet Switch button on the right —
		   no dead vertical space, and the primary colour stays for upgrade/renew. */
		.ja-dn-row{display:flex;align-items:center;gap:14px;border:1px solid var(--border-color);border-radius:10px;padding:12px 14px;transition:border-color .15s}
		.ja-dn-row:hover{border-color:var(--jarvis-primary-faint)}
		.ja-dn-info{flex:1 1 auto;min-width:0}
		.ja-dn-name{font-size:14.5px;font-weight:600;color:var(--text-color)}
		.ja-dn-cap{font-size:12.5px;color:var(--text-muted);margin-top:2px;font-variant-numeric:tabular-nums}
		.ja-dn-price{font-size:15px;font-weight:700;color:var(--text-color);white-space:nowrap;font-variant-numeric:tabular-nums}
		.ja-dn-price .jo-plan-cycle{font-size:12px;font-weight:500;color:var(--text-muted);margin-left:1px}
		.ja-dn-pick{flex:0 0 auto;padding:7px 16px}
		.ja-btn-ghost .ja-spin{border-color:var(--jarvis-primary-faint);border-top-color:var(--jarvis-primary)}
		.ja-overlay{position:fixed;inset:0;z-index:2000;background:rgba(20,20,40,.45);display:flex;align-items:center;justify-content:center}
		.ja-overlay-card{background:var(--card-bg);padding:18px 22px;border-radius:10px;font-size:14px;color:var(--text-color);box-shadow:0 12px 40px -12px rgba(0,0,0,.4)}
		.ja-overlay .ja-spin{border-color:var(--jarvis-primary-faint);border-top-color:var(--jarvis-primary)}
		.ja-kv{width:100%;border-collapse:collapse;margin:8px 0 16px}
		.ja-kv td{padding:6px 0;font-size:13.5px}
		.ja-kv td:first-child{color:var(--text-muted);width:140px}
		/* --- LLM 3-mode editor (Quick | Preset | Custom) --- */
		/* Three-up segmented control: thumb is 1/3 wide and slides in thirds. */
		.ja-tabs-3 .ja-tabs-thumb{width:calc(33.333% - 4px)}
		.ja-tabs-3[data-active="preset"] .ja-tabs-thumb{transform:translateX(100%)}
		.ja-tabs-3[data-active="custom"] .ja-tabs-thumb{transform:translateX(200%)}
		/* Preset cards */
		.ja-preset-scroll{max-height:min(48vh,440px);overflow-y:auto;padding-right:4px;scrollbar-gutter:stable}
		.ja-preset-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:6px}
		.ja-preset-card{border:1.5px solid var(--border-color);border-radius:12px;padding:14px;cursor:pointer;background:var(--card-bg);transition:border-color .15s,box-shadow .15s}
		.ja-preset-card:hover{border-color:var(--jarvis-primary)}
		.ja-preset-card.ja-preset-selected{border-color:var(--jarvis-primary);box-shadow:0 0 0 2px var(--jarvis-primary-faint)}
		.ja-preset-label{font-size:13px;font-weight:700;color:var(--text-color);margin-bottom:4px}
		.ja-preset-model{font-size:12px;color:var(--text-muted);margin-top:4px}
		/* Custom failover rows */
		.ja-crow{position:relative;border:1px solid var(--border-color);border-radius:10px;padding:12px;margin-bottom:10px;background:var(--card-bg)}
		.ja-crow-tag{position:absolute;top:-9px;left:12px;font-size:10.5px;font-weight:600;letter-spacing:.3px;text-transform:uppercase;color:var(--text-muted);background:var(--card-bg);padding:0 6px}
		.ja-crow-head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
		.ja-crow-move{margin-left:auto;display:flex;gap:4px}
		.ja-cred-toggle{display:inline-flex;border:1px solid var(--border-color);border-radius:8px;overflow:hidden}
		.ja-cred-btn{appearance:none;border:0;background:var(--card-bg);color:var(--text-muted);font-size:12px;font-weight:500;padding:6px 12px;cursor:pointer;transition:background .15s,color .15s}
		.ja-cred-btn:hover{color:var(--text-color)}
		.ja-cred-btn.ja-cred-active{background:var(--jarvis-primary-faint);color:var(--jarvis-primary);font-weight:600}
		.ja-cred-btn:disabled{opacity:.5;cursor:not-allowed}
		.ja-crow-grid{display:grid;grid-template-columns:1fr 1.4fr 1.4fr 1.4fr;gap:8px;align-items:center}
		.ja-crow-grid-sub{grid-template-columns:2fr 1fr 1.2fr}
		.ja-accts{display:flex;flex-direction:column;gap:5px;margin:8px 0}
		.ja-acct{display:flex;align-items:center;gap:8px;padding:5px 9px;border:1px solid rgba(46,189,89,.35);background:rgba(46,189,89,.08);border-radius:6px}
		.ja-acct-dot{font-size:11px;font-weight:600;color:var(--green-700,#1f8d3a)}
		.ja-acct-label{font-size:12.5px;color:var(--text-color)}
		.ja-acct-rm{margin-left:auto;border:1px solid rgba(226,76,76,.35);background:rgba(226,76,76,.10);color:var(--red-600,#c0392b);border-radius:5px;width:22px;height:22px;cursor:pointer;flex:none;font-size:11px}
		.ja-acct-rm:disabled{opacity:.5;cursor:not-allowed}
		.ja-connect{padding:10px;background:var(--bg-color);border:1px solid var(--border-color);border-radius:8px;margin-bottom:8px}
		.ja-connect textarea{resize:vertical}
		@media(max-width:760px){.ja{flex-direction:column}.ja-brand{flex-basis:auto}.ja-panel{padding:24px 22px}.ja-row2{grid-template-columns:1fr}.ja-crow-grid,.ja-crow-grid-sub{grid-template-columns:1fr}}
		`;
		$(`<style id="ja-styles">${css}</style>`).appendTo(document.head);
	}
};
