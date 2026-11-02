// Source-level checks (the view is too big to mount): a macro run's chat is not in
// the chat list (jarvis/chat/api.py list_conversations), and ChatView must still keep
// one open when it is opened from the Runs page, the macro page or a notification.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

describe("ChatView keeps open a chat the list never shows", () => {
	it("drops the open chat only when it was in the list before and is gone now", () => {
		const at = src.indexOf("watch(\n\t() => store.conversations,");
		expect(at).toBeGreaterThan(-1);
		const body = src.slice(at, src.indexOf("\n);", at));
		const guard = body.indexOf(
			"if (!(previous || []).some((c) => c.name === currentId.value)) return;"
		);
		expect(guard).toBeGreaterThan(-1);
		// the guard sits before the reset it protects
		expect(guard).toBeLessThan(body.indexOf("currentId.value = null;"));
		expect(body).toMatch(/\(list, previous\) =>/);
	});
});
