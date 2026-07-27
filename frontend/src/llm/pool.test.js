import { test } from "node:test";
import assert from "node:assert/strict";
import {
	deriveMode,
	uniqueVendors,
	missingVendorKeys,
	presetToModels,
	buildCustomModels,
	reorder,
	validatePool,
} from "./pool.js";
import { PROVIDER_LABELS, providerLabel, providerId, seedRowsFromConfig } from "./pool.js";
import { defaultSubscriptionModel, subModelSuggestions } from "./pool.js";
import { apiKeyModelHealth, subscriptionAccountHealth, dirtyAccountHealth } from "./pool.js";
import { LOCAL_PROVIDER_IDS, effectiveApiKey } from "./pool.js";

test("defaultSubscriptionModel: falls back to built-in defaults with no catalog", () => {
	assert.equal(defaultSubscriptionModel("openai"), "gpt-5.5");
	assert.equal(defaultSubscriptionModel("google"), "gemini-2.5-pro");
	assert.equal(defaultSubscriptionModel("unknown"), "gpt-5.5");
	assert.equal(defaultSubscriptionModel(undefined), "gpt-5.5");
});

test("defaultSubscriptionModel: a catalog overrides the built-in default", () => {
	const catalog = { openai: ["gpt-9.9", "gpt-5.5"] };
	assert.equal(defaultSubscriptionModel("openai", catalog), "gpt-9.9");
	// an upstream absent from the catalog still falls back
	assert.equal(defaultSubscriptionModel("google", catalog), "gemini-2.5-pro");
});

test("subModelSuggestions: maps an API subscription_models payload to upstream keys", () => {
	const apiPayload = {
		OpenAI: ["gpt-9.9"],
		"Google Gemini": ["gemini-9.9"],
		"xAI Grok": ["grok-9.9"],
		"Kimi (Moonshot)": ["kimi-9.9"],
	};
	assert.deepEqual(subModelSuggestions(apiPayload), {
		openai: ["gpt-9.9"],
		google: ["gemini-9.9"],
		xai: ["grok-9.9"],
		kimi: ["kimi-9.9"],
	});
});

test("subModelSuggestions: empty or missing payload yields the built-in fallback", () => {
	assert.equal(subModelSuggestions({}).openai[0], "gpt-5.5");
	assert.equal(subModelSuggestions(undefined).openai[0], "gpt-5.5");
});

const LADDER = {
	key: "anthropic-resilient",
	kind: "single_vendor",
	vendors: ["anthropic"],
	models: [
		{ provider: "anthropic", model: "claude-opus-4-8", order: 0 },
		{ provider: "anthropic", model: "claude-sonnet-4-6", order: 1 },
	],
};
const TRIO = {
	key: "max-reliability",
	kind: "cross_vendor",
	vendors: ["anthropic", "openai", "gemini"],
	models: [
		{ provider: "anthropic", model: "claude-opus-4-8", order: 0 },
		{ provider: "openai", model: "gpt-5.5", order: 1 },
		{ provider: "gemini", model: "gemini-2.5-pro", order: 2 },
	],
};

test("deriveMode: 1 model & no preset => direct; else proxy", () => {
	assert.equal(deriveMode([{ provider: "openai", model: "gpt-5.5" }], null), "direct");
	assert.equal(deriveMode([{ provider: "openai", model: "gpt-5.5" }], "cost-saver"), "proxy");
	assert.equal(deriveMode([{}, {}], null), "proxy");
	assert.equal(deriveMode([], null), "direct");
});
test("deriveMode: a single subscription model is proxy (needs cliproxy)", () => {
	assert.equal(
		deriveMode([{ model: "gpt-5.5", credentialType: "subscription" }], null),
		"proxy"
	);
	assert.equal(
		deriveMode([{ model: "gpt-5.5", subscription: { accounts: [] } }], null),
		"proxy"
	);
});
test("uniqueVendors: dedup preserving order", () => {
	assert.deepEqual(uniqueVendors(LADDER), ["anthropic"]);
	assert.deepEqual(uniqueVendors(TRIO), ["anthropic", "openai", "gemini"]);
});
test("missingVendorKeys: all-or-nothing (L8)", () => {
	assert.deepEqual(missingVendorKeys(LADDER, { anthropic: "sk-a" }), []);
	assert.deepEqual(missingVendorKeys(LADDER, { anthropic: "  " }), ["anthropic"]);
	assert.deepEqual(missingVendorKeys(TRIO, { anthropic: "a", openai: "o" }), ["gemini"]);
});
test("presetToModels: one key reused per vendor, order preserved", () => {
	const models = presetToModels(TRIO, { anthropic: "sk-a", openai: "sk-o", gemini: "sk-g" });
	assert.deepEqual(
		models.map((m) => m.order),
		[0, 1, 2]
	);
	assert.equal(models[0].api_key, "sk-a");
	assert.equal(models[1].api_key, "sk-o");
	assert.equal(models[0].model, "claude-opus-4-8");
});
test("buildCustomModels: order = row index; trims; drops incomplete rows", () => {
	const rows = [
		{ provider: "openai", model: "gpt-5.5", apiKey: "sk-o" },
		{ provider: "mistral", model: "mistral-large-latest", apiKey: "sk-m" },
		{ provider: "", model: "", apiKey: "" },
	];
	const models = buildCustomModels(rows);
	assert.deepEqual(
		models.map((m) => m.order),
		[0, 1]
	);
	assert.equal(models[0].api_key, "sk-o");
});
test("buildCustomModels: Ollama/vLLM row with no typed key gets the 'local' placeholder", () => {
	const rows = [
		{ provider: "Ollama (local)", model: "llama3", apiKey: "", baseUrl: "" },
		{ provider: "openai", model: "gpt-5.5", apiKey: "sk-o" },
	];
	const models = buildCustomModels(rows);
	assert.equal(models[0].api_key, "local");
	assert.equal(models[1].api_key, "sk-o");
});
test("buildCustomModels: emits base_url when present, omits when blank", () => {
	const rows = [
		{
			provider: "openai_compat",
			model: "qwen2.5:3b",
			apiKey: "ollama",
			baseUrl: "http://host.docker.internal:11434/v1",
		},
		{ provider: "openai", model: "gpt-5.5", apiKey: "sk-o" },
	];
	const models = buildCustomModels(rows);
	assert.equal(models[0].base_url, "http://host.docker.internal:11434/v1");
	assert.equal("base_url" in models[1], false);
});
test("reorder: pure move", () => {
	assert.deepEqual(reorder(["a", "b", "c"], 2, 0), ["c", "a", "b"]);
});
test("validatePool: rejects empty pool", () => {
	assert.equal(validatePool([], null).ok, false);
	assert.equal(
		validatePool([{ provider: "openai", model: "gpt-5.5", api_key: "k" }], null).ok,
		true
	);
});
test("validatePool: subscription model valid with a connected account (no provider/api_key)", () => {
	const sub = {
		model: "gpt-5.5",
		order: 0,
		subscription: {
			rotation: "sticky",
			accounts: [
				{
					upstream: "openai",
					account_ref: "SUB_abc123",
					label: "me@x.com",
					oauth_blob: '{"token":"t"}',
				},
			],
		},
	};
	assert.equal(validatePool([sub], null).ok, true);
});
test("validatePool: subscription model invalid with no accounts", () => {
	const sub = { model: "gpt-5.5", order: 0, subscription: { rotation: "sticky", accounts: [] } };
	assert.equal(validatePool([sub], null).ok, false);
});
test("validatePool: subscription account with account_ref but blank blob is valid (previously connected; blob merged back on save)", () => {
	const sub = {
		model: "gpt-5.5",
		order: 0,
		subscription: {
			rotation: "sticky",
			accounts: [
				{
					upstream: "openai",
					account_ref: "SUB_abc123",
					label: "me@x.com",
					oauth_blob: "",
				},
			],
		},
	};
	assert.equal(validatePool([sub], null).ok, true);
});
test("validatePool: subscription account with neither blob nor account_ref is invalid (never connected)", () => {
	const sub = {
		model: "gpt-5.5",
		order: 0,
		subscription: {
			rotation: "sticky",
			accounts: [{ upstream: "openai", account_ref: "", label: "", oauth_blob: "" }],
		},
	};
	assert.equal(validatePool([sub], null).ok, false);
});
test("validatePool: api_key model with blank key but has_key is valid (key preserved on save)", () => {
	assert.equal(
		validatePool([{ provider: "openai", model: "gpt-5.5", api_key: "", has_key: true }], null)
			.ok,
		true
	);
});
test("validatePool: api_key model with neither key nor has_key is invalid", () => {
	assert.equal(
		validatePool([{ provider: "openai", model: "gpt-5.5", api_key: "" }], null).ok,
		false
	);
});
test("validatePool: Ollama/vLLM don't need a key (label or id, blank api_key)", () => {
	assert.equal(
		validatePool([{ provider: "Ollama (local)", model: "llama3", api_key: "" }], null).ok,
		true
	);
	assert.equal(
		validatePool(
			[
				{
					provider: "vllm",
					model: "qwen2.5",
					api_key: "",
					base_url: "http://localhost:8000/v1",
				},
			],
			null
		).ok,
		true
	);
	// vLLM still needs its base_url - only the key is optional.
	assert.equal(
		validatePool([{ provider: "vllm", model: "qwen2.5", api_key: "" }], null).ok,
		false
	);
	// A normal provider is unaffected.
	assert.equal(
		validatePool([{ provider: "openai", model: "gpt-5.5", api_key: "" }], null).ok,
		false
	);
});
test("effectiveApiKey: local providers get a placeholder only when blank and unsaved", () => {
	assert.equal(effectiveApiKey("Ollama (local)", "", false), "local");
	assert.equal(effectiveApiKey("vllm", "", false), "local");
	// A typed key always wins.
	assert.equal(effectiveApiKey("ollama", "sk-real", false), "sk-real");
	// An already-saved key (has_key) is left blank so the backend keeps it.
	assert.equal(effectiveApiKey("ollama", "", true), "");
	// A non-local provider stays blank (validatePool rejects it separately).
	assert.equal(effectiveApiKey("openai", "", false), "");
});
test("LOCAL_PROVIDER_IDS: exactly ollama + vllm", () => {
	assert.deepEqual([...LOCAL_PROVIDER_IDS].sort(), ["ollama", "vllm"]);
});
test("validatePool: OpenAI-Compatible / vLLM require a base_url (label or id)", () => {
	// Claude-CLI shim path: provider set, model + key present, but no base_url.
	assert.equal(
		validatePool([{ provider: "OpenAI-Compatible", model: "claude", api_key: "k" }], null).ok,
		false
	);
	assert.equal(
		validatePool([{ provider: "openai_compat", model: "claude", api_key: "k" }], null).ok,
		false
	);
	assert.equal(
		validatePool([{ provider: "vLLM (local)", model: "qwen", api_key: "k" }], null).ok,
		false
	);
	// With a base_url it passes.
	assert.equal(
		validatePool(
			[
				{
					provider: "OpenAI-Compatible",
					model: "claude",
					api_key: "k",
					base_url: "http://host.docker.internal:9000/openai/v1",
				},
			],
			null
		).ok,
		true
	);
	// A normal provider is unaffected (has a default endpoint).
	assert.equal(
		validatePool([{ provider: "openai", model: "gpt-5.5", api_key: "k" }], null).ok,
		true
	);
});

test("providerLabel/providerId: id⇄label round-trip for openai_compat", () => {
	assert.equal(providerLabel("openai_compat"), "OpenAI-Compatible");
	assert.equal(providerId("OpenAI-Compatible"), "openai_compat");
	// unknown id passes through unchanged (no crash)
	assert.equal(providerLabel("weird"), "weird");
});
test("providerLabel/providerId: gemini id ⇄ Google Gemini label (matches catalog id, not legacy 'google')", () => {
	assert.equal(providerLabel("gemini"), "Google Gemini");
	assert.equal(providerId("Google Gemini"), "gemini");
});
test("providerLabel/providerId: zai id ⇄ GLM / Z.ai label (first-class provider, distinct from openai_compat)", () => {
	// Regression lock: "zai" must render as its own label, never fall back to
	// "OpenAI-Compatible" - that fallback only happens for an id absent from
	// PROVIDER_LABELS, and zai has been a first-class entry since #319.
	assert.equal(providerLabel("zai"), "GLM / Z.ai");
	assert.equal(providerId("GLM / Z.ai"), "zai");
	assert.notEqual(providerLabel("zai"), providerLabel("openai_compat"));
});
test("PROVIDER_LABELS: includes the vendors + compat, each {id,label}", () => {
	const ids = PROVIDER_LABELS.map((p) => p.id);
	assert.ok(ids.includes("openai"));
	assert.ok(ids.includes("anthropic"));
	assert.ok(ids.includes("openai_compat"));
	assert.ok(PROVIDER_LABELS.every((p) => p.id && p.label));
});
test("seedRowsFromConfig: api-key model → api_key row with label provider + hasKey", () => {
	const cfg = {
		models: [
			{
				provider: "openai_compat",
				model: "claude-sonnet-4-6",
				api_key: "set",
				base_url: "http://h:9000/openai",
				order: 0,
			},
		],
	};
	const [row] = seedRowsFromConfig(cfg);
	assert.equal(row.credentialType, "api_key");
	assert.equal(row.provider, "OpenAI-Compatible");
	assert.equal(row.model, "claude-sonnet-4-6");
	assert.equal(row.baseUrl, "http://h:9000/openai");
	assert.equal(row.apiKey, ""); // keys never returned to client
	assert.equal(row.hasKey, true); // but we know one is set → placeholder
});
test("seedRowsFromConfig: GLM/Z.ai model stored as first-class 'zai' renders its own label, not OpenAI-Compatible", () => {
	// End-to-end regression lock for the storage-collapsing bug: a row that
	// survives jarvis.onboarding.save_llm_pool -> get_llm_config as provider
	// "zai" (the post-fix stored shape) must seed an editor row labeled
	// "GLM / Z.ai" - not "OpenAI-Compatible", which is what a collapsed
	// "openai_compat" row would render as.
	const cfg = {
		models: [
			{
				provider: "zai",
				model: "glm-4.6",
				credential_type: "api_key",
				has_key: true,
				base_url: "https://api.z.ai/api/paas/v4",
				order: 0,
			},
		],
	};
	const [row] = seedRowsFromConfig(cfg);
	assert.equal(row.credentialType, "api_key");
	assert.equal(row.provider, "GLM / Z.ai");
	assert.equal(row.model, "glm-4.6");
	assert.equal(row.baseUrl, "https://api.z.ai/api/paas/v4");
	assert.equal(row.hasKey, true);
});
test("seedRowsFromConfig: subscription model → subscription row with connected accounts", () => {
	const cfg = {
		models: [
			{
				model: "gpt-5.5",
				order: 1,
				subscription: {
					rotation: "sticky",
					accounts: [{ upstream: "openai", account_ref: "SUB_x", label: "me@x" }],
				},
			},
		],
	};
	const [row] = seedRowsFromConfig(cfg);
	assert.equal(row.credentialType, "subscription");
	assert.equal(row.rotation, "sticky");
	assert.equal(row.accounts.length, 1);
	assert.equal(row.accounts[0].connected, true);
	assert.equal(row.accounts[0].account_ref, "SUB_x");
});
test("seedRowsFromConfig: empty/absent → empty array", () => {
	assert.deepEqual(seedRowsFromConfig(null), []);
	assert.deepEqual(seedRowsFromConfig({ models: [] }), []);
});

// ---- real jarvis.onboarding.get_llm_config shape (credential_type/has_key,
// flat rotation+accounts - NOT the fixture's nested `subscription` object) --
test("seedRowsFromConfig: real get_llm_config shape - api_key model", () => {
	const cfg = {
		models: [
			{
				provider: "openai",
				model: "gpt-4o",
				credential_type: "api_key",
				has_key: true,
				base_url: "",
				order: 0,
			},
		],
	};
	const [row] = seedRowsFromConfig(cfg);
	assert.equal(row.credentialType, "api_key");
	assert.equal(row.provider, "OpenAI");
	assert.equal(row.model, "gpt-4o");
	assert.equal(row.apiKey, "");
	assert.equal(row.hasKey, true);
});
test("seedRowsFromConfig: real get_llm_config shape - subscription model (flat rotation+accounts)", () => {
	const cfg = {
		models: [
			{
				model: "gpt-5.5",
				credential_type: "subscription",
				rotation: "sticky",
				accounts: [{ upstream: "openai", account_ref: "SUB_x", label: "me@x" }],
				order: 1,
			},
		],
	};
	const [row] = seedRowsFromConfig(cfg);
	assert.equal(row.credentialType, "subscription");
	assert.equal(row.rotation, "sticky");
	assert.equal(row.accounts.length, 1);
	assert.equal(row.accounts[0].connected, true);
	assert.equal(row.accounts[0].account_ref, "SUB_x");
});

// ---- apiKeyModelHealth: per-row health from the fleet's model_statuses (contract 1.11) ----
// Before this, api-key rows were hardcoded to a green dot + presence-only "key set", so a
// model with a dead base_url / bad key showed identically to a healthy one. These pin that a
// per-model verdict now drives the row.

const _apiRow = (over = {}) => ({
	credentialType: "api_key",
	provider: providerLabel("openai_compat"),
	model: "claude-sonnet-4-6",
	hasKey: true,
	...over,
});

test("apiKeyModelHealth: a failed model surfaces a warn state with actionable title", () => {
	const ms = [{ provider: "openai_compat", model: "claude-sonnet-4-6", status: "failed" }];
	const h = apiKeyModelHealth(_apiRow(), ms);
	assert.equal(h.level, "warn");
	assert.ok(h.label, "a failed row must carry a label, not just a bare dot");
	assert.match(h.title, /base URL|API key|model id/i);
});

test("apiKeyModelHealth: verified is a MEANINGFUL green (ok, no label)", () => {
	const ms = [{ provider: "openai_compat", model: "claude-sonnet-4-6", status: "verified" }];
	assert.deepEqual(apiKeyModelHealth(_apiRow(), ms), { level: "ok" });
});

test("apiKeyModelHealth: unchecked reads as a neutral 'not verified yet'", () => {
	const ms = [{ provider: "openai_compat", model: "claude-sonnet-4-6", status: "unchecked" }];
	const h = apiKeyModelHealth(_apiRow(), ms);
	assert.equal(h.level, "unchecked");
	assert.ok(h.label);
});

test("apiKeyModelHealth: no verdict for the row -> quiet green (pre-1.11 fleet / not probed)", () => {
	assert.deepEqual(apiKeyModelHealth(_apiRow(), []), { level: "ok" });
	assert.deepEqual(apiKeyModelHealth(_apiRow(), undefined), { level: "ok" });
	assert.deepEqual(
		apiKeyModelHealth(_apiRow(), [{ provider: "x", model: "other", status: "failed" }]),
		{ level: "ok" }
	);
});

test("apiKeyModelHealth: a model-id collision is disambiguated by provider", () => {
	// validate() allows the same model id under two providers (only provider/model PAIRS
	// must be unique), so the verdict must attach to the RIGHT row. Rows store the provider
	// LABEL, model_statuses store the id -> the match must normalize.
	const ms = [
		{ provider: "openai", model: "gpt-4o", status: "verified" },
		{ provider: "openai_compat", model: "gpt-4o", status: "failed" },
	];
	assert.equal(
		apiKeyModelHealth(
			_apiRow({ provider: providerLabel("openai_compat"), model: "gpt-4o" }),
			ms
		).level,
		"warn"
	);
	assert.equal(
		apiKeyModelHealth(_apiRow({ provider: providerLabel("openai"), model: "gpt-4o" }), ms)
			.level,
		"ok"
	);
});

test("apiKeyModelHealth: a SINGLE entry under a DIFFERENT provider is not misattributed", () => {
	// Only one model_statuses entry carries this model id, but it belongs to another
	// provider than the row. The row was never probed under its own provider, so it must
	// stay quiet green -- NOT inherit the other provider's 'failed'. (Model id is not a key:
	// validate() only forbids duplicate provider/model PAIRS.)
	const ms = [{ provider: "openai_compat", model: "gpt-4o", status: "failed" }];
	assert.deepEqual(
		apiKeyModelHealth(_apiRow({ provider: providerLabel("openai"), model: "gpt-4o" }), ms),
		{ level: "ok" }
	);
});

// ---- apiKeyModelHealth: consuming the OPTIONAL `detail` field (fleet-agent contract 1.12)
// DEFENSIVELY - must render the real reason when present, and fall back to today's generic
// text unchanged when it's absent (an older fleet-agent). This is the GLM/Z.ai
// insufficient-balance case: "Not working" alone couldn't distinguish a bad key from an
// unpaid account; the provider's own message can. ----

test("apiKeyModelHealth: detail present -> surfaced in both label and title", () => {
	const ms = [
		{
			provider: "openai_compat",
			model: "glm-4.6",
			status: "failed",
			detail: "Insufficient balance or no resource package. Please recharge.",
		},
	];
	const h = apiKeyModelHealth(_apiRow({ model: "glm-4.6" }), ms);
	assert.equal(h.level, "warn");
	assert.match(h.label, /Insufficient balance/);
	assert.match(h.title, /Insufficient balance/);
	assert.match(h.title, /Please recharge/);
});

test("apiKeyModelHealth: detail ABSENT falls back to today's generic text (older fleet-agent)", () => {
	// No `detail` key at all on the entry - the exact shape a pre-1.12 fleet-agent sends.
	// Must behave identically to before this feature existed.
	const ms = [{ provider: "openai_compat", model: "claude-sonnet-4-6", status: "failed" }];
	const h = apiKeyModelHealth(_apiRow(), ms);
	assert.equal(h.level, "warn");
	assert.equal(h.label, "Not working");
	assert.match(h.title, /base URL|API key|model id/i);
});

test("apiKeyModelHealth: blank/whitespace-only detail also falls back to the generic text", () => {
	const ms = [
		{ provider: "openai_compat", model: "claude-sonnet-4-6", status: "failed", detail: "   " },
	];
	const h = apiKeyModelHealth(_apiRow(), ms);
	assert.equal(h.label, "Not working");
});

test("apiKeyModelHealth: a non-string detail (defensive) is ignored, not rendered", () => {
	const ms = [
		{ provider: "openai_compat", model: "claude-sonnet-4-6", status: "failed", detail: 12345 },
	];
	const h = apiKeyModelHealth(_apiRow(), ms);
	assert.equal(h.label, "Not working");
});

test("apiKeyModelHealth: detail on a non-failed status is never rendered (verified stays quiet green)", () => {
	const ms = [
		{
			provider: "openai_compat",
			model: "claude-sonnet-4-6",
			status: "verified",
			detail: "should never surface",
		},
	];
	assert.deepEqual(apiKeyModelHealth(_apiRow(), ms), { level: "ok" });
});

test("apiKeyModelHealth: a long detail is truncated in the label but stays fuller in the title", () => {
	const long = "x".repeat(500);
	const ms = [
		{ provider: "openai_compat", model: "claude-sonnet-4-6", status: "failed", detail: long },
	];
	const h = apiKeyModelHealth(_apiRow(), ms);
	assert.ok(h.label.length < 80, `label should be short, got ${h.label.length} chars`);
	assert.ok(h.title.length < 260, `title should still be capped, got ${h.title.length} chars`);
});
// ---- GLM Coding Plan ("zai_coding"): a distinct provider from pay-as-you-go "zai" ----
// Live discovery: a Coding Plan key authenticates fine but reports "insufficient balance"
// (z.ai error code 1113) on the pay-as-you-go endpoint (api.z.ai/api/paas/v4) even though
// it's perfectly valid on the coding-plan endpoint (api.z.ai/api/coding/paas/v4). This
// section covers the new provider option and the targeted diagnostic hint for that trap.

test("providerLabel/providerId: zai_coding id ⇄ 'GLM / Z.ai (Coding Plan)' label, distinct from zai", () => {
	assert.equal(providerLabel("zai_coding"), "GLM / Z.ai (Coding Plan)");
	assert.equal(providerId("GLM / Z.ai (Coding Plan)"), "zai_coding");
	assert.notEqual(providerLabel("zai_coding"), providerLabel("zai"));
});

test("PROVIDER_LABELS: zai_coding is a first-class entry", () => {
	const ids = PROVIDER_LABELS.map((p) => p.id);
	assert.ok(ids.includes("zai_coding"));
});

test("validatePool: GLM / Z.ai (Coding Plan) requires a base_url, same as GLM / Z.ai", () => {
	assert.equal(
		validatePool(
			[{ provider: "GLM / Z.ai (Coding Plan)", model: "glm-4.6", api_key: "k" }],
			null
		).ok,
		false
	);
	assert.equal(
		validatePool(
			[
				{
					provider: "GLM / Z.ai (Coding Plan)",
					model: "glm-4.6",
					api_key: "k",
					base_url: "https://api.z.ai/api/coding/paas/v4",
				},
			],
			null
		).ok,
		true
	);
});

test("seedRowsFromConfig: a row stored as 'zai_coding' renders its own distinct label", () => {
	const cfg = {
		models: [
			{
				provider: "zai_coding",
				model: "glm-4.6",
				credential_type: "api_key",
				has_key: true,
				base_url: "https://api.z.ai/api/coding/paas/v4",
				order: 0,
			},
		],
	};
	const [row] = seedRowsFromConfig(cfg);
	assert.equal(row.provider, "GLM / Z.ai (Coding Plan)");
	assert.equal(row.baseUrl, "https://api.z.ai/api/coding/paas/v4");
});

const _zaiRow = (over = {}) => ({
	credentialType: "api_key",
	provider: providerLabel("zai"),
	model: "glm-4.6",
	hasKey: true,
	...over,
});

test("apiKeyModelHealth: a 'zai' row failing with z.ai's insufficient-balance (1113) detail gets the coding-plan hint", () => {
	const ms = [
		{
			provider: "zai",
			model: "glm-4.6",
			status: "failed",
			detail: '{"error":{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}}',
		},
	];
	const h = apiKeyModelHealth(_zaiRow(), ms);
	assert.equal(h.level, "warn");
	assert.match(h.label, /endpoint/i);
	assert.match(h.title, /coding plan/i);
	assert.match(h.title, /api\.z\.ai\/api\/coding\/paas\/v4/);
});

test("apiKeyModelHealth: the hint also matches on the plain 'insufficient balance' phrase (no code substring)", () => {
	const ms = [
		{
			provider: "zai",
			model: "glm-4.6",
			status: "failed",
			detail: "Insufficient balance. Please recharge.",
		},
	];
	assert.match(apiKeyModelHealth(_zaiRow(), ms).title, /coding plan/i);
});

test("apiKeyModelHealth: a 'zai' failure with a DIFFERENT detail falls through to the generic message", () => {
	const ms = [
		{ provider: "zai", model: "glm-4.6", status: "failed", detail: "invalid api key" },
	];
	const h = apiKeyModelHealth(_zaiRow(), ms);
	assert.equal(h.level, "warn");
	// Not the coding-plan hint - this detail isn't the 1113 trap. It falls through to the
	// generic branch, which (contract 1.12) renders the provider's own reason.
	assert.doesNotMatch(h.label, /endpoint/i);
	assert.doesNotMatch(h.title, /coding plan/i);
	assert.match(h.label, /invalid api key/);
});

test("apiKeyModelHealth: a 'zai' failure with NO detail (pre-1.12 fleet) falls through defensively, never throws", () => {
	const ms = [{ provider: "zai", model: "glm-4.6", status: "failed" }];
	const h = apiKeyModelHealth(_zaiRow(), ms);
	assert.equal(h.level, "warn");
	assert.equal(h.label, "Not working");
});

test("apiKeyModelHealth: the same 1113 detail on a 'zai_coding' row does NOT trigger the hint (already the right endpoint)", () => {
	const ms = [
		{
			provider: "zai_coding",
			model: "glm-4.6",
			status: "failed",
			detail: "Insufficient balance or no resource package. Please recharge.",
		},
	];
	const h = apiKeyModelHealth(_zaiRow({ provider: providerLabel("zai_coding") }), ms);
	assert.equal(h.level, "warn");
	// A coding-plan row is already on the right endpoint, so the same 1113 text is a REAL
	// balance problem: report it verbatim, never redirect the customer to an endpoint they
	// are already using.
	assert.doesNotMatch(h.label, /endpoint/i);
	assert.doesNotMatch(h.title, /coding plan/i);
	assert.match(h.label, /Insufficient balance/);
});

test("apiKeyModelHealth: the same 1113 detail on an UNRELATED provider does NOT trigger the hint", () => {
	const ms = [
		{
			provider: "openai_compat",
			model: "gpt-4o",
			status: "failed",
			detail: "Insufficient balance or no resource package. Please recharge.",
		},
	];
	const h = apiKeyModelHealth(
		_apiRow({ provider: providerLabel("openai_compat"), model: "gpt-4o" }),
		ms
	);
	assert.equal(h.level, "warn");
	// z.ai-specific advice must never leak onto another provider's row, even on identical text.
	assert.doesNotMatch(h.label, /endpoint/i);
	assert.doesNotMatch(h.title, /coding plan/i);
	assert.match(h.label, /Insufficient balance/);
});

// ---- subscriptionAccountHealth: honest dot health for a connected chat-subscription
// account (contract: sync.subscription_status). Before this, LlmPoolEditor's onboarding
// (singleMode) picker hardcoded EVERY connected account to {level:"neutral"} unconditionally,
// and the CSS painted "neutral" the same green as a positively-verified "ok" - so a
// FREE-tier account that was out of quota rendered exactly like a healthy one
// (2026-07-23 trace). These pin the real-signal mapping both call sites now share. ----

test("subscriptionAccountHealth: unverified is a warn state with an actionable title", () => {
	const h = subscriptionAccountHealth("unverified");
	assert.equal(h.level, "warn");
	assert.equal(h.label, "Not accepting requests");
	assert.match(h.title, /reconnect/i);
});

test("subscriptionAccountHealth: unverified prefers the fleet's own warning message", () => {
	const h = subscriptionAccountHealth("unverified", { warningDetail: "429: rate limited" });
	assert.equal(h.title, "429: rate limited");
});

test("subscriptionAccountHealth: unchecked reads as a neutral 'not verified yet'", () => {
	const h = subscriptionAccountHealth("unchecked");
	assert.equal(h.level, "unchecked");
	assert.equal(h.label, "Not verified yet");
});

test("subscriptionAccountHealth: verified is a MEANINGFUL green (ok, no label) regardless of knownGood", () => {
	assert.deepEqual(subscriptionAccountHealth("verified"), { level: "ok" });
	assert.deepEqual(subscriptionAccountHealth("verified", { knownGood: false }), { level: "ok" });
});

test("subscriptionAccountHealth: no verdict at all -> quiet green ONLY when knownGood (failover-list default)", () => {
	assert.deepEqual(subscriptionAccountHealth(""), { level: "ok" });
	assert.deepEqual(subscriptionAccountHealth(undefined), { level: "ok" });
	assert.deepEqual(subscriptionAccountHealth("not_applicable"), { level: "ok" });
});

test("subscriptionAccountHealth: no verdict at all -> neutral (grey), never green, when knownGood is false", () => {
	// This is the onboarding case: right after OAuth paste-back, before Start chatting
	// even runs save_llm_pool, nothing has actually probed the account yet. Green here
	// is exactly the defect this test pins against a regression.
	const opts = { knownGood: false };
	assert.deepEqual(subscriptionAccountHealth("", opts), { level: "neutral" });
	assert.deepEqual(subscriptionAccountHealth(undefined, opts), { level: "neutral" });
	assert.deepEqual(subscriptionAccountHealth("not_applicable", opts), { level: "neutral" });
});

// ---- subscriptionAccountHealth: an UNRECOGNISED status must never read as healthy
// (review finding #1 on PR #410). "unverified"/"unchecked"/"verified"/falsy/
// "not_applicable" are the only strings this function knows about - anything else (a
// future fleet verdict this frontend enum hasn't caught up to yet, or a typo upstream)
// used to fall through to knownGood's default, painting solid green in the
// failover-list editor (knownGood defaults true there) for an account the backend is
// actively reporting a problem on. That is the exact false-positive-green defect this
// whole PR exists to kill, reintroduced through an unrecognised string instead of the
// old hardcoded singleMode short-circuit. An unknown verdict must fail towards "not
// proven", the same as "unchecked". ----

test("subscriptionAccountHealth: an unrecognised status is treated as unchecked, NEVER green, regardless of knownGood", () => {
	const known = subscriptionAccountHealth("unchecked");
	assert.deepEqual(subscriptionAccountHealth("rate_limited"), known);
	assert.deepEqual(subscriptionAccountHealth("rate_limited", { knownGood: true }), known);
	assert.deepEqual(subscriptionAccountHealth("rate_limited", { knownGood: false }), known);
	// A typo'd/legacy verdict string behaves identically - the enum is closed, not the
	// set of strings a real or misbehaving fleet might ever send.
	assert.deepEqual(subscriptionAccountHealth("Unverified"), known);
});

test("subscriptionAccountHealth: an unrecognised status is never 'ok' even in the failover-list editor (knownGood defaults true)", () => {
	// This is the exact scenario the finding describes: the failover-list editor calls
	// subscriptionAccountHealth with its default options (knownGood defaults true), so
	// the regression would show here first.
	assert.notEqual(subscriptionAccountHealth("expired").level, "ok");
});

// ---- dirtyAccountHealth: what LlmPoolEditor's dot shows once the pool is dirty
// (unsaved edits) or a save is still being applied (sync.pending) - review finding #2
// on PR #410. Repointing --neutral's CSS from green to grey (the fix above, for the
// real never-verified case) had a side effect: LlmPoolEditor's accountHealth()
// unconditionally forced EVERY row to {level:"neutral"} while dirty/pending, so an
// already-healthy, previously-verified account's dot flipped from green to grey the
// moment the customer edited an unrelated field - reading as "this just broke" when
// nothing about that account's health changed. These pin that a settled "ok" gets its
// own "pending" treatment instead, distinct from both "ok" (still asserting current
// health) and "neutral" (never proven at all), while anything that was already
// unproven or already flagged keeps landing on the same neutral dot as before. ----

test("dirtyAccountHealth: not dirty/pending -> passes the settled health through unchanged", () => {
	assert.deepEqual(dirtyAccountHealth({ level: "ok" }, false), { level: "ok" });
	assert.deepEqual(dirtyAccountHealth({ level: "warn", label: "Not working" }, false), {
		level: "warn",
		label: "Not working",
	});
});

test("dirtyAccountHealth: a settled 'ok' row goes to 'pending' (not 'neutral') while dirty - it was never broken", () => {
	const h = dirtyAccountHealth({ level: "ok" }, true);
	assert.equal(h.level, "pending");
	assert.notEqual(h.level, "neutral");
	assert.ok(h.label, "pending must carry a label explaining why the dot isn't plain green");
	assert.match(h.title, /verified/i);
});

test("dirtyAccountHealth: a settled 'warn'/'unchecked'/'neutral' row stays 'neutral' while dirty - nothing to lose, unlike a settled 'ok'", () => {
	assert.deepEqual(dirtyAccountHealth({ level: "warn", label: "Not working" }, true), {
		level: "neutral",
	});
	assert.deepEqual(dirtyAccountHealth({ level: "unchecked", label: "Not verified yet" }, true), {
		level: "neutral",
	});
	assert.deepEqual(dirtyAccountHealth({ level: "neutral" }, true), { level: "neutral" });
});

test("dirtyAccountHealth: 'pending' and 'neutral' are visibly distinct levels (regression lock)", () => {
	// The whole point of this fix is that these two must NOT collapse into one
	// customer-visible state again - assert the levels differ, not just that both exist.
	const wasHealthy = dirtyAccountHealth({ level: "ok" }, true);
	const neverProven = dirtyAccountHealth({ level: "neutral" }, true);
	assert.notEqual(wasHealthy.level, neverProven.level);
});
