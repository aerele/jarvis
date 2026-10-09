import { describe, expect, it } from "vitest";
import { replyBarParts, stampDeltaTime, useClientStamp } from "./replyBar";

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
	it("stamps a resumed row that already has a server creation", () => {
		const m = { creation: "2026-10-07 10:00:00", streaming: true };
		stampDeltaTime(m, 3000);
		expect(m.creation_browser).toBe(3000);
		expect(useClientStamp(m)).toBe(true);
	});
});

describe("useClientStamp", () => {
	it("prefers the client time only while streaming", () => {
		expect(useClientStamp({ streaming: true, creation_browser: 1 })).toBe(true);
		expect(useClientStamp({ streaming: false, creation_browser: 1 })).toBe(false);
		expect(useClientStamp({ streaming: true })).toBe(false);
	});
});
