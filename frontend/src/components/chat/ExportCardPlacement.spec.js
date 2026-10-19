import fs from "fs";
import path from "path";
import { mount } from "@vue/test-utils";
import { h } from "vue";
import { describe, expect, it, vi } from "vitest";
import ExportCard from "./ExportCard.vue";
import Message from "./Message.vue";
import { exportSummary } from "../../lib/exportJobs.js";

// The desktop ChatView is too large to mount, so (like copyReplyWiring.spec.js) its wiring is
// asserted against the source, and the part that can run is run: a card message's row with the
// body left empty shows the card and none of the placeholder text.
const view = fs.readFileSync(path.resolve(__dirname, "../../views/ChatView.vue"), "utf8");
const cardMessage = {
	role: "assistant",
	content: "Export started: Suppliers.",
	tool_call_id: "export:job1:card",
	tool_result: JSON.stringify({ ok: true, data: { export_job: "job1" } }),
};

describe("desktop export card message", () => {
	it("ChatView leaves the placeholder body empty for a card message", () => {
		expect(view).toMatch(
			/:html="\s*m\.error \|\| exportSummary\(m\) \? '' : render\(m\.content, m\.streaming\)\s*"/
		);
	});

	it("ChatView hides Copy for a card message", () => {
		expect(view).toMatch(
			/function canCopyReply\(m\) \{\s*return replyBarParts\(m\)\.showCopy && !exportSummary\(m\);\s*\}/
		);
		expect(view).toContain(`:disabled="!canCopyReply(m)"`);
		expect(view).not.toMatch(/:disabled="!replyBarParts\(m\)\.showCopy"/);
	});

	it("the message row shows the card and not the 'Export started' text", () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => ({ ok: true, json: async () => ({ message: {} }) }))
		);
		const summary = exportSummary(cardMessage);
		const wrapper = mount(Message, {
			props: { variant: "row", html: "" },
			slots: { "above-body": () => h(ExportCard, { summary }) },
		});
		try {
			expect(wrapper.find(".jv-xcard").exists()).toBe(true);
			expect(wrapper.text()).not.toContain("Export started:");
		} finally {
			wrapper.unmount();
			vi.unstubAllGlobals();
		}
	});
});
