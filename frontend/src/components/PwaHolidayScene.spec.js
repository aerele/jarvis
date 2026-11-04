// The PWA copy of the scene composable (pwa/src/lib/useHolidayScene.js) under the desktop
// vitest config. Its relative imports mean only the network and the art are controlled here.
// The desktop original is covered by src/lib/useHolidayScene.spec.js; this file holds what
// the phone does differently: the phone layout, the room check before any slot is used,
// no glide, and the tap that ends a scene not counting as a click on what is under it.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../../../pwa/src/api", () => ({ recordHolidayShown: vi.fn() }));

import { createHolidayScene, _resetSpentForTests } from "../../../pwa/src/lib/useHolidayScene";
import { LOCAL_KEY } from "@jsshared/holiday/cadence.mjs";

const DAY = {
	v: 1,
	kind: "day",
	design: "diwali",
	occasion: "diwali",
	date: "2026-11-08",
	tz: "Asia/Kolkata",
	year: 2026,
	worn: true,
	scene: true,
	may_play: true,
};
const NOW = new Date("2026-11-08T04:00:00Z");
const DEADLINE_MS = 2500; // a promise to the user: written out, not imported

const memory = () => {
	const data = new Map();
	return {
		getItem: (k) => (data.has(k) ? data.get(k) : null),
		setItem: (k, v) => void data.set(k, String(v)),
	};
};

function fakeScene() {
	const made = [];
	const mod = {
		play: vi.fn((host, opts) => {
			let finish;
			const done = new Promise((resolve) => (finish = resolve));
			const scene = {
				opts,
				settle: vi.fn((mode) => {
					opts.onBeat("settle", mode);
					finish();
				}),
				relayout: vi.fn(),
				done,
			};
			made.push(scene);
			return scene;
		}),
	};
	return { mod, made };
}

function setup(overrides = {}) {
	const { mod, made } = fakeScene();
	const options = {
		answer: () => ({ ...DAY }),
		host: () => document.createElement("div"),
		getBoxes: () => ({}),
		recordShown: vi.fn(async () => ({ play: true })),
		load: vi.fn(async () => mod),
		now: () => NOW,
		storage: memory(),
		report: vi.fn(),
		...overrides,
	};
	return { scene: createHolidayScene(options), options, mod, made };
}
const tick = (ms = 0) => vi.advanceTimersByTimeAsync(ms);
let empty = true; // the composer-empty / room switch of the kept-tap tests

describe("PWA createHolidayScene", () => {
	beforeEach(() => {
		vi.useFakeTimers();
		_resetSpentForTests();
	});
	afterEach(() => vi.useRealTimers());

	it("hides the cards from first paint and plays on the phone layout", async () => {
		const { scene, mod, options } = setup();
		expect(scene.phase.value).toBe("pending");
		expect(scene.cardsHidden.value).toBe(true);
		scene.begin();
		await tick(50);
		expect(options.recordShown).toHaveBeenCalledTimes(1);
		expect(mod.play.mock.calls[0][1]).toMatchObject({
			layout: "phone",
			year: 2026,
			reduced: false,
		});
		expect(scene.phase.value).toBe("playing");
	});

	it("plays on a lead-up day the server marked as a scene day", async () => {
		const { scene, mod, options } = setup({
			answer: () => ({ ...DAY, kind: "leadup", date: "2026-11-06" }),
		});
		expect(scene.phase.value).toBe("pending");
		scene.begin();
		await tick(50);
		expect(options.recordShown).toHaveBeenCalledTimes(1);
		expect(mod.play).toHaveBeenCalledTimes(1);
		expect(scene.phase.value).toBe("playing");
	});

	it("rests with no art load and no record_shown when the screen has no room", async () => {
		const { scene, options } = setup({ fits: () => false });
		scene.begin();
		await tick(10);
		expect(scene.phase.value).toBe("rest");
		expect(scene.reason.value).toBe("no-room");
		expect(scene.cardsHidden.value).toBe(false);
		expect(options.load).not.toHaveBeenCalled();
		expect(options.recordShown).not.toHaveBeenCalled();
		expect(options.storage.getItem(LOCAL_KEY)).toBeNull();
		expect(scene.mark).toMatchObject({ face: true, item: true });
		expect(scene.text.swapped).toBe(true);
	});

	it("rests under reduced motion with no record_shown", async () => {
		const { scene, options } = setup({ reduced: () => true });
		expect(scene.phase.value).toBe("rest");
		scene.begin();
		await tick(10);
		expect(options.recordShown).not.toHaveBeenCalled();
	});

	it("rests at once when the composer already has a draft", () => {
		const { scene } = setup({ composerEmpty: () => false });
		expect(scene.phase.value).toBe("rest");
		expect(scene.cardsHidden.value).toBe(false);
	});

	it("gives art import plus record_shown one 2.5 s deadline from begin", async () => {
		const { mod } = fakeScene();
		const slow = new Promise(() => {});
		const { scene, options } = setup({ load: vi.fn(() => slow.then(() => mod)) });
		scene.begin();
		await tick(DEADLINE_MS - 1);
		expect(scene.cardsHidden.value).toBe(true);
		await tick(1);
		expect(scene.phase.value).toBe("rest");
		expect(scene.reason.value).toBe("deadline");
		expect(options.recordShown).not.toHaveBeenCalled();
	});

	it("{play: false} rests and plays nothing", async () => {
		const { scene, mod } = setup({ recordShown: vi.fn(async () => ({ play: false })) });
		scene.begin();
		await tick(10);
		expect(scene.phase.value).toBe("rest");
		expect(mod.play).not.toHaveBeenCalled();
		expect(vi.getTimerCount()).toBe(0);
	});

	it("waits for a hidden tab before it starts", async () => {
		const doc = Object.assign(new EventTarget(), { visibilityState: "hidden" });
		const { scene, options } = setup({ doc });
		scene.begin();
		await tick(3000);
		expect(options.load).not.toHaveBeenCalled();
		expect(scene.reason.value).toBe("hidden");
		doc.visibilityState = "visible";
		doc.dispatchEvent(new Event("visibilitychange"));
		await tick(50);
		expect(options.recordShown).toHaveBeenCalledTimes(1);
	});

	it("has no hero glide: the cards come back and nothing else is timed", async () => {
		const { scene, made } = setup();
		scene.begin();
		await tick(50);
		expect(scene).not.toHaveProperty("glideMs");
		expect(scene).not.toHaveProperty("centred");
		made[0].opts.onBeat("settle", "end");
		await tick(0);
		expect(scene.phase.value).toBe("rest");
		expect(scene.fadeMs.value).toBe(600);
		expect(vi.getTimerCount()).toBe(0);
	});

	it("a pointerdown ends the scene early and its click is taken once", async () => {
		const { scene, made } = setup();
		scene.begin();
		await tick(50);
		scene.onPointerDown();
		expect(made[0].settle).toHaveBeenCalledWith("early");
		expect(scene.phase.value).toBe("rest");
		expect(scene.fadeMs.value).toBe(400);
		expect(scene.takeEndingTap()).toBe(true);
		expect(scene.takeEndingTap()).toBe(false);
	});

	it("a later tap on the mark at rest replays", async () => {
		const { scene, made, mod } = setup();
		scene.begin();
		await tick(50);
		scene.onPointerDown();
		scene.onPointerDown(); // a new gesture, now at rest
		await scene.replay();
		await tick(50);
		expect(mod.play).toHaveBeenCalledTimes(2);
		expect(mod.play.mock.calls[1][1]).toMatchObject({ replay: true, layout: "phone" });
		expect(made).toHaveLength(2);
	});

	it("the tap that ended the scene does not also replay it", async () => {
		const { scene, mod } = setup();
		scene.begin();
		await tick(50);
		scene.onPointerDown();
		await scene.replay();
		await tick(50);
		expect(mod.play).toHaveBeenCalledTimes(1);
	});

	it("a replay with no room smiles instead, and uses no slot", async () => {
		let room = true;
		const { scene, mod, options } = setup({ fits: () => room });
		scene.begin();
		await tick(50);
		scene.settle("instant");
		room = false;
		await scene.replay();
		expect(mod.play).toHaveBeenCalledTimes(1);
		expect(scene.mark.happy).toBe(true);
		expect(options.recordShown).toHaveBeenCalledTimes(1);
		await tick(1600);
		expect(scene.mark.happy).toBe(false);
	});

	it("reduced motion makes the replay a smile too", async () => {
		let reduced = false;
		const { scene, mod } = setup({ reduced: () => reduced });
		scene.begin();
		await tick(50);
		scene.settle("instant");
		reduced = true;
		await scene.replay();
		expect(mod.play).toHaveBeenCalledTimes(1);
		expect(scene.mark.happy).toBe(true);
	});

	it("a replay hides the starters like the first play and brings them back at rest", async () => {
		const { scene, made } = setup();
		scene.begin();
		await tick(50);
		scene.settle("instant");
		expect(scene.cardsHidden.value).toBe(false);
		await scene.replay();
		await tick(50);
		expect(scene.phase.value).toBe("replaying");
		expect(scene.cardsHidden.value).toBe(true);
		made[1].opts.onBeat("settle", "end");
		await tick(0);
		expect(scene.phase.value).toBe("rest");
		expect(scene.cardsHidden.value).toBe(false);
		expect(scene.fadeMs.value).toBe(600);
	});

	describe("a replay obeys the same rules as the first play", () => {
		it("a replay with a draft in the composer smiles and hides nothing", async () => {
			let empty = true;
			const { scene, mod, options } = setup({ composerEmpty: () => empty });
			scene.begin();
			await tick(50);
			scene.settle("instant");
			empty = false;
			await scene.replay();
			await tick(50);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(scene.mark.happy).toBe(true);
			expect(scene.cardsHidden.value).toBe(false);
			expect(options.recordShown).toHaveBeenCalledTimes(1);
		});

		it("a replay whose art will not load smiles and hides nothing", async () => {
			let broken = false;
			const { mod, made } = fakeScene();
			const { scene, options } = setup({ load: vi.fn(async () => (broken ? null : mod)) });
			scene.begin();
			await tick(50);
			scene.settle("instant");
			broken = true;
			await scene.replay();
			await tick(50);
			expect(made).toHaveLength(1);
			expect(scene.mark.happy).toBe(true);
			expect(scene.cardsHidden.value).toBe(false);
			expect(options.recordShown).toHaveBeenCalledTimes(1);
		});

		it("a replay tapped in a hidden tab smiles", async () => {
			const doc = {
				visibilityState: "visible",
				addEventListener() {},
				removeEventListener() {},
			};
			const { scene, mod } = setup({ doc });
			scene.begin();
			await tick(50);
			scene.settle("instant");
			doc.visibilityState = "hidden";
			await scene.replay();
			await tick(50);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(scene.mark.happy).toBe(true);
		});

		it("a replay with no place to draw returns to rest with the item and the cards back", async () => {
			let el = document.createElement("div");
			const { scene, mod } = setup({ host: () => el });
			scene.begin();
			await tick(50);
			scene.settle("instant");
			el = null;
			await scene.replay();
			await tick(50);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(scene.phase.value).toBe("rest");
			expect(scene.mark.item).toBe(true);
			expect(scene.cardsHidden.value).toBe(false);
		});

		it("a scene that throws when a replay starts brings the starters back softly", async () => {
			let broken = false;
			const { mod } = fakeScene();
			const play = mod.play.getMockImplementation();
			mod.play.mockImplementation((host, opts) => {
				if (broken) throw new Error("boom");
				return play(host, opts);
			});
			const { scene, options } = setup({ load: vi.fn(async () => mod) });
			scene.begin();
			await tick(50);
			scene.settle("instant");
			broken = true;
			await scene.replay();
			await tick(50);
			expect(scene.phase.value).toBe("rest");
			expect(scene.reason.value).toBe("threw");
			expect(scene.fadeMs.value).toBe(400);
			expect(scene.mark.item).toBe(true);
			expect(scene.cardsHidden.value).toBe(false);
			expect(options.report).toHaveBeenCalledTimes(1);
		});

		it("a replay that ends without a settle beat fades the starters back at 400", async () => {
			const finishes = [];
			const mod = {
				play: vi.fn((host, opts) => {
					let finish;
					const done = new Promise((resolve) => (finish = resolve));
					finishes.push(finish);
					return { opts, done, settle: vi.fn(), relayout: vi.fn() }; // never announces a settle
				}),
			};
			const { scene } = setup({ load: vi.fn(async () => mod) });
			scene.begin();
			await tick(50);
			scene.settle("instant");
			finishes[0]();
			await tick(50);
			await scene.replay();
			await tick(50);
			expect(scene.phase.value).toBe("replaying");
			finishes[1]();
			await tick(50);
			expect(scene.phase.value).toBe("rest");
			expect(scene.fadeMs.value).toBe(400);
			expect(scene.cardsHidden.value).toBe(false);
		});
	});

	describe("a replay tapped while the last scene fades out (one tap is kept)", () => {
		// A scene whose done only resolves when the test says so (the fade-out still running).
		function slowScenes() {
			const finishes = [];
			const mod = {
				play: vi.fn((host, opts) => {
					let finish;
					const done = new Promise((resolve) => (finish = resolve));
					finishes.push(finish);
					return {
						opts,
						done,
						settle: vi.fn((mode) => opts.onBeat("settle", mode)),
						relayout: vi.fn(),
					};
				}),
			};
			return { mod, finishes };
		}
		async function fadingWithOneTapKept(over = {}) {
			const { mod, finishes } = slowScenes();
			const made = setup({ load: vi.fn(async () => mod), ...over });
			made.scene.begin();
			await tick(50);
			made.scene.settle("early");
			await made.scene.replay(); // still fading: the tap is kept
			return { ...made, mod, finishes };
		}

		it.each([
			["a key", (scene) => scene.onKeyDown(false)],
			["a pointerdown", (scene) => scene.onPointerDown()],
			["a settle (typing, a hold, a route change)", (scene) => scene.settle("early")],
		])("%s before the fade-out is over forgets the kept tap", async (_name, gesture) => {
			const { scene, mod, finishes } = await fadingWithOneTapKept();
			gesture(scene);
			finishes[0]();
			await tick(50);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(scene.cardsHidden.value).toBe(false);
		});

		it.each([
			["a draft", { composerEmpty: () => empty }],
			["reduced motion", { reduced: () => !empty }],
			["no room", { fits: () => empty }],
		])("%s by the time the fade-out ends smiles instead of replaying", async (_name, over) => {
			// `empty` is true while the first play runs and flips before the drain.
			empty = true;
			const { scene, mod, finishes } = await fadingWithOneTapKept(over);
			empty = false;
			finishes[0]();
			await tick(50);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(scene.mark.happy).toBe(true);
			expect(scene.cardsHidden.value).toBe(false);
		});
	});

	it("a screen with no room does not use up the day: the next new chat may still play", async () => {
		const answer = { ...DAY };
		const first = setup({ answer: () => answer, fits: () => false });
		first.scene.begin();
		await tick(10);
		expect(first.scene.reason.value).toBe("no-room");
		const second = setup({ answer: () => answer });
		expect(second.scene.phase.value).toBe("pending");
	});

	it("asks again whether there is room once the art is loaded, before a showing is used", async () => {
		let asked = 0;
		const { scene, options } = setup({ fits: () => ++asked < 2 });
		scene.begin();
		await tick(50);
		expect(asked).toBe(2);
		expect(scene.reason.value).toBe("no-room");
		expect(scene.phase.value).toBe("rest");
		expect(options.recordShown).not.toHaveBeenCalled();
	});

	it("Enter or Space on the mark ends the scene without the click it starts replaying it", async () => {
		const { scene, mod } = setup();
		scene.begin();
		await tick(50);
		scene.onKeyDown(true);
		expect(scene.phase.value).toBe("rest");
		await scene.replay(); // the click that Enter starts
		await tick(50);
		expect(mod.play).toHaveBeenCalledTimes(1);
		await scene.replay(); // a later activation at rest does replay
		await tick(50);
		expect(mod.play).toHaveBeenCalledTimes(2);
	});

	it("a repeating key is ignored: it does not end a replay, and does not undo the swallow", async () => {
		const { scene, mod } = setup();
		scene.begin();
		await tick(10);
		scene.settle("early");
		await tick(300); // the scene is over and the hero has risen
		await scene.replay();
		await tick(700);
		expect(scene.phase.value).toBe("replaying");
		scene.onKeyDown(true, true);
		scene.onKeyDown(false, true);
		expect(scene.phase.value).toBe("replaying");
		scene.onKeyDown(true); // the key goes down: ends it, and swallows its click
		expect(scene.phase.value).toBe("rest");
		scene.onKeyDown(true, true); // the key is held: a repeat at rest
		await scene.replay(); // the click the key fires
		await tick(1000);
		expect(mod.play).toHaveBeenCalledTimes(2);
	});

	it("any other key ends the scene and leaves the next tap on the mark alone", async () => {
		const { scene, mod } = setup();
		scene.begin();
		await tick(50);
		scene.onKeyDown(false);
		expect(scene.phase.value).toBe("rest");
		await scene.replay();
		await tick(50);
		expect(mod.play).toHaveBeenCalledTimes(2);
	});

	it("plays on this device's own cadence when record_shown fails", async () => {
		const { scene, mod, options } = setup({
			recordShown: vi.fn(async () => {
				throw new Error("offline");
			}),
		});
		scene.begin();
		await tick(50);
		expect(mod.play).toHaveBeenCalledTimes(1);
		expect(options.storage.getItem(LOCAL_KEY)).not.toBeNull();
	});

	it("does not play on a second failure the same day (local cadence)", async () => {
		const failing = () => ({
			recordShown: vi.fn(async () => {
				throw new Error("offline");
			}),
			storage,
		});
		const storage = memory();
		for (let i = 0; i < 2; i += 1) {
			_resetSpentForTests();
			setup(failing()).scene.begin();
			await tick(50);
		}
		const third = setup(failing());
		_resetSpentForTests();
		third.scene.begin();
		await tick(50);
		expect(third.mod.play).not.toHaveBeenCalled();
		expect(third.scene.reason.value).toBe("declined");
	});

	it("rests with the reason load-failed when the art does not load", async () => {
		const { scene, options } = setup({ load: vi.fn(async () => null) });
		scene.begin();
		await tick(50);
		expect(scene.phase.value).toBe("rest");
		expect(scene.reason.value).toBe("load-failed");
		expect(options.recordShown).not.toHaveBeenCalled();
		expect(scene.cardsHidden.value).toBe(false);
	});

	it("a scene that throws while starting rests, says why, and reports it", async () => {
		const mod = {
			play: vi.fn(() => {
				throw new Error("boom");
			}),
		};
		const { scene, options } = setup({ load: vi.fn(async () => mod) });
		scene.begin();
		await tick(50);
		expect(scene.phase.value).toBe("rest");
		expect(scene.reason.value).toBe("threw");
		expect(options.report).toHaveBeenCalledTimes(1);
		expect(options.report.mock.calls[0][0]).toMatchObject({
			error_class: "HolidaySceneFailed",
			error_code: "holiday_scene:diwali",
		});
		expect(scene.cardsHidden.value).toBe(false);
	});

	it("a replay tapped while the last scene is still fading out starts when it is gone", async () => {
		const finishes = [];
		const mod = {
			play: vi.fn((host, opts) => {
				let finish;
				const done = new Promise((resolve) => (finish = resolve));
				finishes.push(finish);
				return {
					opts,
					done,
					settle: vi.fn((mode) => opts.onBeat("settle", mode)), // done comes later
					relayout: vi.fn(),
				};
			}),
		};
		const { scene } = setup({ load: vi.fn(async () => mod) });
		scene.begin();
		await tick(50);
		scene.settle("early");
		expect(scene.phase.value).toBe("rest");
		await scene.replay(); // still fading: the tap is kept
		await tick(50);
		expect(mod.play).toHaveBeenCalledTimes(1);
		finishes[0]();
		await tick(50);
		expect(mod.play).toHaveBeenCalledTimes(2);
		expect(mod.play.mock.calls[1][1]).toMatchObject({ replay: true });
		expect(scene.phase.value).toBe("replaying");
	});

	it("stop() ends a scene that is playing and leaves no timer", async () => {
		const { scene, made } = setup();
		scene.begin();
		await tick(50);
		scene.stop();
		expect(made[0].settle).toHaveBeenCalledWith("instant");
		expect(vi.getTimerCount()).toBe(0);
	});
});
