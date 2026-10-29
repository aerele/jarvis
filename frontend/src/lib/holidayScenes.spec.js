import { describe, it, expect, vi, beforeEach } from "vitest";
import fs from "node:fs";
import { DESIGNS } from "@jsshared/holiday/answer.mjs";
import path from "node:path";

const reported = vi.hoisted(() => vi.fn());
vi.mock("@/lib/errorReporter", () => ({ report: reported }));

import {
	SCENE_RETRY_MS,
	SCENE_LOADERS,
	_resetForTests,
	hasScene,
	isSceneLoading,
	loadScene,
	sceneClearance,
} from "./holidayScenes";

const scene = { play: () => ({}) };

describe("the scene loader map", () => {
	beforeEach(() => {
		_resetForTests();
		reported.mockClear();
	});

	it("maps all nine designs, each with a literal import the bundler can name holiday-*", () => {
		const designs = [
			"birthday",
			"christmas",
			"diwali",
			"eid",
			"holi",
			"independence",
			"newyear",
			"onam",
			"pongal",
		];
		for (const design of designs) expect(hasScene(design)).toBe(true);
		expect(hasScene("ramadan")).toBe(false);
		expect(hasScene("toString")).toBe(false);
		expect(hasScene(undefined)).toBe(false);
		const source = fs.readFileSync(path.resolve(__dirname, "holidayScenes.js"), "utf8");
		for (const design of designs)
			expect(source).toContain(`import("@jsshared/holiday/scenes/${design}.mjs")`);
		expect(Object.keys(SCENE_LOADERS).sort()).toEqual(designs);
		// the answer reader accepts exactly these designs (republic plays "independence")
		expect(designs).toEqual([...DESIGNS].sort());
	});

	it("loads once and keeps the module; a design with no scene gives null without loading", async () => {
		const load = vi.fn(async () => scene);
		const loaders = { diwali: load };
		expect(await loadScene("diwali", { loaders })).toBe(scene);
		expect(await loadScene("diwali", { loaders })).toBe(scene);
		expect(load).toHaveBeenCalledTimes(1);
		expect(await loadScene("ramadan", { loaders })).toBeNull();
	});

	it("is loading while the import is pending, so the router leaves its error alone", async () => {
		let finish;
		const loaders = { diwali: () => new Promise((resolve) => (finish = resolve)) };
		const pending = loadScene("diwali", { loaders });
		expect(isSceneLoading()).toBe(true);
		await Promise.resolve();
		finish(scene);
		await pending;
		expect(isSceneLoading()).toBe(false);
	});

	it("a failed or undefined import is no scene: null, no throw, reported once, not retried for a minute", async () => {
		let clock = 1000;
		const now = () => clock;
		const loaders = {
			diwali: vi.fn(async () => {
				throw new TypeError("Failed to fetch dynamically imported module");
			}),
		};
		expect(await loadScene("diwali", { loaders, now })).toBeNull();
		expect(await loadScene("diwali", { loaders, now })).toBeNull();
		expect(loaders.diwali).toHaveBeenCalledTimes(1);
		expect(reported).toHaveBeenCalledTimes(1);
		expect(reported.mock.calls[0][0]).toMatchObject({
			error_class: "HolidaySceneLoadFailed",
			error_code: "holiday_scene:diwali",
			stack: expect.stringContaining("Failed to fetch dynamically imported module"),
		});
		clock += SCENE_RETRY_MS;
		expect(
			await loadScene("diwali", { loaders: { diwali: async () => undefined }, now })
		).toBeNull();
		expect(reported).toHaveBeenCalledTimes(1);
		expect(isSceneLoading()).toBe(false);
	});
});

describe("sceneClearance", () => {
	it("is the art's own clearance, scaled with the stage; a design with nothing overhead has none", () => {
		const one = sceneClearance("diwali", 1);
		expect(one).toBeGreaterThan(37); // below the toran, whose bottom is 37 units down
		expect(sceneClearance("diwali", 1.25)).toBeCloseTo(one * 1.25, 6);
		for (const design of [
			"christmas",
			"newyear",
			"birthday",
			"eid",
			"pongal",
			"onam",
			"independence",
		])
			expect(sceneClearance(design, 1)).toBeGreaterThan(30);
		expect(sceneClearance("holi", 1)).toBe(0);
		expect(sceneClearance("toString", 1)).toBe(0);
	});
});
