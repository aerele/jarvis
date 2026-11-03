import fs from "node:fs";
import { ref, watch } from "vue";
import { describe, expect, it, vi } from "vitest";

// Exercise the real wait-decision handler at its API boundary. The mounted
// browser flow is covered separately because ChatView has a large dependency graph.
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
function declaration(name) {
	const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
	if (start < 0) throw new Error("Missing SFC function " + name);
	return source.slice(start, source.indexOf("\n}", start) + 2);
}
const body = ["captureChatView", "chatViewIsCurrent", "fileboxWaitIsCurrent", "decideFileboxWait"]
	.map(declaration)
	.join("\n");
const RECOVERY =
	"Decision saved. The source file is unavailable. Select a source file, then send a new message to continue.";

function harness() {
	const api = { decideApproval: vi.fn() };
	const notify = vi.fn();
	const notifyActionError = vi.fn();
	const store = { refreshApprovalsCount: vi.fn() };
	const loadConversation = vi.fn(async () => {});
	const state = {
		watch,
		fileboxWaitInFlight: new Set(),
		fileboxWaits: ref([{ name: "AR-1", conversation: "CONV-1" }]),
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
		`let chatViewGeneration = 0;
		watch(currentId, () => {
			chatViewGeneration++;
			fileboxWaitBusy.value = "";
			fileboxWaitInFlight.clear();
		}, { flush: "sync" });
		${body}
		return decideFileboxWait;`
	)(...Object.values(state));
	return { ...state, decideFileboxWait };
}

describe("File Box wait decisions", () => {
	it("shows source recovery instead of a false resume success", async () => {
		const h = harness();
		h.api.decideApproval.mockResolvedValue({ resumed: false, reason: "source_unavailable" });

		await h.decideFileboxWait({ name: "AR-1", conversation: "CONV-1" }, "Supplier A", 1);

		expect(h.notify).toHaveBeenCalledWith(RECOVERY, { type: "warning" });
		expect(h.notify).not.toHaveBeenCalledWith(
			"Answer sent. Jarvis carries on with this file.",
			{
				type: "success",
			}
		);
		expect(h.store.refreshApprovalsCount).toHaveBeenCalled();
		expect(h.loadConversation).toHaveBeenCalledWith("CONV-1");
		expect(h.fileboxWaitBusy.value).toBe("");
	});

	it("keeps the normal successful-answer notice", async () => {
		const h = harness();
		h.api.decideApproval.mockResolvedValue({ resumed: true });

		await h.decideFileboxWait({ name: "AR-1", conversation: "CONV-1" }, "Supplier A", 1);

		expect(h.notify).toHaveBeenCalledWith("Answer sent. Jarvis carries on with this file.", {
			type: "success",
		});
		expect(h.notifyActionError).not.toHaveBeenCalled();
	});
});

it("does not show an old source warning after leaving and returning to a chat", async () => {
	const h = harness();
	let resolve;
	h.api.decideApproval.mockReturnValue(
		new Promise((done) => {
			resolve = done;
		})
	);
	const pending = h.decideFileboxWait({ name: "AR-1", conversation: "CONV-1" }, "Supplier A", 1);
	h.currentId.value = "CONV-2";
	h.currentId.value = "CONV-1";
	h.fileboxWaitBusy.value = "NEW-ACTION";
	resolve({ resumed: false, reason: "source_unavailable" });
	await pending;
	expect(h.notify).not.toHaveBeenCalled();
	expect(h.loadConversation).not.toHaveBeenCalled();
	expect(h.fileboxWaitBusy.value).toBe("NEW-ACTION");
});
