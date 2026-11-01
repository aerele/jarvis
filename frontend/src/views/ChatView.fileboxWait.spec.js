import fs from "node:fs";
import { ref } from "vue";
import { describe, expect, it, vi } from "vitest";

// Exercise the real wait-decision handler at its API boundary. The mounted
// browser flow is covered separately because ChatView has a large dependency graph.
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
function declaration(name) {
	const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
	if (start < 0) throw new Error("Missing SFC function " + name);
	return source.slice(start, source.indexOf("\n}", start) + 2);
}
const body = declaration("decideFileboxWait");
const RECOVERY =
	"Decision saved. The source file is unavailable. Select a source file, then send a new message to continue.";

function harness() {
	const api = { decideApproval: vi.fn() };
	const notify = vi.fn();
	const notifyActionError = vi.fn();
	const store = { refreshApprovalsCount: vi.fn() };
	const loadConversation = vi.fn(async () => {});
	const state = {
		api,
		currentId: ref("CONV-1"),
		fileboxWaitBusy: ref(""),
		loadConversation,
		notify,
		notifyActionError,
		store,
	};
	const decideFileboxWait = new Function(
		...Object.keys(state),
		body + "\nreturn decideFileboxWait;",
	)(...Object.values(state));
	return { ...state, decideFileboxWait };
}

describe("File Box wait decisions", () => {
	it("shows source recovery instead of a false resume success", async () => {
		const h = harness();
		h.api.decideApproval.mockResolvedValue({ resumed: false, reason: "source_unavailable" });

		await h.decideFileboxWait({ name: "AR-1" }, "Supplier A", 1);

		expect(h.notify).toHaveBeenCalledWith(RECOVERY, { type: "warning" });
		expect(h.notify).not.toHaveBeenCalledWith(
			"Answer sent. Jarvis carries on with this file.",
			{
				type: "success",
			},
		);
		expect(h.store.refreshApprovalsCount).toHaveBeenCalled();
		expect(h.loadConversation).toHaveBeenCalledWith("CONV-1");
		expect(h.fileboxWaitBusy.value).toBe("");
	});

	it("keeps the normal successful-answer notice", async () => {
		const h = harness();
		h.api.decideApproval.mockResolvedValue({ resumed: true });

		await h.decideFileboxWait({ name: "AR-1" }, "Supplier A", 1);

		expect(h.notify).toHaveBeenCalledWith("Answer sent. Jarvis carries on with this file.", {
			type: "success",
		});
		expect(h.notifyActionError).not.toHaveBeenCalled();
	});
});
