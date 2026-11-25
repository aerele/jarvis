// The phone's calls for server draft cards (pwa/src/api.js). The shared draft host and form
// builder call them by the desktop SPA's names, so a wrong endpoint, a values object that is not
// a JSON string, or a key renamed on one side only would save the wrong record or fail on the
// phone alone. Each row is run through the phone module and the desktop module and must send the
// literal request below.
import { describe, it, expect, vi, beforeEach } from "vitest";
const { call } = vi.hoisted(() => ({ call: vi.fn(async () => ({ ok: true })) }));
vi.mock("frappe-ui", () => ({ call }));
import * as phone from "../../../pwa/src/api.js";
import * as desktop from "../api.js";

const AC = "jarvis.chat.actions_api.";
const TABLE = [
	[
		"saveDraftCard sends the edited values as a JSON string",
		"saveDraftCard",
		["tok", "conv-1", { hours: "2", items: [{ _draft_row: 0, qty: 3 }] }],
		AC + "save_draft_card",
		{
			token: "tok",
			conversation: "conv-1",
			values: JSON.stringify({ hours: "2", items: [{ _draft_row: 0, qty: 3 }] }),
		},
	],
	[
		"saveDraftCard with nothing changed sends an empty object and an empty conversation",
		"saveDraftCard",
		["tok", undefined, undefined],
		AC + "save_draft_card",
		{ token: "tok", conversation: "", values: "{}" },
	],
	[
		"ruleValues sends the links as a JSON string",
		"ruleValues",
		["Timesheet", "time_logs", { activity_type: ["Planning"] }],
		AC + "rule_values",
		{
			doctype: "Timesheet",
			table: "time_logs",
			links: JSON.stringify({ activity_type: ["Planning"] }),
		},
	],
	[
		"searchLink with only a doctype asks for 8 rows of everything",
		"searchLink",
		["Customer", undefined, undefined],
		"frappe.desk.search.search_link",
		{ doctype: "Customer", txt: "", page_length: 8 },
	],
	[
		"searchLink sends its context only when known",
		"searchLink",
		["Item", "pen", 5, "Sales Invoice", "item_code", [["Item", "disabled", "=", 0]]],
		"frappe.desk.search.search_link",
		{
			doctype: "Item",
			txt: "pen",
			page_length: 5,
			reference_doctype: "Sales Invoice",
			link_fieldname: "item_code",
			filters: [["Item", "disabled", "=", 0]],
		},
	],
	[
		"proposeNextAction names the record, the kind and the action only when given",
		"proposeNextAction",
		["conv-1", { doctype: "Sales Invoice", name: "SI-1", kind: "submit" }],
		AC + "propose_next_action",
		{ conversation: "conv-1", doctype: "Sales Invoice", name: "SI-1", kind: "submit" },
	],
	[
		"proposeNextAction carries a workflow action",
		"proposeNextAction",
		[
			"conv-1",
			{ doctype: "Leave Application", name: "LA-1", kind: "workflow", action: "Approve" },
		],
		AC + "propose_next_action",
		{
			conversation: "conv-1",
			doctype: "Leave Application",
			name: "LA-1",
			kind: "workflow",
			action: "Approve",
		},
	],
];

describe("phone api: server draft cards", () => {
	beforeEach(() => call.mockClear());

	for (const [title, fn, args, endpoint, payload] of TABLE) {
		it(title, () => {
			phone[fn](...args);
			expect(call).toHaveBeenLastCalledWith(endpoint, payload);
			// The same request the desktop SPA sends: one contract, two clients.
			desktop[fn](...args);
			expect(call.mock.calls.at(-1)).toEqual([endpoint, payload]);
		});
	}

	it("loadDocForEdit is the phone's loadDoc (write-gated saved record), not a second call", () => {
		expect(phone.loadDocForEdit).toBe(phone.loadDoc);
		phone.loadDocForEdit("Task", "TASK-1");
		expect(call).toHaveBeenLastCalledWith(AC + "load_doc", {
			doctype: "Task",
			name: "TASK-1",
		});
	});
});
