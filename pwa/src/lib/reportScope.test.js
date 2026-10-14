// The PWA shows the same report scope as the desktop chat, derived from the
// shared frontend/src/lib/reportScope.js. There is no component harness in this
// app (see importFinishedRender.test.js), so the binding is asserted on the
// shared derivation plus the wiring in the source.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { reportScopes, reportToolsByAssistant } from "../../../frontend/src/lib/reportScope.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const read = (...p) => fs.readFileSync(path.join(HERE, "..", ...p), "utf8");

const receipt = (name, callId, scope = {}) => ({
	name,
	role: "tool",
	tool_call_id: callId,
	tool_name: "run_report",
	tool_status: "completed",
	tool_result: JSON.stringify({
		ok: true,
		data: {
			result: [],
			report_scope: {
				version: 1,
				report_name: "Accounts Payable",
				company: "Acme",
				report_date: "2026-09-24",
				currency_mode: "company",
				currency: "INR",
				...scope,
			},
		},
	}),
});

test("each answer gets only the receipts of its own tool calls", () => {
	const messages = [
		{ name: "u1", role: "user" },
		{ name: "a1", role: "assistant", tool_call_ids: '["c1"]' },
		receipt("t1", "c1", { company: "Acme" }),
		{ name: "a2", role: "assistant", tool_call_ids: '["c2"]' },
		receipt("t2", "c2", { company: "Globex", currency: "USD" }),
	];
	const by = reportToolsByAssistant(messages);
	assert.deepEqual(
		reportScopes(by.a1).map((s) => [s.id, s.company, s.currency]),
		[["t1", "Acme", "INR"]]
	);
	assert.deepEqual(
		reportScopes(by.a2).map((s) => [s.id, s.company, s.currency]),
		[["t2", "Globex", "USD"]]
	);
});

test("a refused report and prose naming a company show no scope", () => {
	const refused = receipt("t1", "c1");
	refused.tool_result = JSON.stringify({ ok: false, error: { code: "PermissionError" } });
	const messages = [
		{
			name: "a1",
			role: "assistant",
			content: "Acme, INR, as of 2026-09-24",
			tool_call_ids: ["c1"],
		},
		refused,
	];
	assert.deepEqual(reportScopes(reportToolsByAssistant(messages).a1), []);
});

test("mixed currencies and stale prepared reports keep their visible differences", () => {
	const mixed = receipt("t1", "c1", { currencies: ["INR", "USD"] });
	const prepared = receipt("t2", "c1");
	const data = JSON.parse(prepared.tool_result);
	Object.assign(data.data, {
		prepared_report: true,
		status: "ready",
		as_of: "2026-09-20 08:00:00",
	});
	prepared.tool_result = JSON.stringify(data);
	const out = reportScopes([mixed, prepared]);
	assert.equal(out[0].currency, "INR, USD");
	assert.equal(out[1].generated_at, "2026-09-20 08:00:00");
});

test("PWA ChatView binds ReportScope to the answer's own key via the shared grouping", () => {
	const chat = read("views", "ChatView.vue");
	assert.match(chat, /reportToolsByAssistant\(messages\.value\)/);
	assert.match(chat, /<ReportScope[^>]*reportToolsByTurn\[it\.key\]/);
	assert.match(read("components", "ReportScope.vue"), /from "@shared\/lib\/reportScope\.js"/);
});
