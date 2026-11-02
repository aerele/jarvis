// The scene loader on both surfaces: the desktop original and the phone's twin
// (pwa/src/lib/holidayScenes.js) must load the same nine designs and fail the same way.
// The error reporter is mocked at the file both surfaces reach it through.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DESIGNS } from "@jsshared/holiday/answer.mjs";

const reported = vi.hoisted(() => vi.fn());
vi.mock("@/lib/errorReporter", () => ({ report: reported }));

import * as desktop from "./holidayScenes";
import * as phone from "../../../pwa/src/lib/holidayScenes";

const scene = { play: () => ({}) };
const loadFailure = () => new TypeError("Failed to fetch dynamically imported module");

describe.each([
	["desktop", desktop],
	["phone", phone],
])("%s scene loader", (_name, mod) => {
	beforeEach(() => {
		mod._resetForTests();
		reported.mockClear();
	});

	it("has a loader for each of the nine designs and for nothing else", () => {
		expect(Object.keys(mod.SCENE_LOADERS).sort()).toEqual([...DESIGNS].sort());
		expect(DESIGNS).toHaveLength(9);
		for (const design of DESIGNS) expect(mod.hasScene(design)).toBe(true);
		for (const other of ["ramadan", "toString", "__proto__", undefined, 7])
			expect(mod.hasScene(other)).toBe(false);
	});

	it("the two surfaces map the same designs", () => {
		expect(Object.keys(desktop.SCENE_LOADERS).sort()).toEqual(
			Object.keys(phone.SCENE_LOADERS).sort()
		);
	});

	it("a failed import is no scene: null, one report, no new attempt for a minute", async () => {
		let clock = 1000;
		const now = () => clock;
		const loader = vi.fn(async () => {
			throw loadFailure();
		});
		const loaders = { diwali: loader };
		expect(await mod.loadScene("diwali", { loaders, now })).toBeNull();
		expect(await mod.loadScene("diwali", { loaders, now })).toBeNull();
		expect(loader).toHaveBeenCalledTimes(1);
		expect(reported).toHaveBeenCalledTimes(1);
		expect(reported.mock.calls[0][0]).toMatchObject({
			error_class: "HolidaySceneLoadFailed",
			error_code: "holiday_scene:diwali",
			stack: "",
		});
	});

	it("tries again after a minute, loads, and keeps the module", async () => {
		let clock = 1000;
		const now = () => clock;
		let broken = true;
		const loader = vi.fn(async () => {
			if (broken) throw loadFailure();
			return scene;
		});
		const loaders = { diwali: loader };
		expect(await mod.loadScene("diwali", { loaders, now })).toBeNull();
		clock += mod.SCENE_RETRY_MS - 1;
		expect(await mod.loadScene("diwali", { loaders, now })).toBeNull();
		expect(loader).toHaveBeenCalledTimes(1);
		clock += 1;
		broken = false;
		expect(await mod.loadScene("diwali", { loaders, now })).toBe(scene);
		expect(await mod.loadScene("diwali", { loaders, now })).toBe(scene);
		expect(loader).toHaveBeenCalledTimes(2);
	});

	it("a module that is not a scene counts as a failed load", async () => {
		const loaders = { diwali: async () => ({}), christmas: async () => undefined };
		expect(await mod.loadScene("diwali", { loaders })).toBeNull();
		expect(await mod.loadScene("christmas", { loaders })).toBeNull();
		expect(reported).toHaveBeenCalledTimes(2);
	});

	it("a design with no scene gives null without loading or reporting", async () => {
		const loader = vi.fn(async () => scene);
		expect(await mod.loadScene("ramadan", { loaders: { diwali: loader } })).toBeNull();
		expect(await mod.loadScene("toString", { loaders: { diwali: loader } })).toBeNull();
		expect(loader).not.toHaveBeenCalled();
		expect(reported).not.toHaveBeenCalled();
	});

	it("a second ask while the import is in flight shares it", async () => {
		let finish;
		const loader = vi.fn(() => new Promise((resolve) => (finish = resolve)));
		const loaders = { diwali: loader };
		const first = mod.loadScene("diwali", { loaders });
		const second = mod.loadScene("diwali", { loaders });
		await Promise.resolve();
		finish(scene);
		expect(await first).toBe(scene);
		expect(await second).toBe(scene);
		expect(loader).toHaveBeenCalledTimes(1);
	});
});

describe("phone scene loader, offline", () => {
	const online = vi.spyOn(window.navigator, "onLine", "get");
	beforeEach(() => {
		phone._resetForTests();
		reported.mockClear();
		online.mockReturnValue(true);
	});

	const failing = { diwali: async () => Promise.reject(loadFailure()) };

	it("an import that fails with no network is expected: null, and nothing is reported", async () => {
		online.mockReturnValue(false);
		expect(await phone.loadScene("diwali", { loaders: failing })).toBeNull();
		expect(reported).not.toHaveBeenCalled();
	});

	it("is still not tried again for a minute, and a later failure online is reported", async () => {
		let clock = 1000;
		const now = () => clock;
		online.mockReturnValue(false);
		await phone.loadScene("diwali", { loaders: failing, now });
		online.mockReturnValue(true);
		clock += phone.SCENE_RETRY_MS;
		expect(await phone.loadScene("diwali", { loaders: failing, now })).toBeNull();
		expect(reported).toHaveBeenCalledTimes(1);
	});
});
