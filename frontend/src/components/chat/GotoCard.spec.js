import { mount } from "@vue/test-utils";
import fs from "node:fs";
import path from "node:path";
import { nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";
import GotoCard from "./GotoCard.vue";

const source = fs.readFileSync(path.resolve(__dirname, "GotoCard.vue"), "utf8");

const CALENDAR = {
	title: "Payment Calendar",
	button: "Open Payment Calendar",
	sub: "Plan this month's payments",
	icon: "calendar",
	note: false,
};
const CHART = {
	title: "Dashboards",
	button: "Open Dashboards",
	sub: "Show sales by region",
	icon: "chart",
	note: false,
};
const NO_ACCESS = {
	title: "Use Dashboards instead",
	button: "Open Dashboards",
	sub: "Show payments due",
	icon: "chart",
	note: true,
};
const COUNTDOWN = {
	visible: "Opening Payment Calendar in 3…",
	srText: "Opening Payment Calendar in a few seconds. Press Escape to stay in chat.",
	stay: "Stay in chat",
	openNow: "Open now",
	openNowLabel: "Open now: Payment Calendar",
};

let wrappers = [];
afterEach(() => {
	wrappers.forEach((w) => w.unmount());
	wrappers = [];
	document.body.innerHTML = "";
});
function card(props = {}) {
	const wrapper = mount(GotoCard, {
		props: { copy: CALENDAR, countdown: null, ...props },
		attachTo: document.body,
	});
	wrappers.push(wrapper);
	return wrapper;
}
const emitted = (w, name) => (w.emitted(name) || []).map((args) => args[0]);
const openBtn = (w) => w.find(".jv-gcard-btn");
const stayBtn = (w) => w.find(".jv-gcard-stay");
const openNowBtn = (w) => w.find(".jv-gcard-now");

describe("GotoCard rendering", () => {
	it("renders the title, the subtitle and the open button", () => {
		const w = card();
		expect(w.find(".jv-gcard-title").text()).toBe("Payment Calendar");
		expect(w.find(".jv-gcard-sub").text()).toBe("Plan this month's payments");
		expect(openBtn(w).text()).toBe("Open Payment Calendar");
	});

	it("omits the subtitle line when there is none", () => {
		const w = card({ copy: { ...CALENDAR, sub: "" } });
		expect(w.find(".jv-gcard-sub").exists()).toBe(false);
	});

	it("draws the calendar icon, hidden from assistive tech", () => {
		const w = card();
		const svg = w.find(".jv-gcard-ic svg");
		expect(svg.attributes("aria-hidden")).toBe("true");
		expect(svg.attributes("data-icon")).toBe("calendar");
		expect(svg.find("rect").exists()).toBe(true);
		expect(svg.html()).toContain("M16 2v4M8 2v4M3 10h18");
	});

	it("draws the chart icon, hidden from assistive tech", () => {
		const w = card({ copy: CHART });
		const svg = w.find(".jv-gcard-ic svg");
		expect(svg.attributes("aria-hidden")).toBe("true");
		expect(svg.attributes("data-icon")).toBe("chart");
		expect(svg.html()).toContain("M18 20V10M12 20V4M6 20v-6");
	});

	it("shows the info icon for the no-access note", () => {
		const w = card({ copy: NO_ACCESS });
		const svg = w.find(".jv-gcard-ic svg");
		expect(svg.attributes("aria-hidden")).toBe("true");
		expect(svg.attributes("data-icon")).toBe("info");
		expect(svg.find("circle").attributes("r")).toBe("10");
		expect(svg.html()).toContain("M12 16v-4M12 8h.01");
		expect(w.findAll(".jv-gcard-ic svg")).toHaveLength(1);
	});

	it("emits open from the open button", async () => {
		const w = card();
		await openBtn(w).trigger("click");
		expect(w.emitted("open")).toHaveLength(1);
	});
});

describe("GotoCard countdown row", () => {
	it("swaps the open button for the status, Stay and Open now", () => {
		const w = card({ countdown: COUNTDOWN });
		expect(openBtn(w).exists()).toBe(false);
		expect(w.find(".jv-gcard-vis").text()).toBe("Opening Payment Calendar in 3…");
		expect(stayBtn(w).text()).toBe("Stay in chat");
		expect(openNowBtn(w).text()).toBe("Open now");
	});

	it("names Open now so it starts with its visible text (WCAG 2.5.3)", () => {
		const w = card({ countdown: COUNTDOWN });
		const name = openNowBtn(w).attributes("aria-label");
		expect(name).toBe("Open now: Payment Calendar");
		expect(name.startsWith(openNowBtn(w).text())).toBe(true);
	});

	it("keeps the status out of any live region and holds the full sr-only sentence", () => {
		const w = card({ countdown: COUNTDOWN });
		const status = w.find(".jv-gcard-status");
		expect(status.exists()).toBe(true);
		expect(status.attributes("aria-live")).toBeUndefined();
		expect(status.attributes("role")).toBeUndefined();
		expect(w.find("[aria-live]").exists()).toBe(false);
		expect(w.find("[role=status]").exists()).toBe(false);
		expect(status.find(".jv-sr").text()).toBe(COUNTDOWN.srText);
		// the ticking number is visual only
		expect(w.find(".jv-gcard-vis").attributes("aria-hidden")).toBe("true");
	});

	it("emits stay and open-now from their buttons", async () => {
		const w = card({ countdown: COUNTDOWN });
		await stayBtn(w).trigger("click");
		await openNowBtn(w).trigger("click");
		expect(w.emitted("stay")).toHaveLength(1);
		// the click event rides along so the view can tell a keyboard activation from a pointer click
		expect(w.emitted("stay")[0][0]).toBeInstanceOf(Event);
		expect(w.emitted("open-now")).toHaveLength(1);
		expect(w.emitted("open")).toBeUndefined();
	});

	it("shows the paused text as it is given", async () => {
		const w = card({ countdown: COUNTDOWN });
		const paused = "Paused · opening Payment Calendar in 2… Move away to continue.";
		await w.setProps({ countdown: { ...COUNTDOWN, visible: paused } });
		expect(w.find(".jv-gcard-vis").text()).toBe(paused);
	});
});

describe("GotoCard opening state (between the open and the arrival)", () => {
	const OPENING = "Opening Payment Calendar…";

	it("shows the opening text with the Open button disabled and no Stay or Open now", () => {
		const w = card({ opening: OPENING });
		expect(w.find(".jv-gcard-opening").text()).toBe(OPENING);
		expect(openBtn(w).exists()).toBe(true);
		expect(openBtn(w).attributes("disabled")).toBeDefined();
		expect(stayBtn(w).exists()).toBe(false);
		expect(openNowBtn(w).exists()).toBe(false);
	});

	it("is not a live region (the countdown already announced once)", () => {
		const w = card({ opening: OPENING });
		expect(w.find("[aria-live]").exists()).toBe(false);
		expect(w.find("[role=status]").exists()).toBe(false);
	});

	it("never emits open from the disabled Open button", async () => {
		const w = card({ opening: OPENING });
		await openBtn(w).trigger("click");
		expect(w.emitted("open")).toBeUndefined();
	});

	it("returns to the plain card, button enabled, when the opening text is cleared (navigation failed)", async () => {
		const w = card({ opening: OPENING });
		await w.setProps({ opening: "" });
		expect(w.find(".jv-gcard-opening").exists()).toBe(false);
		expect(openBtn(w).attributes("disabled")).toBeUndefined();
	});

	it("shows nothing extra without it", () => {
		const w = card();
		expect(w.find(".jv-gcard-opening").exists()).toBe(false);
		expect(openBtn(w).attributes("disabled")).toBeUndefined();
	});
});

describe("GotoCard pause and resume", () => {
	it("emits nothing while no countdown is set", async () => {
		const w = card();
		await w.trigger("pointerenter");
		await w.trigger("focusin");
		await w.trigger("pointerleave");
		await w.trigger("focusout");
		expect(w.emitted("pause")).toBeUndefined();
		expect(w.emitted("resume")).toBeUndefined();
	});

	it("pointerenter pauses for hover and pointerleave resumes it", async () => {
		const w = card({ countdown: COUNTDOWN });
		await w.trigger("pointerenter");
		expect(emitted(w, "pause")).toEqual(["hover"]);
		await w.trigger("pointerleave");
		expect(emitted(w, "resume")).toEqual(["hover"]);
	});

	it("focusin pauses for focus", async () => {
		const w = card({ countdown: COUNTDOWN });
		await w.trigger("focusin");
		expect(emitted(w, "pause")).toEqual(["focus"]);
	});

	it("resumes focus only when focus leaves the card", async () => {
		const w = card({ countdown: COUNTDOWN });
		await w.trigger("focusin");
		await w.trigger("focusout", { relatedTarget: stayBtn(w).element });
		expect(w.emitted("resume")).toBeUndefined();
		await w.trigger("focusout", { relatedTarget: document.body });
		expect(emitted(w, "resume")).toEqual(["focus"]);
	});

	it("keeps the focus pause when the window itself lost focus with focus still in the card", async () => {
		const w = card({ countdown: COUNTDOWN });
		stayBtn(w).element.focus();
		await w.trigger("focusin");
		const hasFocus = vi.spyOn(document, "hasFocus").mockReturnValue(false);
		try {
			await w.trigger("focusout", { relatedTarget: null });
			expect(w.emitted("resume")).toBeUndefined();
			// the window is back, focus really leaves the card: now it resumes
			hasFocus.mockReturnValue(true);
			stayBtn(w).element.blur();
			await w.trigger("focusout", { relatedTarget: null });
			expect(emitted(w, "resume")).toEqual(["focus"]);
		} finally {
			hasFocus.mockRestore();
		}
	});

	it("does not emit a second pause for a reason it already holds", async () => {
		const w = card({ countdown: COUNTDOWN });
		await w.trigger("pointerenter");
		await w.trigger("pointerenter");
		await w.setProps({
			countdown: { ...COUNTDOWN, visible: "Opening Payment Calendar in 2…" },
		});
		expect(emitted(w, "pause")).toEqual(["hover"]);
	});

	it("releases a held hover when the countdown becomes null", async () => {
		const w = card({ countdown: COUNTDOWN });
		await w.trigger("pointerenter");
		await w.setProps({ countdown: null });
		expect(emitted(w, "resume")).toEqual(["hover"]);
	});

	it("releases every held reason when the countdown becomes null", async () => {
		const w = card({ countdown: COUNTDOWN });
		await w.trigger("pointerenter");
		await w.trigger("focusin");
		await w.setProps({ countdown: null });
		expect(emitted(w, "resume").sort()).toEqual(["focus", "hover"]);
		// nothing is held any more: a later leave emits nothing more
		await w.trigger("pointerleave");
		expect(emitted(w, "resume")).toHaveLength(2);
	});

	it("releases held reasons on unmount", async () => {
		const w = mount(GotoCard, {
			props: { copy: CALENDAR, countdown: COUNTDOWN },
			attachTo: document.body,
		});
		await w.trigger("pointerenter");
		await w.trigger("focusin");
		w.unmount();
		expect(emitted(w, "resume").sort()).toEqual(["focus", "hover"]);
	});

	it("does not emit resume on unmount when nothing is held", () => {
		const w = mount(GotoCard, {
			props: { copy: CALENDAR, countdown: COUNTDOWN },
			attachTo: document.body,
		});
		w.unmount();
		expect(w.emitted("resume")).toBeUndefined();
	});
});

describe("GotoCard when the countdown starts with the pointer or focus already inside", () => {
	it("pauses for hover when the card is already hovered", async () => {
		const w = card();
		const el = w.element;
		const real = el.matches.bind(el);
		el.matches = (sel) => (sel === ":hover" ? true : real(sel));
		await w.setProps({ countdown: COUNTDOWN });
		expect(emitted(w, "pause")).toEqual(["hover"]);
		await w.trigger("pointerleave");
		expect(emitted(w, "resume")).toEqual(["hover"]);
	});

	// A touch screen keeps :hover on the last tapped element: only a device that can hover counts it.
	describe("hover at start on a device that cannot hover", () => {
		const realMatchMedia = window.matchMedia;
		afterEach(() => {
			window.matchMedia = realMatchMedia;
		});
		async function startHovered(hoverCapable) {
			const w = card();
			const el = w.element;
			const real = el.matches.bind(el);
			el.matches = (sel) => (sel === ":hover" ? true : real(sel));
			window.matchMedia = vi.fn((q) => ({
				matches: q === "(hover: hover)" && hoverCapable,
			}));
			await w.setProps({ countdown: COUNTDOWN });
			return w;
		}

		it("does not count a sticky :hover when the pointer cannot hover", async () => {
			const w = await startHovered(false);
			expect(window.matchMedia).toHaveBeenCalledWith("(hover: hover)");
			expect(w.emitted("pause")).toBeUndefined();
		});

		it("counts :hover when the pointer can hover", async () => {
			const w = await startHovered(true);
			expect(emitted(w, "pause")).toEqual(["hover"]);
		});

		it("counts :hover when matchMedia is missing", async () => {
			window.matchMedia = undefined;
			const w = card();
			const el = w.element;
			const real = el.matches.bind(el);
			el.matches = (sel) => (sel === ":hover" ? true : real(sel));
			await w.setProps({ countdown: COUNTDOWN });
			expect(emitted(w, "pause")).toEqual(["hover"]);
		});

		it("still pauses on a real pointerenter whatever the media query says", async () => {
			const w = card({ countdown: COUNTDOWN });
			window.matchMedia = vi.fn(() => ({ matches: false }));
			await w.trigger("pointerenter");
			expect(emitted(w, "pause")).toEqual(["hover"]);
		});
	});

	it("checks the pointer and focus on mount when the countdown is already set", () => {
		const real = Element.prototype.matches;
		const spy = vi.spyOn(Element.prototype, "matches").mockImplementation(function (sel) {
			return sel === ":hover" && this.classList?.contains("jv-gcard")
				? true
				: real.call(this, sel);
		});
		try {
			const w = card({ countdown: COUNTDOWN });
			expect(emitted(w, "pause")).toEqual(["hover"]);
		} finally {
			spy.mockRestore();
		}
	});

	it("does not pause on mount when the countdown is already set but nothing is inside", () => {
		const w = card({ countdown: COUNTDOWN });
		expect(w.emitted("pause")).toBeUndefined();
	});

	it("does not pause for hover when the pointer is elsewhere", async () => {
		const w = card();
		await w.setProps({ countdown: COUNTDOWN });
		expect(w.emitted("pause")).toBeUndefined();
	});

	it("pauses for focus and moves focus from the Open button to Open now", async () => {
		const w = card();
		openBtn(w).element.focus();
		expect(document.activeElement).toBe(openBtn(w).element);
		await w.setProps({ countdown: COUNTDOWN });
		await nextTick();
		expect(emitted(w, "pause")).toEqual(["focus"]);
		expect(document.activeElement).toBe(openNowBtn(w).element);
		// focus stays held across the move
		await w.trigger("focusin");
		expect(emitted(w, "pause")).toEqual(["focus"]);
	});

	it("never moves focus when it was not on the Open button", async () => {
		const outside = document.createElement("button");
		document.body.appendChild(outside);
		const w = card();
		outside.focus();
		await w.setProps({ countdown: COUNTDOWN });
		await nextTick();
		expect(w.emitted("pause")).toBeUndefined();
		expect(document.activeElement).toBe(outside);
	});
});

describe("GotoCard focusOpen", () => {
	it("focuses the Open button", () => {
		const w = card();
		w.vm.focusOpen();
		expect(document.activeElement).toBe(openBtn(w).element);
	});

	it("waits for the Open button to come back after a countdown", async () => {
		const w = card({ countdown: COUNTDOWN });
		// Not awaited: the Open button is not back in the DOM yet, so focusOpen must wait a tick.
		const pending = w.setProps({ countdown: null });
		expect(openBtn(w).exists()).toBe(false);
		await w.vm.focusOpen();
		expect(document.activeElement).toBe(openBtn(w).element);
		await pending;
	});
});

describe("GotoCard styles", () => {
	it("keeps the card narrow, a size container, with a narrow layout and tabular digits", () => {
		expect(source).toContain("max-width: 640px");
		expect(source).toContain("container-type: inline-size");
		expect(source).toContain("@container (max-width: 479px)");
		expect(source).toContain("tabular-nums");
		expect(source).toContain("min-height: 44px");
	});

	it("the narrow layout rule body stacks the status, stretches the buttons and wraps the head", () => {
		const at = source.indexOf("@container (max-width: 479px)");
		expect(at).not.toBe(-1);
		// the at-rule holds nested rules: cut it at its matching closing brace
		let depth = 0;
		let end = -1;
		for (let i = source.indexOf("{", at); i < source.length; i++) {
			if (source[i] === "{") depth++;
			if (source[i] === "}" && --depth === 0) {
				end = i;
				break;
			}
		}
		const body = source.slice(at, end);
		const rule = (selector) => {
			const m = body.match(new RegExp(selector + "\\s*\\{([^}]*)\\}"));
			expect(m, selector).not.toBeNull();
			return m[1];
		};
		expect(rule("\\.jv-gcard-cd")).toMatch(/flex-direction:\s*column/);
		expect(rule("\\.jv-gcard-head")).toMatch(/flex-wrap:\s*wrap/);
		expect(body).toMatch(
			/\.jv-gcard-stay,\s*\.jv-gcard-now\s*\{[^}]*flex:\s*1;[^}]*min-height:\s*44px/
		);
		expect(rule("\\.jv-gcard-btn")).toMatch(/min-height:\s*44px/);
		expect(body).toMatch(/overflow-wrap:\s*anywhere/);
	});

	it("rings the three buttons on keyboard focus", () => {
		const rule = source.match(/([^{}]*:focus-visible[^{}]*)\{([^}]*)\}/);
		expect(rule).not.toBeNull();
		expect(rule[1]).toContain(".jv-gcard-btn:focus-visible");
		expect(rule[1]).toContain(".jv-gcard-stay:focus-visible");
		expect(rule[1]).toContain(".jv-gcard-now:focus-visible");
		expect(rule[2]).toContain("outline: 2px solid var(--cta)");
		expect(rule[2]).toContain("outline-offset: 2px");
	});

	it("hides the sr-only text with clip-path as well as clip", () => {
		const rule = source.match(/\.jv-sr\s*\{([^}]*)\}/);
		expect(rule).not.toBeNull();
		expect(rule[1]).toContain("clip-path: inset(50%)");
	});

	it("overrides the secondary button hover for the dark theme", () => {
		expect(source).toMatch(/\.jv-dark \.jv-gcard-stay:hover/);
	});

	it("adds no animation", () => {
		expect(source).not.toMatch(/@keyframes|animation:/);
	});
});
