import fs from "node:fs";
import path from "node:path";
import { describe, it, expect, vi, beforeEach } from "vitest";

const state = vi.hoisted(() => ({ loading: false }));
vi.mock("@/lib/holidayWorn", () => ({ isHolidayLoading: () => state.loading }));

import {
	createPreloadErrorHandler,
	isHolidayChunk,
	isHolidayPreloadError,
} from "./preloadGuard.js";

// What the router's own test for a stale chunk looks for (router/index.js).
const isChunkLoadError = (err) =>
	/dynamically imported module|Importing a module script failed/i.test(
		String((err && (err.message || err)) || "")
	);

const SAFARI = new TypeError("Importing a module script failed.");
const CHROME_HOLIDAY = new TypeError(
	"Failed to fetch dynamically imported module: https://x.test/assets/jarvis/frontend/assets/holiday-worn-AbC123.js"
);
const CHROME_ROUTE = new TypeError(
	"Failed to fetch dynamically imported module: https://x.test/assets/jarvis/frontend/assets/MacrosView-AbC123.js"
);

function fire(handler, payload) {
	const event = { payload, preventDefault: vi.fn() };
	handler(event);
	return event;
}

describe("the holiday chunk matcher", () => {
	it("matches a holiday-* file name anywhere in the message, and nothing else", () => {
		expect(isHolidayChunk(CHROME_HOLIDAY)).toBe(true);
		expect(isHolidayChunk("error loading /assets/holiday-worn-x.js")).toBe(true);
		expect(isHolidayChunk(CHROME_ROUTE)).toBe(false);
		expect(isHolidayChunk(SAFARI)).toBe(false);
		expect(isHolidayChunk(undefined)).toBe(false);
		expect(isHolidayChunk({ message: "/assets/not-holiday-x.js" })).toBe(false);
	});
});

describe("vite:preloadError", () => {
	let recover;
	let handler;
	beforeEach(() => {
		state.loading = false;
		recover = vi.fn();
		handler = createPreloadErrorHandler({ isChunkLoadError, recover });
	});

	it("recovers a stale route chunk with one reload", () => {
		const event = fire(handler, CHROME_ROUTE);
		expect(event.preventDefault).toHaveBeenCalled();
		expect(recover).toHaveBeenCalledTimes(1);
	});

	it("leaves a holiday chunk alone when the message names it", () => {
		const event = fire(handler, CHROME_HOLIDAY);
		expect(event.preventDefault).not.toHaveBeenCalled();
		expect(recover).not.toHaveBeenCalled();
	});

	it("leaves Safari's nameless message alone while the holiday art is loading", () => {
		state.loading = true;
		expect(isHolidayPreloadError(SAFARI)).toBe(true);
		const event = fire(handler, SAFARI);
		expect(event.preventDefault).not.toHaveBeenCalled();
		expect(recover).not.toHaveBeenCalled();
	});

	it("still recovers Safari's message when the holiday art is not loading", () => {
		const event = fire(handler, SAFARI);
		expect(event.preventDefault).toHaveBeenCalled();
		expect(recover).toHaveBeenCalledTimes(1);
	});

	it("ignores an event that is not a chunk failure", () => {
		const event = fire(handler, new Error("something else"));
		expect(event.preventDefault).not.toHaveBeenCalled();
		expect(recover).not.toHaveBeenCalled();
	});

	it("copes with an event with no payload", () => {
		expect(() => handler({ preventDefault: vi.fn() })).not.toThrow();
		expect(() => handler(undefined)).not.toThrow();
	});
});

describe("router wiring", () => {
	it("registers the guarded handler for vite:preloadError", () => {
		const src = fs.readFileSync(path.resolve(__dirname, "index.js"), "utf8");
		expect(src).toMatch(
			/addEventListener\(\s*"vite:preloadError",\s*createPreloadErrorHandler\(\{[\s\S]*?isChunkLoadError: _isChunkLoadError/
		);
		// One listener only: a second, unguarded one would reload the page anyway.
		expect(src.match(/addEventListener\(\s*"vite:preloadError"/g).length).toBe(1);
	});
});
