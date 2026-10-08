import { describe, expect, it } from "vitest";

import {
	SILENT_RUN_RESYNC_MS,
	isSilentRun,
	shouldStopSpinning,
	streamingRowIsLive,
} from "./silentRun";

const NOW = Date.parse("2026-10-06T10:00:00Z");
// The server's `modified` is a local "YYYY-MM-DD HH:MM:SS" string, read as local time.
const ago = (ms) => {
	const d = new Date(NOW - ms);
	const p = (n) => String(n).padStart(2, "0");
	return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(
		d.getMinutes()
	)}:${p(d.getSeconds())}`;
};

describe("isSilentRun (#590)", () => {
	it("only a busy chat that heard nothing for the whole window", () => {
		expect(isSilentRun(true, NOW - SILENT_RUN_RESYNC_MS, NOW)).toBe(true);
		expect(isSilentRun(true, NOW - SILENT_RUN_RESYNC_MS + 1, NOW)).toBe(false);
		expect(isSilentRun(false, 0, NOW)).toBe(false);
	});
});

describe("streamingRowIsLive (#591)", () => {
	it("a row written in the last five minutes is live", () => {
		expect(streamingRowIsLive({ modified: ago(60 * 1000) }, NOW)).toBe(true);
		expect(streamingRowIsLive({ modified: ago(6 * 60 * 1000) }, NOW)).toBe(false);
	});
	it("an older row is live while the server says its turn has not ended", () => {
		// A long tool call writes nothing for minutes; reopening the chat used to
		// drop the spinner and show it as stopped.
		for (const state of ["dispatching", "streaming", "finalizing", "recovering"])
			expect(
				streamingRowIsLive({ modified: ago(20 * 60 * 1000), turn_state: state }, NOW)
			).toBe(true);
		for (const state of ["done", "errored", "cancelled"])
			expect(
				streamingRowIsLive({ modified: ago(20 * 60 * 1000), turn_state: state }, NOW)
			).toBe(false);
	});
	it("no row, or a row with no time, is not live", () => {
		expect(streamingRowIsLive(null, NOW)).toBe(false);
		expect(streamingRowIsLive({}, NOW)).toBe(false);
	});
});

describe("shouldStopSpinning (#590)", () => {
	it("stops only when nothing on the server is still running for this chat", () => {
		expect(shouldStopSpinning({ busy: true, resumed: false, preStream: null })).toBe(true);
		expect(shouldStopSpinning({ busy: true, resumed: true, preStream: null })).toBe(false);
		expect(
			shouldStopSpinning({ busy: true, resumed: false, preStream: { state: "queued" } })
		).toBe(false);
		expect(shouldStopSpinning({ busy: false, resumed: false, preStream: null })).toBe(false);
	});
});
