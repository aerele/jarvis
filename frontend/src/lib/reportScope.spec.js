import { describe, it, expect } from "vitest";
import { reportToolsByAssistant } from "./reportScope";

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
