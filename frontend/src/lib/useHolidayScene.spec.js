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
			["the design has no scene yet", { design: "ramadan", occasion: "ramadan" }],
			["it is a lead-up day without a scene", { kind: "leadup", scene: false }],
			["there is no answer", { kind: "none", scene: undefined }],
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

		it("plays on a lead-up day the server marked as a scene day", async () => {
			const { scene, mod, options } = setup({
				answer: { ...DAY, kind: "leadup", date: "2026-11-06" },
			});
			expect(scene.phase.value).toBe("pending");
			scene.begin();
			await tick(50);
			expect(options.recordShown).toHaveBeenCalledTimes(1);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(scene.phase.value).toBe("playing");
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
			await tick(300); // the hero's rise (250 ms) is the only timer left, and it ends
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
			expect(scene.reason.value).toBe("threw");
			// The centred hero still rises and the cards still fade in, as after a scene's early end.
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.fadeMs.value).toBe(400);
			expect(scene.glideMs.value).toBe(250);
		});

		it("a failure report names the design and keeps the error's stack", async () => {
			const boom = new Error("boom");
			const bad = {
				play: vi.fn(() => {
					throw boom;
				}),
			};
			const { scene, options } = setup({
				answer: { ...DAY, design: "christmas", occasion: "christmas" },
				load: vi.fn(async () => bad),
			});
			scene.begin();
			await tick(10);
			expect(options.report.mock.calls[0][0]).toMatchObject({
				error_class: "HolidaySceneFailed",
				error_code: "holiday_scene:christmas",
				stack: boom.stack,
			});
		});

		it("a timer inside the scene that throws is reported once with the design, and the scene plays on", async () => {
			const { scene, made, options } = setup();
			scene.begin();
			await tick(10);
			made[0].opts.onError(new Error("timer one"));
			made[0].opts.onError(new Error("timer two"));
			expect(options.report).toHaveBeenCalledTimes(1);
			expect(options.report.mock.calls[0][0]).toMatchObject({
				error_code: "holiday_scene:diwali",
			});
			expect(scene.phase.value).toBe("playing");
			expect(scene.reason.value).toBe("threw");
		});

		it("fonts that never load for good (a rejected promise) still let the scene play", async () => {
			const { scene, mod } = setup({ fontsReady: () => Promise.reject(new Error("fonts")) });
			scene.begin();
			await tick(10);
			expect(mod.play).toHaveBeenCalledTimes(1);
		});

		it("a fontsReady that throws at once still lets the scene play", async () => {
			const { scene, mod } = setup({
				fontsReady: () => {
					throw new Error("no fonts api");
				},
			});
			scene.begin();
			await tick(10);
			expect(mod.play).toHaveBeenCalledTimes(1);
		});
	});

	describe("why it is or is not playing (reason)", () => {
		it("is loaded when it plays", async () => {
			const { scene } = setup();
			scene.begin();
			await tick(10);
			expect(scene.reason.value).toBe("loaded");
		});

		it("is load-failed when the art does not load", async () => {
			const { scene } = setup({ load: vi.fn(async () => null) });
			scene.begin();
			await tick(10);
			expect(scene.reason.value).toBe("load-failed");
		});

		it("is deadline when the art is too slow", async () => {
			const { scene } = setup({ load: vi.fn(() => new Promise(() => {})) });
			scene.begin();
			await tick(DEADLINE_MS);
			expect(scene.reason.value).toBe("deadline");
		});

		it("is declined when the server says no, and when it was never going to play", async () => {
			const no = setup({ recordShown: vi.fn(async () => ({ play: false })) });
			no.scene.begin();
			await tick(10);
			expect(no.scene.reason.value).toBe("declined");
			expect(setup({ reduced: () => true }).scene.reason.value).toBe("declined");
		});
	});

	describe("a tab opened hidden", () => {
		function fakeDoc(visibilityState) {
			const doc = new EventTarget();
			doc.visibilityState = visibilityState;
			doc.show = () => {
				doc.visibilityState = "visible";
				doc.dispatchEvent(new Event("visibilitychange"));
			};
			vi.spyOn(doc, "addEventListener");
			vi.spyOn(doc, "removeEventListener");
			return doc;
		}

		it("does not begin, ask for a slot or start the deadline until it is shown", async () => {
			const doc = fakeDoc("hidden");
			const { scene, mod, options } = setup({ doc });
			scene.begin();
			scene.begin();
			await tick(DEADLINE_MS * 4);
			expect(scene.reason.value).toBe("hidden");
			expect(scene.phase.value).toBe("pending");
			expect(options.recordShown).not.toHaveBeenCalled();
			expect(doc.addEventListener).toHaveBeenCalledTimes(1);
			doc.show();
			await tick(10);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(scene.reason.value).toBe("loaded");
			expect(doc.removeEventListener).toHaveBeenCalledTimes(1);
		});

		it("the 2.5 s deadline runs from the moment it is shown", async () => {
			const doc = fakeDoc("hidden");
			const { scene } = setup({ doc, load: vi.fn(() => new Promise(() => {})) });
			scene.begin();
			await tick(60000);
			doc.show();
			await tick(DEADLINE_MS - 1);
			expect(scene.phase.value).toBe("pending");
			await tick(1);
			expect(scene.phase.value).toBe("rest");
		});

		it("stop removes the listener, and a settle while it waits ends it for good", async () => {
			const doc = fakeDoc("hidden");
			const first = setup({ doc });
			first.scene.begin();
			first.scene.stop();
			expect(doc.removeEventListener).toHaveBeenCalledTimes(1);
			const second = setup({ doc });
			second.scene.begin();
			second.scene.settle("early");
			expect(second.scene.phase.value).toBe("rest");
			doc.show();
			await tick(10);
			expect(second.mod.play).not.toHaveBeenCalled();
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
			await tick(300); // the hero's rise (250 ms) is the only timer left, and it ends
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
			ctx.made[0].opts.onBeat("deliver"); // a real scene lands the item by its end
			ctx.scene.settle("early");
			await tick(300); // the scene is over and the hero has risen (250 ms)
			return ctx;
		}

		it("a click at rest hides the cards and centres the hero at once, then plays the scene after the descent, unrecorded", async () => {
			const { scene, mod, options } = await atRest();
			await scene.replay();
			expect(scene.phase.value).toBe("centring");
			expect(scene.centred.value).toBe(true);
			expect(scene.cardsHidden.value).toBe(true);
			expect(scene.glideMs.value).toBe(600);
			expect(scene.mark.item).toBe(true); // the worn item rides down with the mark
			expect(mod.play).toHaveBeenCalledTimes(1); // no scene while the hero moves
			await tick(700);
			expect(mod.play).toHaveBeenCalledTimes(2);
			expect(mod.play.mock.calls[1][1].replay).toBe(true);
			expect(scene.phase.value).toBe("replaying");
			expect(scene.centred.value).toBe(true);
			expect(scene.cardsHidden.value).toBe(true);
			expect(scene.mark.item).toBe(false);
			expect(options.recordShown).toHaveBeenCalledTimes(1);
		});

		it("the replay's end is the first play's end: the cards fade in and the hero glides up", async () => {
			const end = await atRest();
			await end.scene.replay();
			await tick(700);
			end.made[1].opts.onBeat("settle", "end");
			expect(end.scene.phase.value).toBe("rest");
			expect(end.scene.cardsHidden.value).toBe(false);
			expect(end.scene.fadeMs.value).toBe(600);
			expect(end.scene.glideMs.value).toBe(600);
			end.made[1].finish(); // the art is gone: a normal end already went to rest
			await tick(0);
			expect(end.scene.fadeMs.value).toBe(600);
			expect(end.scene.glideMs.value).toBe(600);

			const early = await atRest();
			await early.scene.replay();
			await tick(700);
			early.made[1].opts.onBeat("settle", "early");
			expect(early.scene.fadeMs.value).toBe(400);
			expect(early.scene.glideMs.value).toBe(250);
		});

		it("a pointerdown during the descent ends it: cards back, hero up, no scene, its click swallowed", async () => {
			const { scene, mod } = await atRest();
			await scene.replay();
			scene.onPointerDown();
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.centred.value).toBe(false);
			expect(scene.fadeMs.value).toBe(400);
			expect(scene.glideMs.value).toBe(250);
			await scene.replay(); // the click the pointerdown starts
			await tick(1000);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(scene.phase.value).toBe("rest");
			expect(scene.reason.value).toBe("loaded");
		});

		it("a key during the replay brings the cards back with the early fade and glide", async () => {
			const { scene } = await atRest();
			await scene.replay();
			await tick(700);
			scene.onKeyDown();
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.fadeMs.value).toBe(400);
			expect(scene.glideMs.value).toBe(250);
		});

		describe.each(["centring", "replaying"])(
			"every abort during %s reaches rest",
			(during) => {
				it.each([
					["settle early", (s) => s.settle("early")],
					["settle instant", (s) => s.settle("instant")],
					["a pointerdown", (s) => s.onPointerDown()],
					["a key", (s) => s.onKeyDown()],
				])("%s", async (_, end) => {
					const { scene, mod } = await atRest();
					await scene.replay();
					if (during === "replaying") await tick(700);
					expect(scene.phase.value).toBe(during);
					end(scene);
					expect(scene.phase.value).toBe("rest");
					expect(scene.centred.value).toBe(false);
					expect(scene.cardsHidden.value).toBe(false);
					expect(scene.reason.value).toBe("loaded");
					await tick(1000);
					expect(scene.phase.value).toBe("rest");
					expect(mod.play).toHaveBeenCalledTimes(during === "replaying" ? 2 : 1);
				});
			}
		);

		it("a descent that was ended and clicked again starts its scene only at the second descent's end", async () => {
			const { scene, mod } = await atRest();
			await scene.replay(); // the first descent ends at 650 ms
			await tick(300);
			scene.settle("early"); // the rise takes 250 ms
			await tick(300);
			await scene.replay(); // 600 ms in: a second descent, ending at 1250 ms
			await tick(400); // 1000 ms in: the first descent's timer must be gone
			expect(scene.phase.value).toBe("centring");
			expect(mod.play).toHaveBeenCalledTimes(1);
			await tick(400);
			expect(mod.play).toHaveBeenCalledTimes(2);
			expect(scene.phase.value).toBe("replaying");
		});

		it("a replay with text in the composer only smiles, then the smile ends", async () => {
			let draft = false;
			const { scene, mod, options } = setup({ composerEmpty: () => !draft });
			scene.begin();
			await tick(10);
			scene.settle("early");
			await tick(300);
			draft = true;
			await scene.replay();
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.mark.happy).toBe(true);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(options.load).toHaveBeenCalledTimes(1);
			await tick(1600);
			expect(scene.mark.happy).toBe(false);
		});

		it("an art chunk that fails to load on a replay smiles, and nothing moves", async () => {
			const { mod, made } = fakeScene();
			let loads = 0;
			const { scene } = setup({ load: vi.fn(async () => (++loads === 1 ? mod : null)) });
			scene.begin();
			await tick(10);
			scene.settle("early");
			await tick(300);
			await scene.replay();
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.centred.value).toBe(false);
			expect(scene.mark.happy).toBe(true);
			expect(made).toHaveLength(1);
			await tick(1600);
			expect(vi.getTimerCount()).toBe(0);
		});

		it("a scene that throws on a replay start comes back to rest: cards fade in, hero rises", async () => {
			let plays = 0;
			const { mod, made } = fakeScene();
			const load = vi.fn(async () => ({
				play: (...args) => {
					if (++plays === 2) throw new Error("boom");
					return mod.play(...args);
				},
			}));
			const { scene, options } = setup({ load });
			scene.begin();
			await tick(10);
			made[0].opts.onBeat("settle", "end"); // fade and glide 600, so 400 and 250 are the throw's own
			made[0].finish();
			await tick(700);
			await scene.replay();
			await tick(700);
			expect(plays).toBe(2);
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.centred.value).toBe(false);
			expect(scene.fadeMs.value).toBe(400);
			expect(scene.glideMs.value).toBe(250);
			expect(scene.reason.value).toBe("threw");
			expect(scene.mark).toMatchObject({ face: true, item: true });
			expect(options.report).toHaveBeenCalledTimes(1);
		});

		it("a replay whose descent ends in a hidden tab rests instead of playing", async () => {
			const tab = {
				visibilityState: "visible",
				addEventListener: vi.fn(),
				removeEventListener: vi.fn(),
			};
			const { scene, mod, made } = setup({ doc: tab });
			scene.begin();
			await tick(10);
			made[0].opts.onBeat("deliver");
			scene.settle("early");
			await tick(300);
			await scene.replay();
			tab.visibilityState = "hidden";
			await tick(700);
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.glideMs.value).toBe(0);
			expect(scene.fadeMs.value).toBe(0);
			expect(mod.play).toHaveBeenCalledTimes(1);
			expect(scene.mark.item).toBe(true);
		});

		it("a replay with no host to draw into rests at once, with the item back", async () => {
			let el = document.createElement("div");
			const { scene, mod, made } = setup({ host: () => el });
			scene.begin();
			await tick(10);
			made[0].opts.onBeat("deliver");
			scene.settle("early");
			await tick(300);
			await scene.replay();
			el = null;
			await tick(700);
			expect(scene.phase.value).toBe("rest");
			expect(scene.cardsHidden.value).toBe(false);
			expect(scene.glideMs.value).toBe(0);
			expect(scene.mark.item).toBe(true);
			expect(mod.play).toHaveBeenCalledTimes(1);
		});

		it("lets the page finish moving once the descent is over, just before the scene starts", async () => {
			const beforePlay = vi.fn();
			const { scene, mod } = setup({ beforePlay });
			scene.begin();
			await tick(10);
			scene.settle("early");
			await tick(300);
			await scene.replay();
			await tick(100);
			expect(beforePlay).not.toHaveBeenCalled();
			await tick(600);
			expect(beforePlay).toHaveBeenCalledTimes(1);
			expect(beforePlay.mock.invocationCallOrder[0]).toBeLessThan(
				mod.play.mock.invocationCallOrder[1]
			);
		});

		it("stop() during the descent leaves no timer", async () => {
			const { scene, mod } = await atRest();
			await scene.replay();
			scene.stop();
			expect(vi.getTimerCount()).toBe(0);
			await tick(1000);
			expect(mod.play).toHaveBeenCalledTimes(1);
		});

		describe("Enter or Space on the replay button", () => {
			it.each(["centring", "replaying"])(
				"mid-%s ends it and the click the key fires does not replay again",
				async (during) => {
					const { scene, mod } = await atRest();
					await scene.replay();
					if (during === "replaying") await tick(700);
					scene.onKeyDown(true);
					expect(scene.phase.value).toBe("rest");
					await scene.replay(); // the click the key fires
					await tick(1000);
					expect(mod.play).toHaveBeenCalledTimes(during === "replaying" ? 2 : 1);
				}
			);

			it("a repeating key is ignored: it does not end a replay, and does not undo the swallow", async () => {
				const { scene, mod } = await atRest();
				await scene.replay();
				await tick(700);
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
			await tick(300); // the hero has risen
			scene.onPointerDown(); // a new gesture at rest
			await scene.replay();
			await tick(700); // the descent
			expect(mod.play).toHaveBeenCalledTimes(2);
		});

		describe("a click while the last scene is still going", () => {
			it("waits for the scene's done and the hero's rise, then plays once", async () => {
				const { scene, mod, made } = setup();
				scene.begin();
				await tick(10);
				made[0].opts.onBeat("settle", "end"); // the fade (600 ms) and the rise (600 ms) start
				await scene.replay();
				await scene.replay(); // a second click is not kept: one is
				expect(mod.play).toHaveBeenCalledTimes(1);
				made[0].finish(); // the art is gone
				await tick(10);
				expect(mod.play).toHaveBeenCalledTimes(1); // the hero is still rising
				await tick(600);
				expect(mod.play).toHaveBeenCalledTimes(1); // the hero glides down first
				await tick(700);
				expect(mod.play).toHaveBeenCalledTimes(2);
				expect(mod.play.mock.calls[1][1].replay).toBe(true);
			});

			it("is not lost when the page's clock does not move (a pinned or stepped clock): the rise is timed by a timer", async () => {
				// Only the timers are fake, so Date.now() stays where it is for the whole test.
				vi.useRealTimers();
				vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
				const { scene, mod, made } = setup();
				scene.begin();
				await tick(10);
				made[0].opts.onBeat("settle", "end");
				made[0].finish();
				await tick(10);
				await scene.replay(); // clicked during the rise
				expect(mod.play).toHaveBeenCalledTimes(1);
				await tick(700); // the rise (600 ms) is over; the descent follows
				expect(mod.play).toHaveBeenCalledTimes(1);
				await tick(700);
				expect(mod.play).toHaveBeenCalledTimes(2);
				expect(mod.play.mock.calls[1][1].replay).toBe(true);
			});

			it("a click once the rise is over plays at once", async () => {
				const { scene, mod, made } = setup();
				scene.begin();
				await tick(10);
				made[0].opts.onBeat("settle", "end");
				made[0].finish();
				await tick(700);
				await scene.replay(); // starts the descent at once
				expect(scene.phase.value).toBe("centring");
				await tick(700);
				expect(mod.play).toHaveBeenCalledTimes(2);
			});

			it("a new gesture before then forgets the waiting click", async () => {
				const { scene, mod, made } = setup();
				scene.begin();
				await tick(10);
				made[0].opts.onBeat("settle", "end");
				await scene.replay();
				scene.onPointerDown();
				made[0].finish();
				await tick(1000);
				expect(mod.play).toHaveBeenCalledTimes(1);
			});

			it("stop leaves no timer for it", async () => {
				const { scene, made } = setup();
				scene.begin();
				await tick(10);
				made[0].opts.onBeat("settle", "end");
				made[0].finish();
				await tick(10);
				await scene.replay();
				scene.stop();
				expect(vi.getTimerCount()).toBe(0);
			});

			it("the scene before is closed (its smile ended) when the replay starts", async () => {
				const { scene, made } = setup();
				scene.begin();
				await tick(10);
				scene.settle("early");
				await tick(300);
				await scene.replay();
				await tick(700);
				expect(made[0].settle).toHaveBeenLastCalledWith("instant");
			});

			it.each([
				["a key", (s) => s.onKeyDown()],
				["composer text or a hold (settle)", (s) => s.settle("early")],
			])(
				"%s before then forgets the waiting click: the cards are never hidden",
				async (_, gesture) => {
					const { scene, mod, made } = setup();
					scene.begin();
					await tick(10);
					made[0].opts.onBeat("settle", "end");
					await scene.replay(); // kept: the hero is still rising
					gesture(scene);
					made[0].finish();
					await tick(1000);
					expect(mod.play).toHaveBeenCalledTimes(1);
					expect(scene.phase.value).toBe("rest");
					expect(scene.cardsHidden.value).toBe(false);
				}
			);

			it.each([
				["composer text", { composerEmpty: () => false }],
				["reduced motion", { reduced: () => true }],
			])("%s by the time the click is run only smiles", async (_, rule) => {
				let live = false;
				const gated = Object.fromEntries(
					Object.entries(rule).map(([k, f]) => [k, () => live && f()])
				);
				if (gated.composerEmpty) gated.composerEmpty = () => !live;
				const { scene, mod, made } = setup(gated);
				scene.begin();
				await tick(10);
				made[0].opts.onBeat("settle", "end");
				made[0].finish();
				await tick(10);
				await scene.replay(); // clicked during the rise: allowed then
				live = true; // the rule starts to hold before the click is run
				await tick(700);
				expect(mod.play).toHaveBeenCalledTimes(1);
				expect(scene.phase.value).toBe("rest");
				expect(scene.cardsHidden.value).toBe(false);
				expect(scene.mark.happy).toBe(true);
			});
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
