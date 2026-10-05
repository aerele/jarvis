import { describe, it, expect, vi } from "vitest";

// frappe-ui's ESM entry does not resolve under vitest (see LlmPoolEditor.spec.js) -
// fmtElapsed itself is pure, but datetime.js imports frappe-ui at module scope, so
// any spec importing this module needs the mock even though fmtElapsed never calls it.
const config = vi.hoisted(() => ({}));
vi.mock("frappe-ui", async () => ({
	call: vi.fn(),
	// frappe-ui re-exports dayjs; the real one, so formatLocalMs is checked for real.
	dayjs: (await vi.importActual("dayjs")).default,
	// `config.viewerClock`: what a server datetime reads as on the viewer's clock.
	dayjsLocal: () => ({
		format: () => config.viewerClock ?? "",
		fromNow: () => "",
		isValid: () => false,
	}),
	getConfig: (key) => config[key] ?? null,
}));

import {
	fmtElapsed,
	formatLocalMs,
	formatTime12h,
	onAnotherClock,
	siteTimezone,
} from "./datetime";

describe("formatTime12h", () => {
	it("prints a stored time-of-day the way the time picker shows it", () => {
		expect(formatTime12h("09:00")).toBe("9:00 am");
		expect(formatTime12h("09:00:00")).toBe("9:00 am");
		expect(formatTime12h("00:05")).toBe("12:05 am");
		expect(formatTime12h("12:00")).toBe("12:00 pm");
		expect(formatTime12h("17:30:00")).toBe("5:30 pm");
	});

	it("is empty for anything that is not a time", () => {
		expect(formatTime12h("")).toBe("");
		expect(formatTime12h(null)).toBe("");
		expect(formatTime12h("soon")).toBe("");
	});
});

describe("siteTimezone", () => {
	it("is the zone the shell was told, or empty when it was not", () => {
		expect(siteTimezone()).toBe("");
		config.systemTimezone = "Asia/Kolkata";
		expect(siteTimezone()).toBe("Asia/Kolkata");
		delete config.systemTimezone;
	});
});

describe("onAnotherClock", () => {
	// A server datetime is stored on the site's clock and shown on the viewer's.
	it("is true when the viewer's clock reads it differently from the stored value", () => {
		config.viewerClock = "2026-10-02 04:30:00";
		expect(onAnotherClock("2026-10-02 09:00:00")).toBe(true);
		delete config.viewerClock;
	});

	it("is false when the two read the same, whatever the zones are called", () => {
		// Asia/Kolkata and Asia/Calcutta are one clock: compared by what is shown,
		// not by name.
		config.viewerClock = "2026-10-02 09:00:00";
		expect(onAnotherClock("2026-10-02 09:00:00")).toBe(false);
		expect(onAnotherClock("2026-10-02 09:00:00.000000")).toBe(false);
		delete config.viewerClock;
	});

	it("is false with nothing to compare", () => {
		expect(onAnotherClock("")).toBe(false);
		expect(onAnotherClock(null)).toBe(false);
		// The mock's default: nothing came back from the conversion.
		expect(onAnotherClock("2026-10-02 09:00:00")).toBe(false);
	});
});

// A reply stamped in the browser at run:end shows its time in the same format
// the server copy gets after the reload ("10:42 PM", never "22:42" first).
describe("formatLocalMs", () => {
	it("formats a browser epoch in the local zone with the caller's pattern", () => {
		const ms = new Date(2026, 8, 29, 22, 42).getTime();
		expect(formatLocalMs(ms, "h:mm A")).toBe("10:42 PM");
		expect(formatLocalMs(ms, "ddd, MMM D, YYYY h:mm A")).toBe("Tue, Sep 29, 2026 10:42 PM");
	});

	it("is empty without a stamp", () => {
		expect(formatLocalMs(0, "h:mm A")).toBe("");
		expect(formatLocalMs(undefined, "h:mm A")).toBe("");
	});
});

// jarvis#1062 C3: the running-run progress display's ticking elapsed-time
// label - mm:ss under an hour, h:mm at/above it.
describe("fmtElapsed", () => {
	it("renders mm:ss under an hour", () => {
		expect(fmtElapsed(0)).toBe("00:00");
		expect(fmtElapsed(5)).toBe("00:05");
		expect(fmtElapsed(65)).toBe("01:05");
		expect(fmtElapsed(3599)).toBe("59:59");
	});

	it("renders h:mm at and above an hour", () => {
		expect(fmtElapsed(3600)).toBe("1:00");
		expect(fmtElapsed(3661)).toBe("1:01");
		expect(fmtElapsed(7325)).toBe("2:02");
	});

	it("clamps negative/NaN/missing input to zero instead of printing garbage", () => {
		expect(fmtElapsed(-5)).toBe("00:00");
		expect(fmtElapsed(NaN)).toBe("00:00");
		expect(fmtElapsed(undefined)).toBe("00:00");
		expect(fmtElapsed(null)).toBe("00:00");
	});

	it("floors fractional seconds", () => {
		expect(fmtElapsed(65.9)).toBe("01:05");
	});
});
