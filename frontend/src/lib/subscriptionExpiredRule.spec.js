import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
	turnErrorInfo,
	subscriptionUpstreamFromError,
	SUBSCRIPTION_LABELS,
} from "../../../jarvis/public/js/turn_errors.mjs";
import rules from "../../../jarvis/public/js/turn_error_rules.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const fixtures = (name) =>
	JSON.parse(readFileSync(path.join(HERE, "../../../jarvis/tests/fixtures", name), "utf8"));
const upstreamCases = fixtures("subscription_upstreams.json");

// The real production string (fleet PR #299, prod 2026-10-01).
const PROD = upstreamCases[0].error;
const ALL_MODELS_FAILED = upstreamCases[1].error;
const CLAUDE_LOGIN = upstreamCases[3].error;
const KIMI = upstreamCases[5].error;

describe("subscription-expired rule", () => {
	it("sits before models-exhausted so an `All models failed` turn does not fall to the generic rule", () => {
		const codes = rules.map((r) => r.code);
		expect(codes.indexOf("subscription-expired")).toBeGreaterThan(-1);
		expect(codes.indexOf("subscription-expired")).toBeLessThan(
			codes.indexOf("models-exhausted")
		);
		expect(codes.indexOf("subscription-expired")).toBeLessThan(
			codes.indexOf("authentication")
		);
	});

	it("reads the real prod string as an expired OpenAI sign-in", () => {
		const info = turnErrorInfo(PROD);
		expect(info.code).toBe("subscription-expired");
		expect(info.headline).toBe("OpenAI sign-in expired");
		expect(info.action).toBe("reconnect");
		expect(info.actionLabel).toBe("Reconnect OpenAI");
		expect(info.upstream).toBe("openai");
		expect(info.retryable).toBe(false);
		expect(info.headline).not.toContain("{provider}");
	});

	it("beats models-exhausted for the `All models failed (2)` form and names the failing upstream", () => {
		const info = turnErrorInfo(ALL_MODELS_FAILED);
		expect(info.code).toBe("subscription-expired");
		expect(info.upstream).toBe("openai");
	});

	it("reads a Claude Code 401 as Anthropic", () => {
		const info = turnErrorInfo(CLAUDE_LOGIN);
		expect(info.code).toBe("subscription-expired");
		expect(info.headline).toBe("Anthropic sign-in expired");
	});

	it("reads a kimi invalid_grant that names providers=kimi", () => {
		const info = turnErrorInfo(KIMI);
		expect(info.code).toBe("subscription-expired");
		expect(info.headline).toBe("Kimi (Moonshot) sign-in expired");
	});

	it("keeps a plain 401 from an api-key provider as authentication", () => {
		const info = turnErrorInfo(
			"OpenAI API error: 401 Unauthorized. Incorrect API key provided: sk-xxxx"
		);
		expect(info.code).toBe("authentication");
		expect(info.action).toBeUndefined();
	});

	it("keeps an MCP-shaped invalid_grant with no upstream named as authentication", () => {
		expect(turnErrorInfo("MCP connector google_drive failed: invalid_grant").code).toBe(
			"authentication"
		);
		expect(turnErrorInfo("OAuth token has expired").code).toBe("authentication");
	});

	it("leaves the old `All models failed: Anthropic 401 ...` string on models-exhausted", () => {
		expect(
			turnErrorInfo(
				"All models failed: Anthropic 401 Unauthorized; OpenAI 503 Service Unavailable"
			).code
		).toBe("models-exhausted");
	});

	it("an upstream-less explicit code degrades to authentication and never prints {provider}", () => {
		const info = turnErrorInfo("x", "subscription-expired");
		expect(info.code).toBe("authentication");
		expect(info.headline).not.toContain("{provider}");
	});
});

describe("subscriptionUpstreams option (workspace has no row for that upstream)", () => {
	it("falls back to the authentication rule when the list does not include the upstream", () => {
		for (const list of [[], ["anthropic"]]) {
			const info = turnErrorInfo(PROD, "", { subscriptionUpstreams: list });
			expect(info.code).toBe("authentication");
			expect(info.action).toBeUndefined();
		}
	});

	it("keeps the rule when the list includes it, or when no list is passed", () => {
		expect(turnErrorInfo(PROD, "", { subscriptionUpstreams: ["openai"] }).code).toBe(
			"subscription-expired"
		);
		expect(turnErrorInfo(PROD, "", {}).code).toBe("subscription-expired");
	});

	it("applies to a server-published explicit code too", () => {
		const info = turnErrorInfo(PROD, "subscription-expired", {
			subscriptionUpstreams: ["xai"],
		});
		expect(info.code).toBe("authentication");
	});
});

describe("hints by audience and provider fallback", () => {
	it("an admin is sent to AI models, a member to their admin", () => {
		expect(turnErrorInfo(PROD, "", { admin: true }).hint).toBe(
			"Reconnect it in AI models, then send again."
		);
		expect(turnErrorInfo(PROD, "", { admin: false }).hint).toBe(
			"Ask your workspace admin to reconnect it, then send again."
		);
		expect(turnErrorInfo(PROD).hint).toBe(
			"Ask your workspace admin to reconnect it, then send again."
		);
	});

	it("falls back to the message's provider when the text names no upstream", () => {
		const text = "auth_unavailable: no auth available; last upstream error: unauthorized";
		expect(turnErrorInfo(text).code).toBe("authentication");
		const info = turnErrorInfo(text, "", { provider: "openai-codex" });
		expect(info.code).toBe("subscription-expired");
		expect(info.upstream).toBe("openai");
	});
});

describe("subscriptionUpstreamFromError", () => {
	for (const { error, upstream } of upstreamCases) {
		it(`maps ${JSON.stringify(error.slice(0, 60))} to ${upstream || "(none)"}`, () => {
			expect(subscriptionUpstreamFromError(error)).toBe(upstream);
		});
	}

	it("uses the app's own labels", () => {
		expect(SUBSCRIPTION_LABELS).toEqual({
			openai: "OpenAI",
			anthropic: "Anthropic",
			xai: "xAI Grok",
			kimi: "Kimi (Moonshot)",
		});
	});
});
