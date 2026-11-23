import fs from "node:fs";
import { computed, ref } from "vue";
import { describe, expect, it } from "vitest";

// A chat bound to one of the user's own agents names it in the header, on the line that
// otherwise reads "Your AI Teammate" or the message count. The decision is read out of the
// real SFC (ChatView has too large a dependency graph to mount here) and run on refs.
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
function computedConst(name) {
	const start = source.indexOf(`const ${name} = computed(`);
	if (start < 0) throw new Error("Missing SFC computed " + name);
	const ends = ["\n);", "\n});"].map((e) => source.indexOf(e, start)).filter((i) => i > 0);
	const end = Math.min(...ends);
	return source.slice(start, source.indexOf(";", end) + 1);
}

function harness({ conversations = [], currentId = "CONV-1", loaded = "", messages = 0 } = {}) {
	const state = {
		computed,
		store: { conversations },
		currentId: ref(currentId),
		loadedUserAgentName: ref(loaded),
		msgCount: ref(messages),
	};
	const read = new Function(
		...Object.keys(state),
		`${computedConst("currentUserAgentName")}
		${computedConst("headerSub")}
		return { currentUserAgentName, headerSub };`
	)(...Object.values(state));
	return { ...state, ...read };
}

describe("chat header names the agent of an agent chat", () => {
	it("keeps the usual line for an ordinary chat", () => {
		expect(harness().headerSub.value).toBe("Your AI Teammate");
		expect(harness({ messages: 4 }).headerSub.value).toBe("2 messages");
	});

	it("names the agent from the fetched conversation, before any message", () => {
		const h = harness({ loaded: "Overdue invoices" });
		expect(h.headerSub.value).toBe("Overdue invoices");
	});

	it("puts the message count after the name", () => {
		const h = harness({ loaded: "Overdue invoices", messages: 4 });
		expect(h.headerSub.value).toBe("Overdue invoices · 2 messages");
	});

	it("prefers the sidebar row's name and ignores other chats' rows", () => {
		const rows = [
			{ name: "CONV-2", user_agent_name: "Someone else's" },
			{ name: "CONV-1", user_agent_name: "Stock watcher" },
		];
		expect(harness({ conversations: rows, loaded: "Old" }).headerSub.value).toBe(
			"Stock watcher"
		);
		expect(harness({ conversations: rows.slice(0, 1) }).headerSub.value).toBe(
			"Your AI Teammate"
		);
	});

	it("loads the name with the conversation and clears it for a new chat", () => {
		expect(source).toContain(
			'loadedUserAgentName.value = d?.conversation?.user_agent_name || "";'
		);
		// Cleared wherever the loaded title is: both new-chat paths.
		const clears = source.match(/loadedUserAgentName\.value = "";/g) || [];
		const titleClears = source.match(/loadedConvTitle\.value = "";/g) || [];
		expect(clears.length).toBe(titleClears.length);
	});
});
