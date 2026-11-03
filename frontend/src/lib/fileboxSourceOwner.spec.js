import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { canSelectFileBoxSource } from "./fileboxSourceOwner";

const chatView = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

describe("File Box source owner gate", () => {
	it("allows only a conversation in the owner-only list", () => {
		expect(canSelectFileBoxSource("C1", [{ name: "C1" }])).toBe(true);
	});

	it.each([
		["a different conversation", "C1", [{ name: "C2" }]],
		["a missing conversation id", "", [{ name: "C1" }]],
		["a loading or failed owner list", "C1", undefined],
	])("fails closed for %s", (_case, conversation, conversations) => {
		expect(canSelectFileBoxSource(conversation, conversations)).toBe(false);
	});

	it("passes the owner gate to the File Box source control", () => {
		expect(chatView).toContain(
			'import { canSelectFileBoxSource } from "@/lib/fileboxSourceOwner";'
		);
		expect(chatView).toMatch(
			/const canSelectFileboxSource = computed\(\s*\(\) =>\s*canSelectFileBoxSource\(currentId\.value, store\.conversations\)\s*\);/
		);
		const sourceControl = chatView.slice(chatView.indexOf("<FileBoxUseAsSource"));
		expect(sourceControl).toContain("canSelectFileboxSource");
		expect(sourceControl).toContain(':can-select="canSelectFileboxSource"');
	});
});
