import { describe, it, expect, vi, beforeEach } from "vitest";

const report = vi.hoisted(() => vi.fn());
vi.mock("@/lib/errorReporter", () => ({ report }));

import {
	WORN_RETRY_MS,
	_resetForTests,
	isHolidayLoading,
	loadWorn,
	loadedWorn,
} from "./holidayWorn.js";

const art = () => ({ installWornCss: vi.fn(), wornMarkup: vi.fn() });

describe("loadWorn", () => {
	beforeEach(() => {
		_resetForTests();
		report.mockClear();
	});

	it("loads the art once, installs its stylesheet, and keeps it for later marks", async () => {
		const mod = art();
		const load = vi.fn(async () => mod);
		expect(loadedWorn()).toBeNull();
		const [a, b] = await Promise.all([loadWorn({ load }), loadWorn({ load })]);
		expect(a).toBe(mod);
		expect(b).toBe(mod);
		expect(load).toHaveBeenCalledTimes(1);
		expect(mod.installWornCss).toHaveBeenCalledTimes(1);
		expect(loadedWorn()).toBe(mod);
		expect(await loadWorn({ load })).toBe(mod);
		expect(load).toHaveBeenCalledTimes(1);
	});

	it("says it is loading from the moment it starts until it settles", async () => {
		let release;
		const load = () => new Promise((resolve) => (release = resolve));
		expect(isHolidayLoading()).toBe(false);
		const pending = loadWorn({ load });
		// Set at once, before the import can have failed: Vite dispatches its preload error
		// for that import while it is still pending.
		expect(isHolidayLoading()).toBe(true);
		await Promise.resolve();
		expect(isHolidayLoading()).toBe(true);
		release(art());
		await pending;
		expect(isHolidayLoading()).toBe(false);
	});

	it("is still loading when the import's own failure handlers run", async () => {
		let sawLoading = null;
		const load = () =>
			new Promise((_, reject) => {
				// What Vite does: dispatch its preload error, then reject.
				queueMicrotask(() => {
					sawLoading = isHolidayLoading();
					reject(new Error("Importing a module script failed."));
				});
			});
		await loadWorn({ load });
		expect(sawLoading).toBe(true);
		expect(isHolidayLoading()).toBe(false);
	});

	describe("a failed load", () => {
		it("gives null, never throws, and is reported once with fixed text", async () => {
			const now = vi.fn(() => 1_000_000);
			const load = vi.fn(async () => {
				throw new TypeError(
					"Failed to fetch dynamically imported module: /x/holiday-a.js"
				);
			});
			expect(await loadWorn({ load, now })).toBeNull();
			expect(report).toHaveBeenCalledTimes(1);
			const ctx = report.mock.calls[0][0];
			expect(ctx.error_class).toBe("HolidayArtLoadFailed");
			// Neither the browser's message nor anything the reporter drops as a handled
			// chunk error.
			expect(ctx.message).not.toMatch(
				/dynamically imported|module script|chunk|Failed to fetch/i
			);
			// A second failure after the cool-down is not reported again.
			now.mockReturnValue(1_000_000 + WORN_RETRY_MS + 1);
			expect(await loadWorn({ load, now })).toBeNull();
			expect(load).toHaveBeenCalledTimes(2);
			expect(report).toHaveBeenCalledTimes(1);
		});

		it("makes no new attempt for a minute, then tries again", async () => {
			const now = vi.fn(() => 5000);
			const mod = art();
			const load = vi
				.fn()
				.mockRejectedValueOnce(new Error("offline"))
				.mockResolvedValue(mod);
			expect(await loadWorn({ load, now })).toBeNull();
			now.mockReturnValue(5000 + WORN_RETRY_MS - 1);
			expect(await loadWorn({ load, now })).toBeNull();
			expect(load).toHaveBeenCalledTimes(1);
			now.mockReturnValue(5000 + WORN_RETRY_MS + 1);
			expect(await loadWorn({ load, now })).toBe(mod);
			expect(load).toHaveBeenCalledTimes(2);
		});

		it("a stylesheet that cannot be installed counts as a failed load", async () => {
			const mod = art();
			mod.installWornCss.mockImplementation(() => {
				throw new Error("no document");
			});
			expect(await loadWorn({ load: async () => mod })).toBeNull();
			expect(loadedWorn()).toBeNull();
		});
	});
});
