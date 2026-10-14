// Save as macro takes every eligible prompt in the chat, not only the history pages
// on screen (#715 F13). The SFC function runs here against stubs at its API boundary.
import fs from "fs";
import path from "path";
import { describe, expect, it } from "vitest";

const chat = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
const start = chat.indexOf("async function saveConversationAsMacro(");
const body = chat.slice(start, chat.indexOf("\n}\n", start) + 2);

function harness({ shown, whole, paging }) {
	const s = {
		currentId: { value: "A" },
		messages: { value: shown },
		historyPaging: { value: paging },
		currentTitle: { value: "Month end" },
		prefill: null,
		pushed: null,
		reads: 0,
	};
	const api = {
		getWholeConversation: async (id) => {
			s.reads++;
			expect(id).toBe("A");
			return { messages: whole };
		},
	};
	const run = new Function(
		"currentId",
		"messages",
		"historyPaging",
		"currentTitle",
		"api",
		"setMacroPrefill",
		"router",
		"notifyActionError",
		`${body}\nreturn saveConversationAsMacro;`
	)(
		s.currentId,
		s.messages,
		s.historyPaging,
		s.currentTitle,
		api,
		(draft) => (s.prefill = draft),
		{ push: (to) => (s.pushed = to) },
		(label, e) => {
			throw e;
		}
	);
	return { s, run };
}

const user = (seq, content) => ({ name: `u${seq}`, seq, role: "user", content });
const reply = (seq) => ({ name: `a${seq}`, seq, role: "assistant", content: "done" });
// Three pages of prompts; only the newest page is on screen.
const whole = [
	user(1, "Pull the trial balance"),
	reply(2),
	user(3, "▶ Running macro: month end"),
	reply(4),
	user(5, "Flag accounts over budget"),
	reply(6),
	user(7, "Draft the summary mail"),
	reply(8),
];
const newestPage = whole.slice(6);

describe("Save as macro on a paged chat", () => {
	it("makes every eligible prompt a step without scrolling back", async () => {
		const { s, run } = harness({
			shown: newestPage,
			whole,
			paging: { conv: "A", hasMore: true, before: 7 },
		});
		await run();
		expect(s.reads).toBe(1);
		expect(s.prefill.steps.map((x) => x.prompt)).toEqual([
			"Pull the trial balance",
			"Flag accounts over budget",
			"Draft the summary mail",
		]);
		expect(s.prefill.macro_name).toBe("Month end");
		expect(s.pushed).toBe("/macros/new");
	});

	it("uses the rows on screen when the whole chat is already loaded", async () => {
		const { s, run } = harness({
			shown: whole,
			whole: [],
			paging: { conv: "A", hasMore: false, before: null },
		});
		await run();
		expect(s.reads).toBe(0);
		expect(s.prefill.steps).toHaveLength(3);
	});
});
