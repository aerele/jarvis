import { describe, it, expect } from "vitest";
import { computed, nextTick, ref } from "vue";

import { toolRowFromResult, upsertToolRow, withLiveToolRows } from "./liveToolRows";
import { reportToolsByAssistant } from "./reportScope";
import { collectDocRefs } from "./docRefs";
import { turnToolNames } from "./liveTurn";

const result = (over = {}) => ({
	kind: "tool:result",
	conversation_id: "c1",
	tool_message_id: "t-recall",
	tool_name: "recall",
	args: { query: "customers" },
	result: { ok: true, data: [] },
	status: "completed",
	action_outcome: null,
	...over,
});

describe("toolRowFromResult", () => {
	it("is the saved tool row the event stands for", () => {
		expect(toolRowFromResult(result())).toEqual({
			name: "t-recall",
			role: "tool",
			tool_call_id: null,
			tool_name: "recall",
			tool_status: "completed",
			tool_args: { query: "customers" },
			tool_result: { ok: true, data: [] },
			action_outcome: null,
		});
	});

	it("carries the order this tab saw it in, when given", () => {
		expect(toolRowFromResult(result(), 7).ord).toBe(7);
		expect("ord" in toolRowFromResult(result())).toBe(false);
	});

	it("defaults a missing status to completed and missing payloads to null", () => {
		const row = toolRowFromResult({ tool_message_id: "t1", tool_name: "query" });
		expect(row.tool_status).toBe("completed");
		expect(row.tool_args).toBeNull();
		expect(row.tool_result).toBeNull();
	});
});

describe("upsertToolRow", () => {
	it("adds a new row and replaces one with the same name", () => {
		const a = toolRowFromResult(result());
		const rows = upsertToolRow([], a);
		expect(rows).toEqual([a]);
		const b = { ...a, tool_status: "error" };
		expect(upsertToolRow(rows, b)).toEqual([b]);
		expect(rows).toEqual([a]); // a new list, the old one untouched
	});

	it("ignores a row without a name", () => {
		const rows = [toolRowFromResult(result())];
		expect(upsertToolRow(rows, { tool_name: "query" })).toBe(rows);
	});
});

describe("withLiveToolRows", () => {
	const msgs = [
		{ name: "u1", role: "user" },
		{ name: "a1", role: "assistant" },
		{ name: "u2", role: "user" },
		{ name: "a2", role: "assistant" },
	];

	it("places live rows right after their assistant row", () => {
		const row = toolRowFromResult(result());
		const out = withLiveToolRows(msgs, { msgId: "a1", rows: [row] });
		expect(out.map((m) => m.name)).toEqual(["u1", "a1", "t-recall", "u2", "a2"]);
	});

	it("goes after tool rows already loaded for the reply, and skips ones it has", () => {
		const saved = { name: "t-query", role: "tool", tool_name: "query" };
		const withSaved = [...msgs, saved];
		const live = {
			msgId: "a2",
			rows: [
				{ ...saved, tool_status: "completed" },
				toolRowFromResult(
					result({ tool_message_id: "t-report", tool_name: "run_report" })
				),
			],
		};
		const out = withLiveToolRows(withSaved, live);
		expect(out.map((m) => m.name)).toEqual(["u1", "a1", "u2", "a2", "t-query", "t-report"]);
		expect(out[4]).toBe(saved); // the loaded row wins
	});

	it("lists the live rows' call ids on the reply row, keeping the ones it has", () => {
		const withIds = [...msgs.slice(0, 3), { ...msgs[3], tool_call_ids: '["call_0"]' }];
		const live = {
			msgId: "a2",
			rows: [
				toolRowFromResult(result({ tool_message_id: "t1", tool_call_id: "call_1" })),
				toolRowFromResult(result({ tool_message_id: "t2", tool_call_id: "call_0" })),
				toolRowFromResult(result({ tool_message_id: "t3" })),
			],
		};
		const out = withLiveToolRows(withIds, live);
		expect(out[3]).toEqual({
			name: "a2",
			role: "assistant",
			tool_call_ids: ["call_0", "call_1"],
		});
		expect(withIds[3].tool_call_ids).toBe('["call_0"]'); // the loaded row is not mutated
		const noIds = withLiveToolRows(msgs, { msgId: "a2", rows: [live.rows[2]] });
		expect(noIds[3]).toBe(msgs[3]); // nothing to bind: the reply row is untouched
	});

	it("never reads the streaming reply's content, so reveal ticks do not recompute it", async () => {
		// Review C1: spreading the reactive reply row tracked `content`.
		const messages = ref([
			{ name: "u1", role: "user" },
			{ name: "a1", role: "assistant", content: "" },
		]);
		const live = ref({
			msgId: "a1",
			rows: [toolRowFromResult(result({ tool_message_id: "t1", tool_call_id: "call_1" }))],
		});
		let runs = 0;
		const transcript = computed(() => {
			runs++;
			return withLiveToolRows(messages.value, live.value);
		});
		expect(transcript.value[1].tool_call_ids).toEqual(["call_1"]);
		for (let i = 0; i < 5; i++) {
			messages.value[1].content += "x";
			await nextTick();
			void transcript.value;
		}
		expect(runs).toBe(1);
	});

	it("harvests record links from live rows, whose payloads are objects", () => {
		const list = toolRowFromResult(
			result({
				tool_message_id: "t-list",
				tool_name: "get_list",
				args: { doctype: "Customer" },
				result: { ok: true, data: [{ name: "West View Software Ltd." }] },
			})
		);
		const out = withLiveToolRows(msgs, { msgId: "a2", rows: [list] });
		expect(collectDocRefs(out)).toEqual({ "West View Software Ltd.": "Customer" });
	});

	it("returns the same list when there is nothing to add", () => {
		expect(withLiveToolRows(msgs, { msgId: null, rows: [] })).toBe(msgs);
		expect(
			withLiveToolRows(msgs, { msgId: "gone", rows: [toolRowFromResult(result())] })
		).toBe(msgs);
	});

	it("feeds the count and Reports consulted before the reload, unchanged after it", () => {
		const reportResult = {
			ok: true,
			data: {
				result: [{ party: "West View" }],
				report_scope: {
					version: 1,
					currency_mode: "company",
					company: "Jarvis Demo",
					report_name: "Accounts Receivable Summary",
					report_date: "2026-09-29",
					currency: "INR",
				},
			},
		};
		const live = {
			msgId: "a2",
			rows: [
				toolRowFromResult(result()),
				toolRowFromResult(
					result({
						tool_message_id: "t-report",
						tool_call_id: "call_1",
						tool_name: "run_report",
						result: reportResult,
					})
				),
			],
		};
		const liveTools = [{ id: "call_1", name: "run_report" }];
		const before = withLiveToolRows(msgs, live);
		const rowsBefore = before.filter((m) => m.role === "tool");
		expect(turnToolNames(rowsBefore, liveTools)).toEqual(["recall", "run_report"]);
		expect(reportToolsByAssistant(before).a2.map((t) => t.name)).toEqual(["t-report"]);

		// The enrichment reload: the same rows, saved; the reply row now carries
		// the call ids the server bound from the run's tool events.
		const reloaded = [
			...msgs.slice(0, 3),
			{ ...msgs[3], tool_call_ids: JSON.stringify(["call_1"]) },
			{ ...live.rows[0] },
			{ ...live.rows[1], tool_result: JSON.stringify(reportResult) },
		];
		const after = withLiveToolRows(reloaded, live);
		expect(after).toBe(reloaded);
		const rowsAfter = after.filter((m) => m.role === "tool");
		expect(turnToolNames(rowsAfter, liveTools)).toEqual(["recall", "run_report"]);
		expect(reportToolsByAssistant(after).a2.map((t) => t.name)).toEqual(["t-report"]);
	});
});
