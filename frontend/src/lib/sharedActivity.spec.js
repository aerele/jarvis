import { describe, it, expect } from "vitest";
import { sharedActivity } from "./sharedActivity";

const actor = (id) => ({ id, name: "Team member" });
const user = (name, seq, id = "A") => ({ name, seq, role: "user", actor: actor(id) });
const answer = (name, seq, ids = [], id = "A", extra = {}) => ({
	name,
	seq,
	role: "assistant",
	actor: actor(id),
	tool_call_ids: ids,
	...extra,
});
const tool = (name, seq, call = "", id = "A", extra = {}) => ({
	name,
	seq,
	role: "tool",
	actor: actor(id),
	tool_call_id: call,
	tool_name: "get_doc",
	tool_status: "completed",
	tool_result: "{}",
	...extra,
});

describe("shared activity ownership and visible rows", () => {
	it("groups proven tools under their reply without duplicated transcript rows", () => {
		const rows = [
			user("U", 1),
			answer("A", 2, '["C1","C2"]'),
			tool("T1", 3, "C1"),
			tool("T2", 4, "C2"),
		];
		const result = sharedActivity(rows);
		expect(result.messages.map((r) => r.name)).toEqual(["U", "A"]);
		expect(result.tools.get("A").map((r) => r.name)).toEqual(["T1", "T2"]);
	});
	it("does not combine two actors with the same display name", () => {
		const result = sharedActivity([answer("A", 1, ["C"]), tool("T", 2, "C", "B")]);
		expect(result.messages.map((r) => r.name)).toEqual(["A", "T"]);
		expect(result.tools.get("A") || []).toEqual([]);
	});
	it("leaves a late tool visible when a queued user message splits its segment", () => {
		const result = sharedActivity([
			answer("A", 1, ["C"]),
			user("QUEUED", 2, "B"),
			tool("T", 3, "C"),
		]);
		expect(result.messages.map((r) => r.name)).toEqual(["A", "QUEUED", "T"]);
	});
	it("never assigns an unknown explicit call ID to the adjacent reply", () => {
		const result = sharedActivity([answer("A", 1, ["OTHER"]), tool("T", 2, "C")]);
		expect(result.messages.map((r) => r.name)).toEqual(["A", "T"]);
	});
	it("keeps pending decisions and receipts inline and splits fallback activity", () => {
		const result = sharedActivity([
			answer("A", 1),
			tool("T", 2),
			tool("P", 3, "", "A", { tool_status: "pending", awaiting_actor: actor("A") }),
			tool("AFTER", 4),
			tool("R", 5, "", "A", { action_outcome: "confirmed" }),
		]);
		expect(result.messages.map((r) => r.name)).toEqual(["A", "P", "AFTER", "R"]);
		expect(result.tools.get("A").map((r) => r.name)).toEqual(["T"]);
	});
	it("uses conservative legacy adjacency only inside one user segment", () => {
		const a = answer("A", 1);
		delete a.actor;
		const t = tool("T", 2);
		delete t.actor;
		const result = sharedActivity([a, t, user("U", 3), tool("ORPHAN", 4)]);
		expect(result.messages.map((r) => r.name)).toEqual(["A", "U", "ORPHAN"]);
		expect(result.tools.get("A").map((r) => r.name)).toEqual(["T"]);
	});
	it("keeps compacted results and orphan failures visible", () => {
		const rows = [
			tool("COMPACT", 1, "C", "A", {
				tool_args: null,
				tool_result: null,
				tool_result_compact: 1,
			}),
			tool("ERROR", 2, "E", "A", { tool_result: null, tool_status: "error" }),
		];
		expect(sharedActivity(rows).messages.map((r) => r.name)).toEqual(["COMPACT", "ERROR"]);
	});
	it("regroups an orphan when its proven assistant anchor is loaded", () => {
		const t = tool("T", 3, "C");
		expect(sharedActivity([t]).messages).toEqual([t]);
		const result = sharedActivity([answer("A", 2, ["C"]), t]);
		expect(result.messages.map((r) => r.name)).toEqual(["A"]);
		expect(result.tools.get("A")).toEqual([t]);
	});
	it("rejects ambiguous call owners and conflicting public run identifiers", () => {
		for (const rows of [
			[answer("A", 1, ["C"]), answer("B", 2, ["C"]), tool("T", 3, "C")],
			[
				answer("A", 1, ["C"], "A", { run_id: "R1" }),
				tool("T", 2, "C", "A", { run_id: "R2" }),
			],
		])
			expect(sharedActivity(rows).messages.some((r) => r.name === "T")).toBe(true);
	});
	it("does not bridge incompatible runs or treat malformed call IDs as legacy", () => {
		for (const rows of [
			[
				answer("A", 1, ["C"], "A", { run_id: "R1" }),
				answer("B", 2, [], "A", { run_id: "R2" }),
				tool("T", 3, "C", "A", { run_id: "R1" }),
			],
			[answer("A", 1, "invalid-json"), tool("T", 2)],
		])
			expect(sharedActivity(rows).messages.some((r) => r.name === "T")).toBe(true);
	});
});
