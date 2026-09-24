import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import ReportScope from "./ReportScope.vue";

function receipt(company = "Example Company", extra = {}) {
	return {
		name: company,
		role: "tool",
		tool_name: "run_report",
		tool_status: "completed",
		tool_result: {
			ok: true,
			data: {
				result: [],
				report_scope: {
					version: 1,
					report_name: "Accounts Payable Summary",
					company,
					report_date: "2026-09-24",
					currency_mode: "company",
					currency: "INR",
				},
			},
		},
		...extra,
	};
}

describe("ReportScope", () => {
	it("renders empty completed reports without needing any assistant text", () => {
		const w = mount(ReportScope, { props: { tools: [receipt()] } });
		expect(w.text()).toContain("Example Company · As of 2026-09-24 · INR");
	});
	it("accepts serialized receipts after reload and live objects identically", async () => {
		const tool = receipt();
		const w = mount(ReportScope, { props: { tools: [tool] } });
		const live = w.text();
		await w.setProps({ tools: [{ ...tool, tool_result: JSON.stringify(tool.tool_result) }] });
		expect(w.text()).toBe(live);
	});
	it("keeps separate companies and escapes their names", () => {
		const w = mount(ReportScope, {
			props: { tools: [receipt(), receipt("<img src=x onerror=alert(1)>")] },
		});
		expect(w.findAll(".report-scope")).toHaveLength(2);
		expect(w.find("img").exists()).toBe(false);
		expect(w.text()).toContain("<img src=x onerror=alert(1)>");
	});
	it.each(["started", "generating", "failed"])(
		"does not present %s as a completed report",
		(status) => {
			const tool = receipt();
			Object.assign(tool.tool_result.data, { prepared_report: true, status });
			expect(mount(ReportScope, { props: { tools: [tool] } }).text()).toBe("");
		}
	);
	it("does not infer scope from model content, arguments or malformed results", () => {
		const tools = [
			receipt("a", { role: "assistant" }),
			receipt("b", { tool_status: "error" }),
			receipt("c", { tool_name: "query" }),
			receipt("d", { tool_result: "invalid" }),
			receipt("e", { tool_result: { ok: false } }),
			receipt("f", { tool_result: null }),
		];
		expect(mount(ReportScope, { props: { tools } }).text()).toBe("");
	});
	it("does not substitute company currency for account currency", () => {
		const tool = receipt();
		tool.tool_result.data.report_scope.currency_mode = "account";
		const w = mount(ReportScope, { props: { tools: [tool] } });
		expect(w.text()).toContain("Party/account currency");
		expect(w.text()).not.toContain("INR");
	});
});
