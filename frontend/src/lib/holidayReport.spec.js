import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/errorReporter", () => ({ report: vi.fn() }));

import { report as sink } from "@/lib/errorReporter";
import { reportOnce, _resetReportedForTests } from "./holidayReport";

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
