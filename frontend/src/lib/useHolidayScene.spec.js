import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("@/api", () => ({ recordHolidayShown: vi.fn() }));

import { createHolidayScene, _resetSpentForTests } from "./useHolidayScene";
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
const NOW = new Date("2026-11-08T04:00:00Z"); // 09:30 in Kolkata

const memory = () => {
	const data = new Map();
	return {
		getItem: (k) => (data.has(k) ? data.get(k) : null),
		setItem: (k, v) => void data.set(k, String(v)),
	};
};

// A scene that does what the contract says and nothing else.
function fakeScene() {
	const made = [];
	const mod = {
		play: vi.fn((host, opts) => {
			let finish;
			const done = new Promise((resolve) => (finish = resolve));
			const scene = {
				opts,
				finish,
				settle: vi.fn((mode) => {
					opts.onBeat("settle", mode);
					finish();
				}),
				done,
			};
			made.push(scene);
			return scene;
		}),
	};
	return { mod, made };
}

function setup({ answer: given, ...overrides } = {}) {
	const { mod, made } = fakeScene();
	const answer = given || { ...DAY };
	const options = {
		answer: () => answer,
		host: () => document.createElement("div"),
		getBoxes: () => ({}),
		recordShown: vi.fn(async () => ({ play: true })),
		load: vi.fn(async () => mod),
		now: () => NOW,
		storage: memory(),
		report: vi.fn(),
		...overrides,
	};
	const scene = createHolidayScene(options);
	return { scene, options, mod, made, answer };
}

const tick = (ms = 0) => vi.advanceTimersByTimeAsync(ms);

// The overall deadline is a promise to the user: written out here, not imported.
const DEADLINE_MS = 2500;

describe("createHolidayScene", () => {
	beforeEach(() => {
		vi.useFakeTimers();
		_resetSpentForTests();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	describe("when a scene may play", () => {
		it("hides the cards from first paint only while it may, with the normal greeting", () => {
			const { scene } = setup();
			expect(scene.phase.value).toBe("pending");
			expect(scene.cardsHidden.value).toBe(true);
			expect(scene.text.swapped).toBe(false);
			expect(scene.mark).toMatchObject({ face: false, item: false });
		});

		it.each([
			["it is not a scene day", { scene: false }],
			["the server says no slot is left", { may_play: false }],
			["the design has no scene yet", { design: "pongal", occasion: "pongal" }],
			["it is a lead-up day", { kind: "leadup" }],
			["there is no answer", { kind: "none" }],
		])("shows the rest state at once when %s, and records nothing", async (_, patch) => {
			const { scene, options } = setup({ answer: { ...DAY, ...patch } });
			scene.begin();
			await tick(10);
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
			expect(options.recordShown).not.toHaveBeenCalled();
			expect(options.load).not.toHaveBeenCalled();
			expect(scene.mark).toMatchObject({ face: true, item: true, happy: false });
			expect(scene.text.swapped).toBe(true);
		});

		it("shows the rest state at first paint under reduced motion and records nothing", async () => {
			const { scene, options } = setup({ reduced: () => true });
			scene.begin();
			await tick(10);
			expect(scene.phase.value).toBe("rest");
			expect(options.recordShown).not.toHaveBeenCalled();
			expect(options.storage.getItem(LOCAL_KEY)).toBeNull();
			expect(vi.getTimerCount()).toBe(0);
		});

		it("does not hide the cards when the composer already has text", () => {
			const { scene } = setup({ composerEmpty: () => false });
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.phase.value).toBe("rest");
		});

		it("does not play when this browser already showed it in the same slot", () => {
			const storage = memory();
			storage.setItem(
				LOCAL_KEY,
				JSON.stringify({ times: [new Date("2026-11-08T03:30:00Z").toISOString()] })
			);
			const { scene } = setup({ storage });
			expect(scene.phase.value).toBe("rest");
		});
	});

	describe("the deadline", () => {
		it("plays on {play: true} inside the deadline", async () => {
			const { scene, mod, options } = setup();
			scene.begin();
			await tick(50);
			expect(options.recordShown).toHaveBeenCalledTimes(1);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(mod.play.mock.calls[0][1]).toMatchObject({
				layout: "desktop",
				year: 2026,
				reduced: false,
			});
			expect(scene.phase.value).toBe("playing");
			expect(scene.cardsHidden.value).toBe(true);
			expect(vi.getTimerCount()).toBe(0); // the deadline is cleared once it plays
		});

		it("with a 5 s import the cards are visible by 2.5 s, nothing plays, and the late art is ignored", async () => {
			const { mod } = fakeScene();
			let arrive;
			const slow = new Promise((resolve) => (arrive = resolve));
			const { scene, options } = setup({ load: vi.fn(() => slow.then(() => mod)) });
			scene.begin();
			await tick(DEADLINE_MS - 1);
			expect(options.recordShown).not.toHaveBeenCalled(); // no slot before there is art
			expect(scene.cardsHidden.value).toBe(true);
			await tick(1);
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.fadeMs.value).toBe(0);
			expect(scene.text.swapped).toBe(true);
			await tick(5000);
			arrive();
			await tick(10);
			expect(mod.play).not.toHaveBeenCalled();
			expect(scene.phase.value).toBe("rest");
			expect(options.recordShown).not.toHaveBeenCalled(); // the late art asks for nothing
			expect(options.storage.getItem(LOCAL_KEY)).toBeNull(); // no local slot either
		});

		it("a record_shown that never answers also ends at 2.5 s with the cards shown", async () => {
			const { scene, mod } = setup({ recordShown: vi.fn(() => new Promise(() => {})) });
			scene.begin();
			await tick(DEADLINE_MS);
			expect(scene.phase.value).toBe("rest");
			expect(mod.play).not.toHaveBeenCalled();
		});

		it("{play: false} settles to the rest state at once", async () => {
			const { scene, mod } = setup({ recordShown: vi.fn(async () => ({ play: false })) });
			scene.begin();
			await tick(10);
			expect(scene.phase.value).toBe("rest");
			expect(mod.play).not.toHaveBeenCalled();
			expect(vi.getTimerCount()).toBe(0);
		});

		it("a failed import shows the cards at once, without waiting for the deadline", async () => {
			const { scene, mod, options } = setup({ load: vi.fn(async () => null) });
			scene.begin();
			await tick(10);
			expect(scene.phase.value).toBe("rest");
			expect(mod.play).not.toHaveBeenCalled();
			expect(options.recordShown).not.toHaveBeenCalled(); // a failed import uses no slot
			expect(options.storage.getItem(LOCAL_KEY)).toBeNull();
		});

		it("plays on a fast network error and remembers the showing in this browser", async () => {
			const { scene, mod, options } = setup({
				recordShown: vi.fn(async () => {
					throw new Error("offline");
				}),
			});
			scene.begin();
			await tick(10);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(JSON.parse(options.storage.getItem(LOCAL_KEY)).times).toHaveLength(1);
		});

		it("does not play on a network error when the local rule says no", async () => {
			const storage = memory();
			const { scene, mod } = setup({
				storage,
				recordShown: vi.fn(async () => {
					// The slot got used by another tab while the call was in flight.
					storage.setItem(LOCAL_KEY, JSON.stringify({ times: [NOW.toISOString()] }));
					throw new Error("offline");
				}),
			});
			scene.begin();
			await tick(10);
			expect(mod.play).not.toHaveBeenCalled();
			expect(scene.phase.value).toBe("rest");
		});

		it("an error that arrives after the deadline does not play", async () => {
			let fail;
			const { scene, mod } = setup({
				recordShown: vi.fn(() => new Promise((_, reject) => (fail = reject))),
			});
			scene.begin();
			await tick(DEADLINE_MS);
			fail(new Error("late"));
			await tick(10);
			expect(mod.play).not.toHaveBeenCalled();
		});

		it("a scene that throws while starting is reported once and leaves the rest state", async () => {
			const bad = {
				play: vi.fn(() => {
					throw new Error("boom");
				}),
			};
			const { scene, options } = setup({ load: vi.fn(async () => bad) });
			scene.begin();
			await tick(10);
			expect(scene.phase.value).toBe("rest");
			expect(options.report).toHaveBeenCalledTimes(1);
		});
	});

	describe("after a reply", () => {
		it("a second new chat on the same answer does not hide the cards again", async () => {
			const answer = { ...DAY };
			const first = setup({ answer, recordShown: vi.fn(async () => ({ play: false })) });
			first.scene.begin();
			await tick(10);
			const second = setup({ answer });
			expect(second.scene.phase.value).toBe("rest");
			expect(second.scene.cardsHidden.value).toBe(false);
		});

		it("a fresh answer from get_today gets a new chance", async () => {
			const first = setup({ recordShown: vi.fn(async () => ({ play: false })) });
			first.scene.begin();
			await tick(10);
			expect(setup({ answer: { ...DAY } }).scene.phase.value).toBe("pending");
		});

		it("a played scene uses the answer up too", async () => {
			const answer = { ...DAY };
			const first = setup({ answer });
			first.scene.begin();
			await tick(10);
			first.scene.settle("early");
			expect(setup({ answer }).scene.phase.value).toBe("rest");
		});
	});

	describe("ending a scene", () => {
		async function playing(extra) {
			const ctx = setup(extra);
			ctx.scene.begin();
			await tick(10);
			return ctx;
		}

		it("an early settle reveals the cards over 400 ms and hands the ending to the scene", async () => {
			const { scene, made } = await playing();
			scene.settle("early");
			expect(made[0].settle).toHaveBeenCalledWith("early");
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.fadeMs.value).toBe(400);
		});

		it("the scene's own ending fades the cards in over 600 ms", async () => {
			const { scene, made } = await playing();
			made[0].opts.onBeat("settle", "end");
			expect(scene.fadeMs.value).toBe(600);
			expect(scene.cardsHidden.value).toBe(false);
		});

		it("the hero is centred while pending and playing, and rises over 600 / 250 / 0 ms by how it ends", async () => {
			const end = await playing();
			expect(end.scene.centred.value).toBe(true);
			end.made[0].opts.onBeat("settle", "end");
			expect(end.scene.centred.value).toBe(false);
			expect(end.scene.glideMs.value).toBe(600);
			const early = await playing();
			early.scene.settle("early");
			expect(early.scene.glideMs.value).toBe(250);
			const instant = await playing();
			instant.scene.settle("instant");
			expect(instant.scene.glideMs.value).toBe(0);
			expect(instant.scene.centred.value).toBe(false);
		});

		it("a hero centred while pending rises in 250 ms when the scene is given up", async () => {
			const { scene } = setup({ load: vi.fn(() => new Promise(() => {})) });
			scene.begin();
			expect(scene.centred.value).toBe(true);
			await tick(DEADLINE_MS);
			expect(scene.centred.value).toBe(false);
			expect(scene.glideMs.value).toBe(250);
		});

		it("a scene that was never going to play is at rest from the start: no centring", () => {
			const { scene } = setup({ reduced: () => true });
			expect(scene.centred.value).toBe(false);
			expect(scene.glideMs.value).toBe(0);
		});

		it("the beats drive the mark and the greeting", async () => {
			const { scene, made } = await playing();
			const beat = made[0].opts.onBeat;
			beat("peek");
			beat("look", -9, 3);
			expect(scene.mark).toMatchObject({ face: true, item: false, look: [-9, 3] });
			beat("deliver");
			beat("happy");
			expect(scene.mark).toMatchObject({ item: true, happy: true, look: null });
			beat("swap", { instant: false });
			expect(scene.text).toMatchObject({ swapped: true, instant: false });
		});

		it("never restarts: begin again after a settle plays nothing and never hides the cards", async () => {
			const { scene, mod, options } = await playing();
			scene.settle("early");
			scene.begin();
			scene.settle("early");
			await tick(DEADLINE_MS * 2);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(options.recordShown).toHaveBeenCalledTimes(1); // no second slot asked for
			expect(vi.getTimerCount()).toBe(0); // no second deadline
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
		});

		it("a settle while still pending cancels it for good", async () => {
			const { mod } = fakeScene();
			let arrive;
			const slow = new Promise((resolve) => (arrive = resolve));
			const { scene, options } = setup({ load: vi.fn(() => slow.then(() => mod)) });
			scene.begin();
			scene.settle("early");
			expect(scene.phase.value).toBe("rest");
			arrive();
			await tick(10);
			expect(mod.play).not.toHaveBeenCalled();
			expect(options.recordShown).not.toHaveBeenCalled(); // an early settle used no slot
			expect(vi.getTimerCount()).toBe(0);
		});

		it("stop ends a playing scene instantly and leaves no timer", async () => {
			const { scene, made } = await playing();
			scene.stop();
			expect(made[0].settle).toHaveBeenCalledWith("instant");
			expect(vi.getTimerCount()).toBe(0);
		});

		it("stop while pending leaves no timer and no unhandled rejection", async () => {
			const unhandled = vi.fn();
			process.on("unhandledRejection", unhandled);
			const { scene } = setup({ recordShown: vi.fn(() => new Promise(() => {})) });
			scene.begin();
			scene.stop();
			await tick(DEADLINE_MS * 2);
			process.off("unhandledRejection", unhandled);
			expect(unhandled).not.toHaveBeenCalled();
			expect(vi.getTimerCount()).toBe(0);
		});
	});

	describe("with the real Diwali scene", () => {
		const BOXES = {
			host: { w: 1170, h: 640 },
			mark: { x: 540, y: 80, w: 90, h: 90 },
			heading: { x: 400, y: 214, w: 370, h: 44 },
			cards: { x: 245, y: 400, w: 680, h: 230 },
		};
		beforeEach(() => {
			Element.prototype.animate = vi.fn(() => ({ cancel: vi.fn() }));
		});

		it("stop() after an early settle leaves no timer, not even the smile's tail", async () => {
			const real = await import("@jsshared/holiday/scenes/diwali.mjs");
			const host = document.createElement("div");
			document.body.appendChild(host);
			const { scene } = setup({
				host: () => host,
				getBoxes: () => BOXES,
				load: vi.fn(async () => real),
			});
			scene.begin();
			await tick(10);
			expect(scene.phase.value).toBe("playing");
			scene.settle("early");
			await tick(400); // the fade is over and the scene is done; the smile's tail is not
			expect(scene.phase.value).toBe("rest");
			scene.stop();
			expect(vi.getTimerCount()).toBe(0);
			host.remove();
		});
	});

	describe("replay on the mark", () => {
		async function atRest() {
			const ctx = setup();
			ctx.scene.begin();
			await tick(10);
			ctx.scene.settle("early");
			return ctx;
		}

		it("plays again with the cards still showing, and does not record", async () => {
			const { scene, mod, options } = await atRest();
			await scene.replay();
			expect(mod.play).toHaveBeenCalledTimes(2);
			expect(mod.play.mock.calls[1][1].replay).toBe(true);
			expect(scene.phase.value).toBe("replaying");
			expect(scene.cardsHidden.value).toBe(false);
			expect(options.recordShown).toHaveBeenCalledTimes(1);
		});

		it("a click on the mark mid-scene only settles it", async () => {
			const { scene, mod } = setup();
			scene.begin();
			await tick(10);
			scene.onPointerDown(); // the pointerdown on the mark
			await scene.replay(); // and the click that follows it
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(scene.phase.value).toBe("rest");
		});

		it("a click at rest after a swallowed one replays", async () => {
			const { scene, mod } = setup();
			scene.begin();
			await tick(10);
			scene.onPointerDown();
			await scene.replay();
			scene.onPointerDown(); // a new gesture at rest
			await scene.replay();
			expect(mod.play).toHaveBeenCalledTimes(2);
		});

		it("under reduced motion a click only smiles, then the smile ends", async () => {
			let reduced = false;
			const ctx = setup({ reduced: () => reduced });
			ctx.scene.begin();
			await tick(10);
			ctx.scene.settle("early");
			reduced = true;
			await ctx.scene.replay();
			expect(ctx.mod.play).toHaveBeenCalledTimes(1);
			expect(ctx.scene.mark.happy).toBe(true);
			await tick(1600);
			expect(ctx.scene.mark.happy).toBe(false);
			expect(vi.getTimerCount()).toBe(0);
		});
	});
});
