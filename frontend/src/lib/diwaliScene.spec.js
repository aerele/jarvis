import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { play, FLASHES, END_MS } from "@jsshared/holiday/scenes/diwali.mjs";

const BOXES = {
	host: { w: 1170, h: 640 },
	mark: { x: 540, y: 80, w: 90, h: 90 },
	heading: { x: 400, y: 214, w: 370, h: 44 },
	cards: { x: 245, y: 400, w: 680, h: 230 },
};

let host;
let beats;
let animations;
const onBeat = (...a) => beats.push(a);
const names = () => beats.map((b) => b[0]);
const run = (extra = {}) =>
	play(host, {
		layout: "desktop",
		getBoxes: () => BOXES,
		year: 2026,
		idPrefix: "jvs7",
		reduced: false,
		onBeat,
		...extra,
	});

describe("the Diwali scene", () => {
	beforeEach(() => {
		vi.useFakeTimers();
		host = document.createElement("div");
		document.body.appendChild(host);
		beats = [];
		animations = [];
		Element.prototype.animate = vi.fn(function (frames, options) {
			const anim = { el: this, frames, options, cancel: vi.fn() };
			animations.push(anim);
			return anim;
		});
	});
	afterEach(() => {
		host.remove();
		vi.useRealTimers();
	});

	it("draws one layer into the host, with ids and classes of its own", () => {
		run();
		const svg = host.querySelector("svg");
		expect(svg).not.toBeNull();
		expect(svg.getAttribute("aria-hidden")).toBe("true");
		const ids = [...svg.querySelectorAll("[id]")].map((e) => e.id);
		expect(ids.length).toBeGreaterThan(5);
		expect(ids.every((id) => id.startsWith("jvs7-"))).toBe(true);
		expect(new Set(ids).size).toBe(ids.length);
	});

	it("plays in order: face, eyes, spark, item and smile, greeting swap, face settles, end", async () => {
		const scene = run();
		expect(names()[0]).toBe("peek");
		await vi.advanceTimersByTimeAsync(END_MS + 700);
		const seq = names();
		const at = (n) => seq.indexOf(n);
		expect(at("look")).toBeGreaterThan(at("peek"));
		expect(at("deliver")).toBeGreaterThan(at("look"));
		expect(at("happy")).toBeGreaterThan(at("deliver") - 1);
		expect(at("swap")).toBeGreaterThan(at("happy"));
		expect(seq.lastIndexOf("peek")).toBeGreaterThan(at("swap"));
		expect(seq.at(-1)).toBe("settle");
		expect(beats.at(-1)[1]).toBe("end");
		await scene.done;
		expect(host.querySelector("svg")).toBeNull();
		expect(vi.getTimerCount()).toBe(0);
	});

	it("the flashes it animates are exactly the ones in the flash table", async () => {
		run();
		await vi.advanceTimersByTimeAsync(3400); // the lit flash is made when the spark starts
		const flashAnims = animations.filter((a) =>
			/(^|-)(flash\d|lit)\b/.test(a.el.getAttribute("class") || "")
		);
		expect(flashAnims).toHaveLength(FLASHES.length);
		const starts = flashAnims.map(
			(a) => a.options.delay + (a.el.classList.contains("jvs7-lit") ? 3300 : 0)
		);
		expect(starts.sort((a, b) => a - b)).toEqual(
			FLASHES.map((f) => f.at).sort((a, b) => a - b)
		);
	});

	it("the circles it draws for the flashes have the radii of the flash table", async () => {
		run();
		await vi.advanceTimersByTimeAsync(3400); // the lit flash is made when the spark starts
		for (const f of FLASHES) {
			const circles = [...host.querySelectorAll(`.jvs7-${f.cls}`)];
			expect(circles, f.cls).toHaveLength(1);
			expect(Number(circles[0].getAttribute("r")), f.cls).toBe(f.r);
		}
	});

	it("early: the greeting swaps, the item lands with the smile, the art fades in 250 ms and the face settles", async () => {
		const scene = run();
		await vi.advanceTimersByTimeAsync(500);
		scene.settle("early");
		const seq = names();
		expect(seq).toContain("deliver");
		expect(seq).toContain("happy");
		expect(beats.find((b) => b[0] === "swap")[1]).toEqual({ instant: false });
		expect(beats.find((b) => b[0] === "settle")[1]).toBe("early");
		const fade = animations.at(-1);
		expect(fade.options.duration).toBe(250);
		await vi.advanceTimersByTimeAsync(300);
		await scene.done;
		expect(host.querySelector("svg")).toBeNull();
		const before = names().filter((n) => n === "peek").length;
		await vi.advanceTimersByTimeAsync(1600);
		expect(names().filter((n) => n === "peek").length).toBe(before + 1);
		expect(vi.getTimerCount()).toBe(0);
	});

	it("instant: no motion, the rest beats at once, nothing left behind", async () => {
		const scene = run();
		scene.settle("instant");
		expect(names().slice(-4)).toEqual(["settle", "swap", "deliver", "peek"]);
		expect(beats.find((b) => b[0] === "swap")[1]).toEqual({ instant: true });
		expect(host.querySelector("svg")).toBeNull();
		await scene.done;
		expect(vi.getTimerCount()).toBe(0);
	});

	it("an instant settle after an early one also ends the smile's tail", async () => {
		const scene = run();
		scene.settle("early");
		scene.settle("instant");
		await scene.done;
		expect(vi.getTimerCount()).toBe(0);
	});

	it("settling twice acts once", () => {
		const scene = run();
		scene.settle("early");
		const count = beats.length;
		scene.settle("early");
		expect(beats).toHaveLength(count);
	});

	it("the replay has no floor props and no greeting swap", async () => {
		const normal = run();
		const withFloor = host.querySelectorAll("[class*=dflame]").length;
		normal.settle("instant");
		beats = [];
		const again = run({ replay: true });
		expect(withFloor).toBeGreaterThan(0);
		expect(host.querySelectorAll("[class*=dflame]").length).toBe(0);
		await vi.advanceTimersByTimeAsync(END_MS + 700);
		expect(names()).not.toContain("swap");
		await again.done;
	});

	it("a phone layout or reduced motion is only the rest state, at once", async () => {
		for (const extra of [{ layout: "phone" }, { reduced: true }]) {
			beats = [];
			const scene = run(extra);
			expect(host.querySelector("svg")).toBeNull();
			expect(names()).toEqual([]);
			scene.settle("early");
			expect(names()).toEqual(["settle", "swap", "deliver", "peek"]);
			await scene.done;
		}
	});

	it("a cancelled scene leaves no unhandled rejection and no timer", async () => {
		const unhandled = vi.fn();
		process.on("unhandledRejection", unhandled);
		const scene = run();
		await vi.advanceTimersByTimeAsync(2000);
		scene.settle("instant");
		await scene.done;
		await vi.advanceTimersByTimeAsync(10000);
		process.off("unhandledRejection", unhandled);
		expect(unhandled).not.toHaveBeenCalled();
		expect(vi.getTimerCount()).toBe(0);
	});

	it("the toran covers the whole width, wide or narrow", () => {
		const wide = {
			...BOXES,
			host: { w: 2200, h: 900 },
			mark: { x: 1055, y: 80, w: 90, h: 90 },
		};
		run({ getBoxes: () => wide });
		const marigolds = host.querySelectorAll("[class*=toran] circle").length;
		host.innerHTML = "";
		run({ getBoxes: () => BOXES });
		expect(marigolds).toBeGreaterThan(host.querySelectorAll("[class*=toran] circle").length);
	});
});
