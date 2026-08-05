import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, expect, vi, beforeEach } from "vitest";

// readiness.js reaches the backend through @/api.js, whose real module pulls in
// frappe-ui. The factory form of vi.mock keeps that module out of the graph
// entirely, so these tests exercise the verdict logic and nothing else.
const isReadyForChat = vi.fn();
vi.mock("@/api.js", () => ({ isReadyForChat: (...a) => isReadyForChat(...a) }));

const {
	readinessDetailOf,
	needsLlmConnection,
	forgetReady,
	replacedBanner,
	hasReconnectIntent,
	RECONNECT_INTENT_URL,
} = await import("./readiness.js");

// The verdict is memoized for the page load, so every case has to start clean.
beforeEach(() => {
	forgetReady();
	isReadyForChat.mockReset();
});

const verdict = (v) => isReadyForChat.mockResolvedValue(v);

/**
 * ChatView renders the not-ready banners as ONE v-else-if chain, so at most one
 * of these two accessors may answer for any given reason. When both answered for
 * "llm_credentials" the generic, button-less "Chat may not work yet" banner won
 * the chain and the actionable "No AI connected" banner (the only route back to
 * the AI models pane) could never appear. These tests pin the split so a future
 * edit cannot silently shadow it again.
 */
describe("readiness verdict ownership", () => {
	it("gives llm_credentials to needsLlmConnection ALONE", async () => {
		// is_ready_for_chat sends no `detail` for this reason (jarvis/account.py):
		// there is nothing to quote, only a state to name.
		verdict({ ready: false, reason: "llm_credentials" });
		expect(await needsLlmConnection()).toBe(true);
		expect(await readinessDetailOf()).toBe("");
	});

	it("still hands llm_credentials to needsLlmConnection when a detail leaks in", async () => {
		// Defensive: readinessDetailOf must key off the REASON, not the presence of
		// a detail string, or a future backend that starts sending one for this
		// reason would re-create the shadowing bug.
		verdict({ ready: false, reason: "llm_credentials", detail: "Anything at all." });
		expect(await needsLlmConnection()).toBe(true);
		expect(await readinessDetailOf()).toBe("");
	});

	it("keeps container_provisioning on the generic banner, not the AI one", async () => {
		verdict({
			ready: false,
			reason: "container_provisioning",
			detail: "Your OpenAI account has reached its usage limit.",
		});
		expect(await readinessDetailOf()).toBe("Your OpenAI account has reached its usage limit.");
		expect(await needsLlmConnection()).toBe(false);
	});

	it("shows authority_repair_required on the generic banner, never a CTA one", async () => {
		// review plan 04 P0-6: the safe blocked state must surface admin's own
		// reassurance and NOTHING that offers payment/reconnect. It rides the same
		// generic detail banner as container_provisioning, and needsLlmConnection
		// (the only accessor with a CTA) must stay silent for it.
		verdict({
			ready: false,
			reason: "authority_repair_required",
			detail: "Your payment is safe — please don't retry payment.",
		});
		expect(await readinessDetailOf()).toBe(
			"Your payment is safe — please don't retry payment."
		);
		expect(await needsLlmConnection()).toBe(false);
	});

	it("leaves subscription_suspended to its own Renew banner", async () => {
		verdict({ ready: false, reason: "subscription_suspended", detail: "Renew to continue." });
		expect(await readinessDetailOf()).toBe("");
		expect(await needsLlmConnection()).toBe(false);
	});

	it("shows neither banner on a ready workspace", async () => {
		verdict({ ready: true, reason: null });
		expect(await readinessDetailOf()).toBe("");
		expect(await needsLlmConnection()).toBe(false);
	});

	it("shows neither banner when the check throws (fail-open)", async () => {
		isReadyForChat.mockRejectedValue(new Error("admin unreachable"));
		expect(await readinessDetailOf()).toBe("");
		expect(await needsLlmConnection()).toBe(false);
	});

	// The exhaustive form of the rule above: whatever account.py answers, the two
	// accessors must never BOTH claim the same verdict.
	it("never lets both accessors claim one reason", async () => {
		const reasons = [
			"signup",
			"llm_credentials",
			"llm_pool_provisioning",
			"llm_provisioning",
			"container_provisioning",
			"authority_repair_required",
			"subscription_suspended",
			null,
		];
		for (const reason of reasons) {
			forgetReady();
			verdict({ ready: false, reason, detail: "d" });
			const both = !!(await readinessDetailOf()) && (await needsLlmConnection());
			expect(both, `reason ${reason} is claimed by both accessors`).toBe(false);
		}
	});
});

/**
 * The banner stack in ChatView is a v-else-if chain, so its SOURCE ORDER is the
 * behaviour. Mounting ChatView to assert this is not viable (it is the largest
 * component in the app and boots sockets, so this reads the template instead,
 * the same trick dashboardOpen.test.js already uses on this file).
 */
describe("ChatView banner chain order", () => {
	const HERE = path.dirname(fileURLToPath(import.meta.url));
	const chatSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

	it("tests the specific No-AI banner before the generic not-ready one", () => {
		const specific = chatSrc.indexOf('v-else-if="noAiConnected"');
		const generic = chatSrc.indexOf('v-else-if="notReadyNotice"');
		expect(specific, "ChatView must still render the noAiConnected banner").not.toBe(-1);
		expect(generic, "ChatView must still render the notReadyNotice banner").not.toBe(-1);
		// Specific before generic: the No-AI banner carries the "Connect a model"
		// call to action, so a generic banner ahead of it in the chain leaves a
		// disconnected customer with no discoverable way back to the AI models pane.
		expect(specific).toBeLessThan(generic);
	});
});

describe("replaced-site banner", () => {
	it("names the site the account moved to", () => {
		const b = replacedBanner({ replaced: true, moved_to: "https://jarvis-2.example.com/" });
		expect(b.message).toContain("jarvis-2.example.com");
		expect(b.message).not.toContain("https://");
		expect(b.action).toBeTruthy();
	});

	it("still explains itself without a destination", () => {
		expect(replacedBanner({ replaced: true }).message).toContain("another site");
	});

	it("stays silent when nothing moved", () => {
		expect(replacedBanner({})).toBeNull();
		expect(replacedBanner(null)).toBeNull();
		expect(replacedBanner({ replaced: false })).toBeNull();
	});
});

describe("reconnect intent", () => {
	it("recognises only the flag the banner actually sends", () => {
		expect(hasReconnectIntent(new URL(RECONNECT_INTENT_URL, "http://x").search)).toBe(true);
		expect(hasReconnectIntent("?reconnect=1&foo=bar")).toBe(true);
		expect(hasReconnectIntent("?reconnect=0")).toBe(false);
		expect(hasReconnectIntent("?other=1")).toBe(false);
	});

	it("treats missing or unparseable input as no intent", () => {
		expect(hasReconnectIntent("")).toBe(false);
		expect(hasReconnectIntent(null)).toBe(false);
		expect(hasReconnectIntent(undefined)).toBe(false);
	});
});
