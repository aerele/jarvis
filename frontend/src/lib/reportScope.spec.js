import { describe, it, expect } from "vitest";
import { reportScopes, reportToolsByAssistant } from "./reportScope";

const assistant = { name: "answer", role: "assistant", tool_call_ids: '["call-1","call-2"]' };
const receipt = { name: "receipt", role: "tool", tool_call_id: "call-1" };

describe("durable report ownership", () => {
	it.each([
		[assistant, { role: "user" }, receipt],
		[assistant, { role: "user" }, { role: "assistant", name: "cancelled" }, receipt],
		[receipt, assistant],
		[assistant, receipt, { ...receipt }],
	])(
		"uses call identity despite queueing, cancellation, callback order and replay",
		(...messages) => {
			expect(reportToolsByAssistant(messages)).toEqual({ answer: [receipt] });
		}
	);
	it("does not guess for legacy, malformed or conflicting ownership", () => {
		expect(reportToolsByAssistant([{ role: "assistant", name: "old" }, receipt])).toEqual({});
		expect(
			reportToolsByAssistant([{ ...assistant, tool_call_ids: "invalid" }, receipt])
		).toEqual({});
		expect(
			reportToolsByAssistant([assistant, { ...assistant, name: "other" }, receipt])
		).toEqual({});
	});
	it("preserves multiple reports and survives a serialized reload", () => {
		const second = { ...receipt, name: "second-company", tool_call_id: "call-2" };
		const messages = [assistant, receipt, second];
		expect(reportToolsByAssistant(JSON.parse(JSON.stringify(messages)))).toEqual({
			answer: [receipt, second],
		});
	});
});

const run = (name, scope, data = {}) => ({
	name,
	role: "tool",
	tool_name: "run_report",
	tool_status: "completed",
	tool_result: {
		ok: true,
		data: {
			result: [],
			report_scope: {
				version: 1,
				report_name: "General Ledger",
				company: "Acme",
				report_date: "2026-09-24",
				currency_mode: "company",
				currency: "INR",
				...scope,
			},
			...data,
		},
	},
});

describe("reportScopes from receipts", () => {
	it("retains scope for metadata-only prepared report receipts after reload", () => {
		const metadata = run("ready", {}, { result: undefined, prepared_report: true, status: "ready", read_status: "not_read", coverage: { complete: false }, as_of: "2026-10-30 08:00:00" });
		expect(reportScopes(JSON.parse(JSON.stringify([metadata])))).toMatchObject([{ company: "Acme", generated_at: "2026-10-30 08:00:00" }]);
	});
	it("keeps one entry per company and per currency set, in receipt order", () => {
		const out = reportScopes([
			run("t1", { company: "Acme" }),
			run("t2", { company: "Globex", currency: "USD" }),
			run("t3", { company: "Acme", currencies: ["INR", "USD", "INR"] }),
		]);
		expect(out.map((s) => [s.id, s.company, s.currency])).toEqual([
			["t1", "Acme", "INR"],
			["t2", "Globex", "USD"],
			["t3", "Acme", "INR, USD"],
		]);
	});
	it("separates a prepared report's generation time from its as-of date", () => {
		const [s] = reportScopes([
			run(
				"t1",
				{},
				{ prepared_report: true, status: "ready", as_of: "2026-09-20 08:00:00" }
			),
		]);
		expect(s.report_date).toBe("2026-09-24");
		expect(s.generated_at).toBe("2026-09-20 08:00:00");
		const [unknown] = reportScopes([
			run("t2", {}, { prepared_report: true, status: "ready" }),
		]);
		expect(unknown.generated_at).toBe("Unknown");
	});
	it("shows nothing for a permission-refused or failed report", () => {
		const refused = run("t1", {});
		refused.tool_result = { ok: false, error: { code: "PermissionError" } };
		const errored = { ...run("t2", {}), tool_status: "error" };
		expect(reportScopes([refused, errored])).toEqual([]);
	});
	it("never infers scope from prose or requested filters", () => {
		const prose = {
			role: "assistant",
			name: "a",
			content: "General Ledger for Acme as of 2026-09-24 in INR",
		};
		const asked = { ...run("t1", {}), tool_args: { filters: { company: "Acme" } } };
		asked.tool_result.data.report_scope = undefined;
		expect(reportScopes([prose, asked])).toEqual([]);
	});
	it("binds receipts to the answer that ran them, not a neighbour", () => {
		const a1 = { role: "assistant", name: "a1", tool_call_ids: ["c1"] };
		const a2 = { role: "assistant", name: "a2", tool_call_ids: ["c2"] };
		const r1 = { ...run("t1", { company: "Acme" }), tool_call_id: "c1" };
		const r2 = { ...run("t2", { company: "Globex" }), tool_call_id: "c2" };
		const by = reportToolsByAssistant([a1, a2, r2, r1]);
		expect(reportScopes(by.a1).map((s) => s.company)).toEqual(["Acme"]);
		expect(reportScopes(by.a2).map((s) => s.company)).toEqual(["Globex"]);
	});
});
