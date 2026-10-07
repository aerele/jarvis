import { describe, expect, it } from "vitest";
import { replyBarParts, stampDeltaTime } from "./replyBar";

describe("replyBarParts", () => {
	it("shows the bar (time) while streaming but not the copy button", () => {
		expect(replyBarParts({ content: "Hel", streaming: true })).toEqual({
			showBar: true,
			showCopy: false,
		});
	});
	it("shows the copy button once streaming ends", () => {
		expect(replyBarParts({ content: "Hello.", streaming: false }).showCopy).toBe(true);
	});
	it("shows nothing for an empty or errored reply", () => {
		expect(replyBarParts({ content: "", streaming: true }).showBar).toBe(false);
		expect(replyBarParts({ content: "x", error: "boom" }).showBar).toBe(false);
	});
});

describe("stampDeltaTime", () => {
	it("follows each delta so the end time equals the last delta", () => {
		const m = { content: "Hel", streaming: true };
		stampDeltaTime(m, 1000);
		expect(m.creation_browser).toBe(1000);
		stampDeltaTime(m, 2500);
		expect(m.creation_browser).toBe(2500);
	});
	it("leaves a row that already has a server time alone", () => {
		const m = { modified: "2026-10-07 10:00:00" };
		stampDeltaTime(m, 1000);
		expect(m.creation_browser).toBeUndefined();
		const n = { creation: "2026-10-07 10:00:00" };
		stampDeltaTime(n, 1000);
		expect(n.creation_browser).toBeUndefined();
	});
});
