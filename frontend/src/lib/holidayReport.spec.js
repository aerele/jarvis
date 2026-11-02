import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/errorReporter", () => ({ report: vi.fn() }));

import { report as sink } from "@/lib/errorReporter";
import { reportOnce, reportSceneFailure, _resetReportedForTests } from "./holidayReport";

describe("reportOnce", () => {
	beforeEach(() => {
		_resetReportedForTests();
		sink.mockClear();
	});

	it("reports a key once per session, and another key separately", () => {
		reportOnce("load", { message: "a" });
		reportOnce("load", { message: "b" });
		reportOnce("play", { message: "c" });
		expect(sink.mock.calls.map((c) => c[0].message)).toEqual(["a", "c"]);
	});

	it("uses the reporter it is given instead of the default", () => {
		const own = vi.fn();
		reportOnce("play", { message: "x" }, own);
		expect(own).toHaveBeenCalledTimes(1);
		expect(sink).not.toHaveBeenCalled();
	});
});

describe("reportSceneFailure", () => {
	beforeEach(() => {
		_resetReportedForTests();
		sink.mockClear();
	});

	it("names the design in error_code, keeps the stack, and fits the reporter's 64 characters", () => {
		const error = new Error("boom");
		reportSceneFailure("play", "independence", error);
		expect(sink).toHaveBeenCalledWith({
			error_class: "HolidaySceneFailed",
			error_code: "holiday_scene:independence",
			message: "The holiday scene failed while playing",
			stack: error.stack,
		});
		expect(sink.mock.calls[0][0].error_code.length).toBeLessThanOrEqual(64);
	});

	it("reports once per design and kind", () => {
		reportSceneFailure("play", "christmas", new Error("a"));
		reportSceneFailure("play", "christmas", new Error("b"));
		reportSceneFailure("play", "newyear", new Error("c"));
		reportSceneFailure("load", "christmas", new Error("d"));
		expect(sink.mock.calls.map((c) => c[0].error_code)).toEqual([
			"holiday_scene:christmas",
			"holiday_scene:newyear",
			"holiday_scene:christmas",
		]);
		expect(sink.mock.calls[2][0].error_class).toBe("HolidaySceneLoadFailed");
	});

	it("only the nine designs go into error_code; anything else is unknown, and a missing error has no stack", () => {
		reportSceneFailure("load", "x".repeat(200), undefined);
		expect(sink.mock.calls[0][0]).toMatchObject({
			error_code: "holiday_scene:unknown",
			stack: "",
		});
	});
});
