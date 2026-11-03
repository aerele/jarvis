import { effectScope, nextTick, ref } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parseAsk } from "@/lib/chatAsk";
import { useFileBoxWaits } from "./useFileBoxWaits";

const sheet = { src: "sheet", name: "SH-1" };
const question = { src: "ar", source: "Chat", name: "AR-1", question: "Which supplier?" };
const ask = { questions: [{ q: "Which supplier?" }] };
const scopes = [];
function setup(items = [sheet, question]) {
	const scope = effectScope();
	scopes.push(scope);
	const conversation = ref("A");
	const activeAsk = ref(ask);
	const load = vi.fn(async () => ({ items }));
	const onChanged = vi.fn();
	const waits = scope.run(() => useFileBoxWaits({ conversation, activeAsk, load, onChanged }));
	return { ...waits, conversation, activeAsk, load, onChanged };
}
afterEach(() => {
	scopes.splice(0).forEach((s) => s.stop());
	vi.useRealTimers();
});

describe("File Box waits in the current chat", () => {
	it("hides only the Chat question with the full visible ask label", async () => {
		const w = setup([question, { ...question, name: "AR-2", source: "File Box" }]);
		await w.refresh("A", true);
		expect(w.visibleItems.value.map((i) => i.name)).toEqual(["AR-2"]);
		w.activeAsk.value = null;
		expect(w.visibleItems.value).toHaveLength(2);
	});
	it("uses every field label and normalizes whitespace without hiding unrelated asks", async () => {
		const w = setup([{ ...question, question: "Supplier\n  Company" }, question]);
		w.activeAsk.value = { questions: [{ q: "", fieldname: "supplier" }, { q: "Company" }] };
		await w.refresh("A", true);
		expect(w.visibleItems.value).toEqual([question]);
	});
	it("keeps a mismatched server last-fence question", async () => {
		const w = setup([question]);
		w.activeAsk.value = parseAsk(
			'```jarvis-ask\n[{"q":"First?","type":"text"}]\n```\n```jarvis-ask\n[{"q":"Which supplier?","type":"text"}]\n```'
		);
		await w.refresh("A", true);
		expect(w.visibleItems.value).toEqual([question]);
	});
	it("does not expose an empty duplicate panel", async () => {
		const w = setup([question]);
		await w.refresh("A", true);
		expect(w.visibleItems.value).toEqual([]);
	});
	it("rejects a late A response after A -> B -> A", async () => {
		const w = setup();
		let resolve;
		w.load.mockImplementationOnce(() => new Promise((r) => (resolve = r)));
		const old = w.refresh("A", true);
		w.conversation.value = "B";
		w.conversation.value = "A";
		await w.refresh("A", true);
		resolve({ items: [] });
		await old;
		expect(w.items.value).toEqual([sheet, question]);
	});
	it("retains known decisions on a failed refresh", async () => {
		const w = setup();
		await w.refresh("A", true);
		w.load.mockRejectedValueOnce(new Error("offline"));
		await w.refresh("A", true);
		expect(w.items.value).toEqual([sheet, question]);
	});
	it("coalesces terminal events and matches an AR settled name without conversation", async () => {
		vi.useFakeTimers();
		const w = setup();
		await w.refresh("A", true);
		w.load.mockClear();
		w.onEvent({ kind: "sheet:progress", conversation_id: "A", state: "applying" });
		w.onEvent({ kind: "action:settled", name: "unrelated" });
		await vi.runAllTimersAsync();
		expect(w.load).not.toHaveBeenCalled();
		w.onEvent({ kind: "action:settled", name: "AR-1" });
		w.onEvent({ kind: "approval:new", conversation_id: "A" });
		await vi.runAllTimersAsync();
		expect(w.load).toHaveBeenCalledOnce();
	});
	it("preserves the review after its sheet leaves the Pending list", async () => {
		const w = setup();
		await w.refresh("A", true);
		w.openReview(sheet);
		const current = w.reviewSession.value;
		w.load.mockResolvedValueOnce({ items: [] });
		await w.refresh("A", true);
		expect(w.reviewSession.value).toBe(current);
	});
	it("ignores callbacks from an old review and closes the current review once", async () => {
		const w = setup();
		await w.refresh("A", true);
		w.openReview(sheet);
		const old = w.reviewSession.value;
		w.closeReview(old);
		w.openReview(sheet);
		const current = w.reviewSession.value;
		w.onChanged.mockClear();
		w.reviewChanged(old);
		w.closeReview(old);
		expect(w.onChanged).not.toHaveBeenCalled();
		expect(w.reviewSession.value).toBe(current);
		w.closeReview(current);
		w.closeReview(current);
		expect(w.onChanged).toHaveBeenCalledOnce();
		expect(w.reviewSession.value).toBeNull();
	});
	it("invalidates a dialog and pending refresh at navigation and disposal", async () => {
		vi.useFakeTimers();
		const w = setup();
		await w.refresh("A", true);
		w.openReview(sheet);
		const current = w.reviewSession.value;
		w.onEvent({ kind: "approval:new", conversation_id: "A" });
		w.conversation.value = "B";
		await nextTick();
		w.onChanged.mockClear();
		w.closeReview(current);
		await vi.runAllTimersAsync();
		expect(w.onChanged).not.toHaveBeenCalled();
		expect(w.reviewSession.value).toBeNull();
		expect(w.items.value).toEqual([]);
		expect(w.load).toHaveBeenCalledTimes(1);
	});
});
