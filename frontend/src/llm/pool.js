// Shared pure pool logic. Consumed by the Vue app (direct import) AND the desk
// onboarding page (via jarvis_onboarding_llm.bundle.js -> window). No framework imports.
export function deriveMode(models, preset) {
	const list = Array.isArray(models) ? models : [];
	// A chat-subscription model needs the cliproxy sidecar, which only the proxy
	// path provisions - so even a single subscription is "proxy". (#200 review #1)
	const hasSubscription = list.some(
		(m) =>
			m &&
			(m.subscription ||
				m.credentialType === "subscription" ||
				m.credential_type === "subscription")
	);
	// A preset is picked from a curated ladder that always resolves through the
	// proxy path, so it stays "proxy" independent of model count. Otherwise mode
	// mirrors the backend's compute_proxy_active: a proxy sidecar is only
	// deployed for a subscription model, so a pool of any size that is purely
	// BYO api keys is "direct" - it renders agent-direct with no sidecar.
	// (was: any 2+-model pool counted as "proxy" regardless of credential type)
	return hasSubscription || preset ? "proxy" : "direct";
}
// The one place that NAMES a tenant's LLM topology. Settings > General and
// Settings > Usage's pool-status section each used to name it themselves and
// disagreed:
// a 2-model api-key pool read "Direct" on General while Billing called the
// identical state "Pool (direct failover)" (jarvis#561).
//
// The two inputs answer DIFFERENT questions and blurring them is what this
// wording has to keep apart (backend: compute_pool_mode vs compute_proxy_active):
//   proxyActive  - a Bifrost + CLIProxyAPI sidecar pair is deployed in front of
//                  the container. Only a chat subscription needs one, because
//                  cliproxy is what serves an OAuth blob.
//   enabledCount - how many models the container fails over between. A pool of
//                  BYO api keys renders agent-direct and fails over natively
//                  with NO sidecar at all, so it is a pool AND it is direct - the
//                  parenthetical is what says so without claiming a proxy.
export function connectionModeLabel(proxyActive, enabledCount) {
	if (proxyActive) return "Pool (proxied)";
	return Number(enabledCount || 0) > 1 ? "Pool (direct failover)" : "Direct";
}
export function uniqueVendors(entry) {
	if (entry && Array.isArray(entry.vendors) && entry.vendors.length)
		return entry.vendors.slice();
	const seen = new Set(),
		out = [];
	for (const m of entry?.models || [])
		if (!seen.has(m.provider)) {
			seen.add(m.provider);
			out.push(m.provider);
		}
	return out;
}
export function missingVendorKeys(entry, keysByVendor) {
	return uniqueVendors(entry).filter((v) => !(keysByVendor?.[v] || "").trim());
}
export function presetToModels(entry, keysByVendor) {
	return (entry?.models || [])
		.slice()
		.sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
		.map((m, i) => ({
			provider: m.provider,
			model: m.model,
			api_key: (keysByVendor?.[m.provider] || "").trim(),
			order: i,
		}));
}
export function buildCustomModels(rows) {
	return (rows || [])
		.filter((r) => r && (r.provider || "").trim() && (r.model || "").trim())
		.map((r, i) => {
			const m = {
				provider: r.provider.trim(),
				model: r.model.trim(),
				api_key: effectiveApiKey(r.provider, r.apiKey, r.hasKey),
				order: i,
			};
			if (r.hasKey) m.has_key = true;
			const b = (r.baseUrl || "").trim();
			if (b) m.base_url = b;
			return m;
		});
}
export function reorder(list, from, to) {
	const a = list.slice();
	const [x] = a.splice(from, 1);
	a.splice(to, 0, x);
	return a;
}
// Built-in fallback for chat-subscription model ids per upstream. The catalog is
// owned by admin now (Jarvis LLM Provider doctype) and arrives via
// get_chat_ui_settings.subscription_models; this literal is the degraded-mode
// floor used before the first response lands and when admin is unreachable.
// Do NOT add models here to make them selectable: add them in the admin desk.
const FALLBACK_SUB_MODELS = {
	openai: ["gpt-5.5", "gpt-5.4"],
	xai: ["grok-4.3", "grok-build-0.1"],
	kimi: ["kimi-k2.7-code", "kimi-k2.6"],
};

// Provider label (as admin stores it) -> the upstream key the pool editor uses.
const LABEL_TO_UPSTREAM = {
	OpenAI: "openai",
	"xAI Grok": "xai",
	"Kimi (Moonshot)": "kimi",
};

// Map a get_chat_ui_settings.subscription_models payload (keyed by provider
// LABEL) to the upstream keys this module uses. Falls back when empty.
export function subModelSuggestions(apiSubscriptionModels) {
	const src = apiSubscriptionModels || {};
	const out = {};
	for (const [label, models] of Object.entries(src)) {
		const upstream = LABEL_TO_UPSTREAM[label];
		if (upstream && Array.isArray(models) && models.length) out[upstream] = models;
	}
	return Object.keys(out).length ? out : FALLBACK_SUB_MODELS;
}

// Default chat-subscription model for an upstream. Synchronous by contract: the
// onboarding editor calls it before any API response exists, so `catalog` is
// optional and omitting it yields the built-in fallback.
export function defaultSubscriptionModel(upstream, catalog) {
	const table = catalog && Object.keys(catalog).length ? catalog : FALLBACK_SUB_MODELS;
	const models = table[upstream] || FALLBACK_SUB_MODELS[upstream] || FALLBACK_SUB_MODELS.openai;
	return models[0];
}
export function validatePool(models, preset) {
	if (!Array.isArray(models) || models.length === 0)
		return { ok: false, error: "Add at least one model." };
	for (const m of models) {
		// Chat-subscription model: needs a provider + a model id + at least one
		// connected account. No api_key required (jarvis#756: a subscription row
		// that reaches here with no provider is refused HERE, with a clear
		// message, rather than accepted locally and rejected later by admin once
		// the OAuth capture already went through).
		if (m.subscription) {
			if (!(m.provider || "").trim())
				return { ok: false, error: "Choose a provider for your chat subscription." };
			if (!(m.model || "").trim())
				return { ok: false, error: "Every model needs a model id." };
			const accounts = Array.isArray(m.subscription.accounts) ? m.subscription.accounts : [];
			// Connected = a freshly-captured account this session (has a capture_id) OR a
			// previously-connected account (has an account_ref; its stored blob is merged
			// back on save). The raw oauth_blob is NEVER a wire field anymore (P0-04), so
			// it is not consulted here - a stale bundle sending one cannot pass this gate.
			const connected = accounts.some(
				(a) => a && ((a.capture_id || "").trim() || (a.account_ref || "").trim())
			);
			if (!connected)
				return {
					ok: false,
					error: `Model ${m.model} needs at least one connected account.`,
				};
			continue;
		}
		// API-key model.
		if (!(m.provider || "").trim() || !(m.model || "").trim())
			return { ok: false, error: "Every model needs a provider and a model id." };
		// Custom-endpoint providers ARE their base_url - an OpenAI-Compatible shim
		// (e.g. a Claude-CLI gateway) or a local vLLM with no base_url would push a
		// provider that routes nowhere, and both validators used to let it through.
		const pid = _ID_BY_LABEL[m.provider] || (m.provider || "").trim().toLowerCase();
		if (NEEDS_BASE_URL.has(pid) && !(m.base_url || "").trim()) {
			return {
				ok: false,
				error: `Model ${m.model}: ${m.provider} needs a Base URL (its custom endpoint).`,
			};
		}
		// A freshly-entered key OR a previously-saved key (has_key; merged back on
		// save). Local providers (Ollama, vLLM) run inside/near the container with
		// no auth of their own - a key is optional, not a value to make up.
		if (!(m.api_key || "").trim() && !m.has_key && !LOCAL_PROVIDER_IDS.has(pid))
			return { ok: false, error: `Model ${m.model} needs an API key.` };
	}
	return { ok: true, error: "" };
}

// Client-side mirror of admin's `multi_sub_without_backstop` rule (jarvis#934):
// a pool with 2+ chat-subscription models on DISTINCT upstreams and no
// non-subscription (API-key) model has no fallback if a subscription is
// rate-limited or its OAuth session drops - chat just stalls. That used to
// surface only at apply time, from admin's rejection, well after the second
// subscription was already wired up (and the OAuth dance already done).
// Non-blocking: validatePool above stays the hard save-time gate; this is
// only meant to speak up the moment the invalid combination exists, before
// the customer has invested in a second sign-in.
//
// Two subscriptions on the SAME upstream do NOT warn - failing over between
// two accounts on one upstream is still a real backstop, just not an
// api-key one. ANY non-subscription row satisfies the backstop, including a
// keyless local provider (Ollama/vLLM): the rule is "is there a
// non-subscription leg to fall back to", not "is there a paid API key".
//
// Tolerates both shapes this module deals in, same as deriveMode above: raw
// editor rows (credentialType + a top-level upstream) and the
// validatePool/save shape (a `subscription` object, upstream only on its
// accounts) - so a caller can pass rows straight from the editor with no
// normalization step.
export function poolBackstopWarning(models) {
	const list = Array.isArray(models) ? models : [];
	const isSubscription = (m) =>
		!!(
			m &&
			(m.subscription ||
				m.credentialType === "subscription" ||
				m.credential_type === "subscription")
		);
	const upstreamOf = (m) => {
		const accounts = (m.subscription && m.subscription.accounts) || m.accounts || [];
		return m.upstream || (accounts[0] && accounts[0].upstream) || "openai";
	};
	const subUpstreams = new Set(list.filter(isSubscription).map(upstreamOf));
	const hasNonSubscriptionModel = list.some((m) => m && !isSubscription(m));
	if (subUpstreams.size >= 2 && !hasNonSubscriptionModel) {
		return "A pool with 2+ subscription providers and no API-key-backed model has no backstop - if a subscription is rate-limited or drops, chat can stall. Add an API-key model as a fallback.";
	}
	return "";
}

// ---- provider id <-> label ------------------------------------------------
// Ports jarvis_account.js's PROVIDER_LABEL_BY_ID / providerLabel() verbatim
// (same ids, same labels) so the dropdown in the shared editor matches the
// desk page exactly. Stored pools may carry either the provider *id* (e.g.
// "openai_compat" from presets / admin normalization) or the dropdown
// *label* ("OpenAI-Compatible") directly - providerLabel() maps id -> label
// and passes an already-a-label (or unknown) value through unchanged.
export const PROVIDER_LABELS = [
	{ id: "anthropic", label: "Anthropic" },
	{ id: "openai", label: "OpenAI" },
	{ id: "gemini", label: "Google Gemini" },
	{ id: "mistral", label: "Mistral" },
	{ id: "groq", label: "Groq" },
	{ id: "together", label: "Together AI" },
	{ id: "deepseek", label: "DeepSeek" },
	{ id: "moonshot", label: "Moonshot (Kimi)" },
	{ id: "xai", label: "xAI Grok" },
	{ id: "zai", label: "GLM / Z.ai" },
	// z.ai sells two distinct products on two distinct endpoints: pay-as-you-go
	// API credits (plain "zai", above) and a separate "GLM Coding Plan"
	// subscription that reports "insufficient balance" (code 1113) on the
	// pay-as-you-go endpoint even with a perfectly valid key - the two do not
	// share a balance. A dedicated provider id (rather than a toggle inside the
	// "zai" option) means the existing PROVIDER_DEFAULTS/NEEDS_BASE_URL/dropdown
	// machinery just works with no new UI - the same shape every other provider
	// already uses. See apiKeyModelHealth() below for the targeted hint when a
	// "zai" row hits this exact trap.
	{ id: "zai_coding", label: "GLM / Z.ai (Coding Plan)" },
	{ id: "openrouter", label: "OpenRouter" },
	{ id: "ollama", label: "Ollama (local)" },
	{ id: "vllm", label: "vLLM (local)" },
	{ id: "openai_compat", label: "OpenAI-Compatible" },
];
const _LABEL_BY_ID = Object.fromEntries(PROVIDER_LABELS.map((p) => [p.id, p.label]));
const _ID_BY_LABEL = Object.fromEntries(PROVIDER_LABELS.map((p) => [p.label, p.id]));

// Providers whose approval screen hands back a BARE authorization code instead
// of redirecting to a callback URL the customer can copy from the address bar.
// Keyed by BOTH the pool `upstream` value ("xai") and the OAuth provider label
// ("xAI Grok"), so the pool editor and the direct paste-back card can share one
// answer instead of each keeping a copy.
//
// MUST match the providers carrying `code_only_paste` in
// jarvis/oauth/providers.py - that flag is what actually makes the backend take
// a bare code; this only steers the paste copy. Telling an xAI customer to
// "copy the full URL from the address bar" sends them hunting for an address
// bar that never holds a code.
const _CODE_ONLY_PASTE = new Set(["xai", "xAI Grok"]);
export const isCodeOnlyPaste = (upstreamOrLabel) => _CODE_ONLY_PASTE.has(upstreamOrLabel);

// Custom-endpoint providers whose whole identity IS the base_url (no default
// endpoint) - validatePool requires one for these (mirrors validate_models).
// "zai" (GLM / Z.ai, pay-as-you-go) and "zai_coding" (GLM Coding Plan) both
// have no native Bifrost provider, so each needs its own Z.ai base_url
// (standard api.z.ai/api/paas/v4 vs coding-plan api.z.ai/api/coding/paas/v4).
const NEEDS_BASE_URL = new Set(["openai_compat", "vllm", "zai", "zai_coding"]);
// Providers with no auth of their own - Ollama/vLLM run inside or next to the
// tenant's container (loopback / LAN), so there is no key to bring. Shared
// with LlmPoolEditor.vue's Test-button disclaimer (predates this export) -
// MUST also match jarvis.llm_key_probe.LOCAL_PROVIDER_IDS on the Python side.
export const LOCAL_PROVIDER_IDS = new Set(["ollama", "vllm"]);
export function providerLabel(id) {
	return _LABEL_BY_ID[id] || id || "";
}
export function providerId(label) {
	return _ID_BY_LABEL[label] || label || "";
}

// True when a base URL names an endpoint only the tenant's CONTAINER can reach:
// loopback, an RFC1918 / CGNAT / link-local address, a .local-style suffix, or a
// bare single-label name (a docker service alias). Probing one of those from the
// bench says nothing about the key, and before jarvis#556 it blocked the save of
// a perfectly good private endpoint.
//
// Keyed on the ADDRESS, not the provider id. A private endpoint reached through
// openai_compat is exactly as unreachable from this bench as one reached through
// ollama, and the provider id was never evidence either way.
//
// SSRF direction: this only ever REMOVES a bench-originated request to a private
// address, it never adds one. The server keeps its own link_fetch SSRF guard, so
// any probe that does reach it is still refused there.
export function isContainerOnlyBaseUrl(rawUrl) {
	const raw = (rawUrl || "").trim();
	if (!raw) return false;
	let host = "";
	try {
		host = new URL(raw.includes("://") ? raw : `http://${raw}`).hostname.toLowerCase();
	} catch {
		return false; // unparseable, so treat as public and let the probe run
	}
	// URL.hostname KEEPS the brackets on an IPv6 literal (unlike Python's
	// urlsplit), so strip them before matching.
	if (host.startsWith("[") && host.endsWith("]")) host = host.slice(1, -1);
	if (!host) return false;
	if (host === "::1") return true;
	if (/^f[cd][0-9a-f]{2}:/.test(host)) return true; // unique-local
	if (/^fe[89ab][0-9a-f]:/.test(host)) return true; // link-local
	// A single label resolves only inside a container network. Require it to look
	// like a real hostname so a malformed URL is not quietly waved through as
	// "local" and left unprobed.
	if (!host.includes(".")) return /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(host);
	if (/\.(local|internal|lan|home|localdomain)$/.test(host)) return true;
	const quad = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
	if (!quad) return false;
	const a = Number(quad[1]);
	const b = Number(quad[2]);
	if (a === 0 || a === 127 || a === 10) return true;
	if (a === 192 && b === 168) return true;
	if (a === 169 && b === 254) return true;
	if (a === 172 && b >= 16 && b <= 31) return true;
	if (a === 100 && b >= 64 && b <= 127) return true; // CGNAT
	return false;
}

// Row-level form of the above. A local provider (ollama/vllm) is container-only
// ONLY when its base URL is empty or itself container-only (F4/F7): a local
// provider id pointed at a remotely-reachable URL (e.g. Ollama with
// https://api.openai.com/v1) is NOT exempt from the probe - the short-circuit on
// the provider id alone let a remote endpoint reach desired state untested.
export function isContainerOnlyRow(row) {
	if (!row) return false;
	const url = row.baseUrl || row.base_url || "";
	if (LOCAL_PROVIDER_IDS.has(providerId(row.provider))) {
		return !url || isContainerOnlyBaseUrl(url);
	}
	return isContainerOnlyBaseUrl(url);
}
// The api_key value to persist for one API-key row. A real typed key always
// wins; an unchanged has_key row sends "" (the merge-on-save convention - see
// buildCustomModels/buildSaveModels callers, "let backend merge keep a stored
// key on re-save"). Only a BLANK, never-saved key on a local provider (Ollama
// / vLLM) gets the placeholder: the backend's dangling-key_ref guard
// (pool_serialize.validate_models) requires a non-empty api_key on every
// enabled api_key model regardless of provider, and local providers don't
// authenticate with it anyway, so a fixed, harmless placeholder satisfies
// that contract without asking the customer to invent one.
export function effectiveApiKey(providerRaw, apiKey, hasKey) {
	const trimmed = (apiKey || "").trim();
	if (trimmed || hasKey) return trimmed;
	const pid = _ID_BY_LABEL[providerRaw] || (providerRaw || "").trim().toLowerCase();
	return LOCAL_PROVIDER_IDS.has(pid) ? "local" : "";
}

// ---- config -> editor rows -------------------------------------------------
// Ports jarvis_account.js's seedLlmSetupFromConfig() into a pure function that
// turns a jarvis.onboarding.get_llm_config payload into the shared editor's
// row shape. get_llm_config never returns secrets - api-key rows carry
// `has_key` (bool) instead of the key itself, and subscription rows carry
// `accounts`/`rotation` flat on the model entry (credential_type distinguishes
// the two), NOT a nested `subscription` object with an `api_key` string.
// Both shapes are accepted here (credential_type/has_key from the real
// payload, subscription/api_key from the plain object shape) so this stays a
// faithful mirror of the desk regardless of which shape a caller passes.
export function seedRowsFromConfig(cfg) {
	const models = cfg && Array.isArray(cfg.models) ? cfg.models : [];
	return models
		.slice()
		.sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
		.map((m, i) => {
			const sub = m.subscription || null;
			const isSubscription = m.credential_type === "subscription" || !!sub;
			if (isSubscription) {
				const rotation = (sub && sub.rotation) || m.rotation || "sticky";
				const rawAccounts = (sub && sub.accounts) || m.accounts || [];
				const accounts = rawAccounts.map((a) => ({
					upstream: a.upstream,
					account_ref: a.account_ref,
					label: a.label,
					connected: true,
				}));
				return {
					provider: "",
					model: m.model || "",
					apiKey: "",
					hasKey: !!m.has_key,
					baseUrl: "",
					credentialType: "subscription",
					rotation,
					accounts,
					order: i,
				};
			}
			return {
				provider: providerLabel(m.provider),
				model: m.model || "",
				apiKey: "",
				hasKey: !!(m.has_key || m.api_key),
				baseUrl: m.base_url || "",
				credentialType: "api_key",
				rotation: "sticky",
				accounts: [],
				order: i,
			};
		});
}

// The fleet's last per-model probe verdict for ONE api-key row, matched out of the
// model_statuses list (contract 1.11: [{ provider, model, status }], api-key models only;
// contract 1.12 adds an optional `detail` string carrying the provider's raw error text -
// absent on an older fleet). The match keys on (provider, model) TOGETHER: a model id is NOT
// unique -- validate() only forbids duplicate provider/model PAIRS, so the same id can appear
// under two providers, and keying on the id alone would attach one provider's verdict to the
// other's row. Rows carry the provider LABEL while model_statuses carry the id, so normalize
// the row's provider back to an id to compare. Returns null when the row has no matching
// verdict (a pre-1.11 fleet, a model not yet probed, or an entry that belongs to a different
// provider) - callers must not assume a match.
function modelVerdictEntry(row, modelStatuses) {
	if (!row || !Array.isArray(modelStatuses)) return null;
	const rid = providerId(row.provider);
	return modelStatuses.find((e) => e && e.model === row.model && e.provider === rid) || null;
}

// Sensible truncation for a provider error string of unknown length dropped into a fixed-
// width row/tooltip. The inline chip LABEL sits in a `white-space:nowrap` flex row next to
// several other elements, so it stays short; the tooltip TITLE has more room (still capped -
// the backend already caps at 400 chars, but a defensive cap here means this helper is safe
// even if that ever changes).
function truncate(s, max) {
	const t = (s || "").trim();
	return t.length > max ? t.slice(0, max - 1).trimEnd() + "…" : t;
}

// z.ai's exact "wrong endpoint" trap: a GLM Coding Plan key authenticates fine against
// the pay-as-you-go endpoint (api.z.ai/api/paas/v4) but that endpoint reports the key's
// coding-plan balance as zero, so the probe fails with z.ai's real error text - code 1113,
// "Insufficient balance or no resource package. Please recharge." - even though the key
// itself is perfectly valid on the coding-plan endpoint. Only meaningful on a "zai" (the
// pay-as-you-go option) row: a "zai_coding" row already points at the right endpoint, so
// the same error there is a real balance problem, not a wrong-endpoint one. Matching is
// defensive by construction - it only ever fires when `detail` is present (contract 1.12);
// an older fleet that never sends `detail` simply never matches and falls through to the
// generic "Not working" message below.
const _ZAI_INSUFFICIENT_BALANCE_RE = /\b1113\b|insufficient balance/i;
function isZaiWrongEndpoint(row, entry) {
	if (!entry || !entry.detail) return false;
	if (providerId(row && row.provider) !== "zai") return false;
	return _ZAI_INSUFFICIENT_BALANCE_RE.test(entry.detail);
}

// Map an api-key pool row to its health for the failover list. Unlike a subscription
// (probed pool-wide), an api-key model is probed in isolation, so its row shows its OWN
// verdict instead of the presence-only "key set" that used to make a dead model look
// identical to a healthy one.
//   failed    -> warn      : rejected (bad key/model id) OR an unreachable base_url
//     - a "zai" row whose failure detail is z.ai's insufficient-balance/1113 error gets a
//       specific hint instead of the generic message (see isZaiWrongEndpoint above)
//   unchecked -> unchecked : could not confirm; re-checked on the next apply
//   verified / "" / unknown -> ok (quiet green; "" = pre-1.11 fleet or a not-yet-probed row)
//
// `detail` (fleet-agent contract 1.12) carries the PROVIDER'S OWN error message on a failed
// row (e.g. "Insufficient balance or no resource package. Please recharge." from a real
// GLM/Z.ai zero-balance case) instead of the generic "Not working" - the whole point being a
// customer can tell a bad key from an unpaid account. Consumed DEFENSIVELY: an older
// fleet-agent that doesn't send it yet falls back to today's text, so this must work
// unchanged against a fleet that predates the field.
//
// Order matters: the z.ai wrong-endpoint hint is checked FIRST because it is the specific
// diagnosis of one particular `detail` string, and reporting the raw "insufficient balance"
// text there would actively mislead (the key has balance; it's pointed at the wrong
// endpoint). Every other `detail` falls through to the generic rendering below.
export function apiKeyModelHealth(row, modelStatuses) {
	const entry = modelVerdictEntry(row, modelStatuses);
	const status = (entry && entry.status) || "";
	const detail = (entry && typeof entry.detail === "string" && entry.detail.trim()) || "";
	if (status === "failed") {
		if (isZaiWrongEndpoint(row, entry)) {
			return {
				level: "warn",
				label: "Wrong Z.ai endpoint",
				title:
					"Z.ai reported insufficient balance on the pay-as-you-go endpoint. If this is a " +
					'GLM Coding Plan key, switch this model\'s provider to "GLM / Z.ai (Coding Plan)" ' +
					"(base URL https://api.z.ai/api/coding/paas/v4) instead.",
			};
		}
		return {
			level: "warn",
			label: detail ? `Not working: ${truncate(detail, 46)}` : "Not working",
			title: detail
				? `This model failed a test request: ${truncate(detail, 220)}`
				: "This model failed a test request - check its API key, model id, and base URL. " +
				  "It's skipped during failover until it passes.",
		};
	}
	if (status === "unchecked") {
		return {
			level: "unchecked",
			label: "Not verified yet",
			title: "We couldn't confirm this model - it will be re-checked on the next apply.",
		};
	}
	return { level: "ok" };
}

// Map the fleet's pool-wide subscription-probe verdict (sync.subscription_status) to a
// dot health + label + title. Shared by the failover-list account row (LlmPoolEditor's
// !singleMode accountHealth) and onboarding's single-account picker - both read the
// exact same field; disambiguation of WHICH row it describes happens in the caller
// (the failover list only attributes it when there is exactly one subscription row;
// onboarding always has exactly one, so it skips that check).
//
// `knownGood` decides what "no verdict at all" (status is "not_applicable", "", or
// undefined) degrades to:
//   - true  (the failover-list editor): a previously-saved, working pool that a
//     pre-1.11 fleet just didn't report on - quiet green, same as before this fix.
//   - false (onboarding): "no verdict yet" almost always means the account was JUST
//     connected and nothing has actually probed it - painting THAT green is exactly
//     how an out-of-quota account got shown "Account connected" before anyone had
//     checked it (2026-07-23 trace). Green there is earned only by an explicit
//     "verified".
export function subscriptionAccountHealth(status, { knownGood = true, warningDetail = "" } = {}) {
	if (status === "unverified") {
		return {
			level: "warn",
			label: "Not accepting requests",
			title:
				warningDetail ||
				"This account rejected a test request. Reconnect to restore chat.",
		};
	}
	if (status === "verified") return { level: "ok" };
	// No verdict AT ALL - status is falsy, or the fleet's own "nothing to probe here"
	// value - is the one case allowed to degrade to knownGood's default, because it is
	// the ONLY case that can legitimately mean "an existing, previously-working pool
	// that a pre-1.11 fleet simply never reported on" (see knownGood's doc above).
	if (!status || status === "not_applicable")
		return knownGood ? { level: "ok" } : { level: "neutral" };
	// Anything else - "unchecked", or a status string this frontend enum does not
	// recognise at all (a future "rate_limited"/"expired", or a typo upstream) - must
	// fail towards "not proven" rather than "fine". Falling an UNRECOGNISED value
	// through to knownGood's default (as a bare `else` would) reintroduces the exact
	// false-positive-green bug this function was written to kill, just via a status
	// string instead of a hardcoded short-circuit: a fleet reporting a verdict this
	// frontend has never heard of would otherwise paint solid green in the
	// failover-list editor (knownGood defaults true there), for an account the backend
	// is actively flagging. Treat it identically to the known "unchecked" case instead.
	return {
		level: "unchecked",
		label: "Not verified yet",
		title: "We couldn't confirm this account is active yet. It will be re-checked on the next apply.",
	};
}

// Given the SETTLED health a row would show if the pool were clean and no apply were
// in flight (from apiKeyModelHealth/subscriptionAccountHealth above), decides what
// LlmPoolEditor's dot actually renders while the pool is dirty (unsaved edits) or a
// previous save is still being applied (sync.pending). In both cases the last probe
// result no longer describes what's about to be saved, so it can't be asserted
// verbatim - but that must NOT mean every row collapses to the same grey dot a
// never-verified row shows: a row that was settled "ok" (positively verified, or a
// previously-working pool nothing has yet contradicted) has earned better than
// looking freshly-suspect. Collapsing it into --neutral grey (the same colour the CSS
// now correctly uses for "never proven", see subscriptionAccountHealth's knownGood
// doc above) made an already-healthy account's dot flip from green to grey the
// instant the customer edited an unrelated field - reading as "this just broke" when
// nothing about THAT account changed (PR #410 review finding 2). A settled row that
// was already unproven or already flagged (anything that isn't "ok") has nothing to
// lose by staying at that same dot; only "ok" gets the softer, visibly-not-a-failure
// "pending" treatment, and it stays visibly distinct from "neutral" so the two can
// never be confused for one another.
export function dirtyAccountHealth(settled, isDirtyOrPending) {
	if (!isDirtyOrPending) return settled;
	if (settled.level !== "ok") return { level: "neutral" };
	return {
		level: "pending",
		label: "Pending re-check",
		title: "This account was verified before your latest changes. It will be re-checked once they're applied.",
	};
}
