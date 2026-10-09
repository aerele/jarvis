import { describe, it, expect } from "vitest";
import {
	subscriptionAccountHealth,
	dirtyAccountHealth,
	expiredLine,
	expiredEntryMap,
	accountExpiryStates,
	rowExpiry,
} from "./pool.js";

const fmt = () => "Oct 6, 2026";
const entry = (ref, extra = {}) => ({
	account_ref: ref,
	upstream: "openai",
	label: "OpenAI",
	email: `${ref}@x.com`,
	state: "expired",
	source: "poll",
	since: 1791230000,
	fallback: "",
	...extra,
});
const row = (...refs) => ({
	credentialType: "subscription",
	upstream: "openai",
	accounts: refs.map((account_ref) => ({ account_ref, upstream: "openai" })),
});

describe("subscriptionAccountHealth expired level", () => {
	it("beats every other verdict, including a green one", () => {
		for (const status of ["verified", "unverified", "unchecked", "", undefined]) {
			const h = subscriptionAccountHealth(status, { expired: entry("A1") });
			expect(h.level).toBe("expired");
			expect(h.label).toBe("Sign-in expired");
			expect(h.title).toContain("Reconnect");
		}
	});

	it("is unchanged without an entry (signature stays backward compatible)", () => {
		expect(subscriptionAccountHealth("verified")).toEqual({ level: "ok" });
		expect(subscriptionAccountHealth("unverified").level).toBe("warn");
		expect(subscriptionAccountHealth("", { expired: null })).toEqual({ level: "ok" });
	});

	it("survives a dirty or pending pool instead of collapsing to neutral", () => {
		const settled = subscriptionAccountHealth("", { expired: entry("A1") });
		expect(dirtyAccountHealth(settled, true)).toBe(settled);
		expect(dirtyAccountHealth({ level: "ok" }, true).level).toBe("pending");
	});
});

describe("expiredLine", () => {
	it("names the fallback", () => {
		expect(expiredLine(entry("A1", { fallback: "Anthropic" }), { formatDate: fmt })).toBe(
			"Expired Oct 6, 2026. Auto uses Anthropic until you reconnect."
		);
	});

	it("reads naturally when the fallback is another account of the same upstream", () => {
		expect(
			expiredLine(entry("A1", { fallback: "Another OpenAI account" }), { formatDate: fmt })
		).toBe("Expired Oct 6, 2026. Auto uses another OpenAI account until you reconnect.");
	});

	it("formats the since epoch as a local date by default", () => {
		const since = 1791230000;
		const expected = new Date(since * 1000).toLocaleDateString(undefined, {
			year: "numeric",
			month: "short",
			day: "numeric",
		});
		expect(expiredLine(entry("A1", { fallback: "Anthropic", since }))).toBe(
			`Expired ${expected}. Auto uses Anthropic until you reconnect.`
		);
	});

	it("says chats fail when nothing else can answer", () => {
		expect(expiredLine(entry("A1"), { formatDate: fmt })).toBe(
			"Chats fail until you reconnect."
		);
	});

	it("says another account answers when one in the same row is fine", () => {
		expect(
			expiredLine(entry("A1", { fallback: "Anthropic" }), {
				sameRowOk: true,
				formatDate: fmt,
			})
		).toBe("Auto uses another account until you reconnect.");
	});

	it("says Auto skips it when the entry's row is after the answering one, keeping the date", () => {
		expect(
			expiredLine(entry("A1", { fallback: "OpenAI", skipped: true }), { formatDate: fmt })
		).toBe(`Expired ${fmt(entry("A1").since)}. Auto skips it until you reconnect.`);
		expect(
			expiredLine(entry("A1", { fallback: "OpenAI", skipped: false }), { formatDate: fmt })
		).toContain("Auto uses OpenAI until you reconnect.");
	});

	it("says Auto skips it for a skipped entry even when a sibling on the same row is healthy", () => {
		expect(expiredLine(entry("A1", { skipped: true }), { sameRowOk: true })).toBe(
			"Auto skips it until you reconnect."
		);
	});

	it("keeps the no-fallback line when skipped", () => {
		expect(expiredLine(entry("A1", { fallback: "", skipped: true }))).toBe(
			"Chats fail until you reconnect."
		);
	});

	it("drops the date prefix when there is no since", () => {
		expect(
			expiredLine(entry("A1", { since: 0, fallback: "Anthropic" }), { formatDate: fmt })
		).toBe("Auto uses Anthropic until you reconnect.");
	});

	it("is empty for no entry and has no em dash", () => {
		expect(expiredLine(null)).toBe("");
		expect(expiredLine(entry("A1", { fallback: "Anthropic" }))).not.toContain("\u2014");
	});
});

describe("per-account state (Review Focus 5)", () => {
	it("a pool row with two OpenAI accounts, one expired, says another account answers", () => {
		const states = accountExpiryStates(row("A1", "A2"), [
			entry("A1", { fallback: "Anthropic" }),
		]);
		expect(states[0].entry.account_ref).toBe("A1");
		expect(states[0].line).toBe("Auto uses another account until you reconnect.");
		expect(states[0].line).not.toContain("Chats fail");
		expect(states[1]).toMatchObject({ entry: null, line: "" });
	});

	it("is not a row-level expiry while an account on the row still works", () => {
		expect(rowExpiry(row("A1", "A2"), [entry("A1")])).toBeNull();
	});

	it("is a row-level expiry when every account is expired, and then falls back to the fallback", () => {
		const out = rowExpiry(row("A1", "A2"), [
			entry("A1", { fallback: "Anthropic" }),
			entry("A2", { fallback: "Anthropic" }),
		]);
		expect(out.entry.account_ref).toBe("A1");
		expect(out.line).toContain("Auto uses Anthropic until you reconnect.");
	});

	it("a single-account row expires on its own account", () => {
		expect(rowExpiry(row("A1"), [entry("A1")]).line).toBe("Chats fail until you reconnect.");
	});

	it("ignores api-key rows, accountless rows and junk entries", () => {
		expect(rowExpiry({ credentialType: "api_key", accounts: [] }, [entry("A1")])).toBeNull();
		expect(rowExpiry(row(), [entry("A1")])).toBeNull();
		expect(expiredEntryMap([null, {}, entry("A1")]).size).toBe(1);
		expect(expiredEntryMap(undefined).size).toBe(0);
	});
});
