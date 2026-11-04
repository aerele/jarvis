import { describe, expect, it } from "vitest";
import { anchorFileboxWaits, waitStamp } from "./fileboxWaitAnchors";

// The thread of Aerele-RnD/jarvis-admin-v2#763: three replies, each ending in a
// card the File Box policy turned into a board question at its turn's end.
const thread = [
	{ name: "U1", role: "user", creation: "2026-10-30 10:00:00.000001" },
	{ name: "A1", role: "assistant", creation: "2026-10-30 10:00:01" },
	{ name: "T1", role: "tool", creation: "2026-10-30 10:00:05" },
	{ name: "U2", role: "user", creation: "2026-10-30 10:05:00" },
	{ name: "A2", role: "assistant", creation: "2026-10-30 10:05:01.5" },
	{ name: "U3", role: "user", creation: "2026-10-30 10:10:00" },
	{ name: "A3", role: "assistant", creation: "2026-10-30 10:10:01" },
];
const wait = (name, creation, extra = {}) => ({ src: "ar", name, creation, ...extra });

describe("anchorFileboxWaits", () => {
	it("puts each wait under the reply whose turn filed it", () => {
		const w1 = wait("AR-1", "2026-10-30 10:00:09.250000");
		const w2 = wait("AR-2", "2026-10-30 10:05:30");
		const w3 = wait("PA-1", "2026-10-30 10:10:02", { src: "pa" });
		const { byMessage, rest } = anchorFileboxWaits([w1, w2, w3], thread);
		expect(byMessage).toEqual({ A1: [w1], A2: [w2], A3: [w3] });
		expect(rest).toEqual([]);
	});

	it("keeps the thread's order inside one reply and never anchors to a user or tool row", () => {
		const a = wait("AR-1", "2026-10-30 10:00:05.000001"); // after the tool row T1
		const b = wait("AR-2", "2026-10-30 10:00:09");
		const { byMessage } = anchorFileboxWaits([a, b], thread);
		expect(byMessage).toEqual({ A1: [a, b] });
	});

	it("a wait filed before any reply, or without a usable stamp, stays at the bottom", () => {
		const early = wait("AR-0", "2026-10-30 09:59:59");
		const blank = wait("AR-X", "");
		const missing = { src: "sheet", name: "SH-1" };
		const { byMessage, rest } = anchorFileboxWaits([early, blank, missing], thread);
		expect(byMessage).toEqual({});
		expect(rest).toEqual([early, blank, missing]);
	});

	it("a reply row without a server stamp (the live turn's placeholder) is skipped", () => {
		const live = [
			{ name: "A1", role: "assistant" },
			{ name: "A2", role: "assistant", creation: null },
		];
		const w = wait("AR-1", "2026-10-30 10:00:09");
		expect(anchorFileboxWaits([w], live)).toEqual({ byMessage: {}, rest: [w] });
		expect(anchorFileboxWaits([], thread)).toEqual({ byMessage: {}, rest: [] });
		expect(anchorFileboxWaits(null, null)).toEqual({ byMessage: {}, rest: [] });
	});

	it("waitStamp compares a datetime with and without a microsecond part", () => {
		expect(waitStamp("2026-10-30 10:00:01")).toBe("2026-10-30 10:00:01.000000");
		expect(waitStamp("2026-10-30T10:00:01.5")).toBe("2026-10-30 10:00:01.500000");
		expect(waitStamp("2026-10-30 10:00:01") < waitStamp("2026-10-30 10:00:01.000001")).toBe(
			true
		);
		expect(waitStamp("yesterday")).toBe(null);
		expect(waitStamp(undefined)).toBe(null);
	});
});
