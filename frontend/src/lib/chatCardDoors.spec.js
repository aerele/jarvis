// What the desktop does with the server's answer to Create / Confirm, Approve & run and Cancel /
// Discard of a pending card, and that it does exactly that after the decision was moved out of the
// view into lib/chatCardActions.js (confirmReplyOutcome, dismissReplyOutcome, thrownCardError).
//
// chatCardDoors.golden.json holds what the OLD in-view handlers did for every reply in
// chatCardReplyCases.js: the ordered effects (api calls with the card's busy state, toasts,
// reloads, every write to the list, the card and the view's refs) and the end state, for a card
// that is a draft and for one that is not, plus six scenarios around the call (the chat switched
// during the call or the reload, the card leaving the list, a running turn, a busy card, another
// chat's card). The default test runs the CURRENT handlers, cut out of the working tree's
// ChatView.vue, over the same table and requires the same record.
//
// To regenerate the golden from the old source (needs git history; the in-view ladder no longer
// exists in the working tree):
//   CHAT_CARD_DOORS_GOLDEN_FROM=aa6884300 npx vitest run src/lib/chatCardDoors.spec.js
// It reads `git show <ref>:frontend/src/views/ChatView.vue`, cuts out the handlers, runs every row
// and rewrites the golden. Regenerating from aa6884300 (the commit before the move) must leave
// `git diff` on the golden empty; a hotfix line's own ChatView.vue works the same way.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { NOW_MS, allRuns, recordAll } from "./chatCardDoorsRunner.js";

const GOLDEN = path.resolve(__dirname, "chatCardDoors.golden.json");
const VIEW = path.resolve(__dirname, "../views/ChatView.vue");
// Through JSON, as the golden file is.
const viaJson = (v) => JSON.parse(JSON.stringify(v));

beforeEach(() => {
	// the card's expiry stamp is read against the wall clock
	vi.useFakeTimers({ toFake: ["Date"] });
	vi.setSystemTime(NOW_MS);
});
afterEach(() => vi.useRealTimers());

const ref = process.env.CHAT_CARD_DOORS_GOLDEN_FROM;
describe.skipIf(!ref)("regenerate the golden from the old in-view handlers", () => {
	it("writes chatCardDoors.golden.json", async () => {
		const root = execFileSync("git", ["rev-parse", "--show-toplevel"], {
			encoding: "utf8",
		}).trim();
		const source = execFileSync("git", ["show", `${ref}:frontend/src/views/ChatView.vue`], {
			cwd: root,
			encoding: "utf8",
			maxBuffer: 64 * 1024 * 1024,
		});
		const record = await recordAll(source);
		fs.writeFileSync(GOLDEN, JSON.stringify(record, null, 1) + "\n");
		expect(Object.keys(record).length).toBe(allRuns().length);
	});
});

describe.skipIf(!!ref)("the desktop's handlers do what they did before the decision moved", () => {
	const loadGolden = () => JSON.parse(fs.readFileSync(GOLDEN, "utf8"));

	// Break: any change in what a handler does with a reply or an error: which calls it makes and
	// in what order, the card's error text and shape, the toast, whether and when the list is
	// reloaded, what is removed, what is settled, the queued chip, what stays marked busy or in
	// flight when the call ends, and what a view that changed during the call is left with. The
	// pins that cut these handlers out of the view check the lines; no other test runs them.
	it("every run has the effects and end state of the old handlers", async () => {
		const golden = loadGolden();
		const got = viaJson(await recordAll(fs.readFileSync(VIEW, "utf8")));
		expect(Object.keys(got)).toEqual(Object.keys(golden));
		for (const key of Object.keys(golden)) expect(got[key], key).toEqual(golden[key]);
	});

	// The golden is only worth something while it holds real effects: a cut that matched nothing,
	// or a fake that stopped being called, would regenerate an empty golden and pass the test above.
	it("the golden holds the branches the handlers have", () => {
		const golden = loadGolden();
		const names = (key) => golden[key].effects.map((e) => e[0]);
		const only = (key) => golden[key];
		// success: the card goes, the chat reloads, the list is refreshed (the removed card's busy flag is
		// not cleared: it is not on the list any more)
		expect(names("normal | confirm | ordinary | ok")).toEqual([
			"card t1.busy",
			"card t1.error",
			"api.confirmTool",
			"pendingActions",
			"loadConversation",
			"store.loadConversations",
		]);
		// a kept card: the object the server sent is the card's error, and it is toasted
		const kept = only("normal | confirm | ordinary | storage unavailable (confirm_tool)");
		expect(kept.end.cards[0].error.type).toBe("ConfirmationUnavailableError");
		expect(kept.end.pending).toEqual(["t1", "t2"]);
		// a refused Confirm that spends the card toasts and does not reload
		const stale = only(
			"normal | confirm | ordinary | stale: the record changed after it was proposed"
		);
		expect(stale.effects.map((e) => e[0])).not.toContain("loadConversation");
		expect(stale.effects.find((e) => e[0] === "notify")[1]).toMatch(
			/changed after this was proposed/
		);
		// the draft's Create reloads the receipt that the refused Confirm did not
		const draftStale = only(
			"normal | confirm | draft | stale: the record changed after it was proposed"
		);
		expect(draftStale.effects.map((e) => e[0])).toContain("loadConversation");
		expect(draftStale.effects.find((e) => e[0] === "notify")[1]).toMatch(
			/after the draft was made/
		);
		// a thrown call: the card keeps an error and is usable again
		const thrown = only("normal | dismiss | ordinary | thrown: the server said why (417)");
		expect(thrown.end.cards[0]).toMatchObject({
			busy: false,
			error: { message: "Due Date is required." },
		});
		// the sibling card is never touched
		for (const key of Object.keys(golden)) {
			expect(
				golden[key].effects.filter((e) => e[0].startsWith("card t2")),
				key
			).toEqual([]);
		}
	});

	// The rows are the table the shared decision is tested over too; a row added there is run here.
	it("has a run for every door, item kind, scenario and reply", () => {
		const runs = allRuns();
		expect(new Set(runs.map(([key]) => key)).size).toBe(runs.length);
		expect(Object.keys(loadGolden()).length).toBe(runs.length);
	});
});
