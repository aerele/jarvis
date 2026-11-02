// What reaches the server, with the REAL error reporter (the other specs mock it). The reporter
// drops anything that looks like a stale-chunk failure, so a scene that did not load must be
// reported with a fixed text and no browser stack: the stack opens with the browser's own
// "Failed to fetch dynamically imported module", which the reporter treats as already handled.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flush } from "@/lib/errorReporter";
import * as desktop from "./holidayReport";
import * as phone from "../../../pwa/src/lib/holidayReport";

describe.each([
	["desktop", desktop],
	["phone", phone],
])("%s scene failure reports", (_name, mod) => {
	beforeEach(() => {
		mod._resetReportedForTests();
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => ({ ok: true }))
		);
	});
	afterEach(() => vi.unstubAllGlobals());

	const sent = () => JSON.parse(fetch.mock.calls[0][1].body).errors;

	it("a scene that would not load reaches the server, with the design and no stack", async () => {
		const browserError = new TypeError("Failed to fetch dynamically imported module: /x.js");
		mod.reportSceneFailure("load", "diwali", browserError);
		await flush();
		expect(fetch).toHaveBeenCalledTimes(1);
		expect(sent()).toHaveLength(1);
		expect(sent()[0]).toMatchObject({
			error_class: "HolidaySceneLoadFailed",
			error_code: "holiday_scene:diwali",
			stack: "",
		});
	});

	it("a scene that threw while playing keeps its stack", async () => {
		const error = new Error("boom");
		mod.reportSceneFailure("play", "christmas", error);
		await flush();
		expect(sent()[0]).toMatchObject({
			error_code: "holiday_scene:christmas",
			stack: error.stack,
		});
	});
});
