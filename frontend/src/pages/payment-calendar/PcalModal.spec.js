import { afterEach, describe, expect, it } from "vitest";
import { defineComponent, h, nextTick, ref } from "vue";
import { mount } from "@vue/test-utils";
import PcalModal from "./PcalModal.vue";
import { provideOverlayStack } from "./overlayStack";

const $ = (selector) => document.body.querySelector(selector);
const press = (key, init = {}) => {
	const target = document.activeElement || document.body;
	const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
	target.dispatchEvent(event);
	return event;
};

// Page-like host: an opener button, the fallback target and two modals sharing one stack.
const Host = defineComponent({
	components: { PcalModal },
	setup() {
		provideOverlayStack();
		return {
			details: ref(false),
			aux: ref(false),
			openerGone: ref(false),
		};
	},
	template: `<div>
		<button v-if="!openerGone" id="opener" @click="details = true">Open</button>
		<button id="selected-invoices">Fallback</button>
		<PcalModal v-model="details" title="Details" close-label="Close invoice details" size="lg">
			<input id="first-field" />
			<button id="open-aux" @click="aux = true">More</button>
			<button id="last-button">Last</button>
		</PcalModal>
		<PcalModal v-model="aux" title="Aux" close-label="Close account balances" layer="aux" size="sm">
			<button id="aux-action">Act</button>
		</PcalModal>
	</div>`,
});

describe("PcalModal", () => {
	let wrapper;
	afterEach(() => {
		wrapper?.unmount();
		document.body.innerHTML = "";
	});
	async function mountHost() {
		wrapper = mount(Host, { attachTo: document.body });
		await nextTick();
	}
	async function openDetails() {
		$("#opener").focus();
		wrapper.vm.details = true;
		await nextTick();
		await nextTick();
	}

	it("teleports to body as a labelled aria-modal dialog carrying .pcal-overlay", async () => {
		await mountHost();
		expect($(".pcal-overlay")).toBeNull();
		await openDetails();
		const overlay = $(".pcal-overlay");
		expect(overlay.parentElement).toBe(document.body);
		expect(wrapper.element.contains(overlay)).toBe(false);
		const dialog = overlay.querySelector('[role="dialog"]');
		expect(dialog.getAttribute("aria-modal")).toBe("true");
		const heading = document.getElementById(dialog.getAttribute("aria-labelledby"));
		expect(heading.tagName).toBe("H2");
		expect(heading.textContent).toBe("Details");
	});

	it("moves focus to Close on open", async () => {
		await mountHost();
		await openDetails();
		const close = $('button[aria-label="Close invoice details"]');
		expect(close).not.toBeNull();
		expect(document.activeElement).toBe(close);
	});

	it("wraps Tab from the last control to the first, and Shift+Tab back", async () => {
		await mountHost();
		await openDetails();
		const close = $('button[aria-label="Close invoice details"]');
		$("#last-button").focus();
		const forward = press("Tab");
		expect(forward.defaultPrevented).toBe(true);
		expect(document.activeElement).toBe(close);
		const back = press("Tab", { shiftKey: true });
		expect(back.defaultPrevented).toBe(true);
		expect(document.activeElement).toBe($("#last-button"));
	});

	it("leaves Tab alone between the first and the last control", async () => {
		await mountHost();
		await openDetails();
		$("#first-field").focus();
		expect(press("Tab").defaultPrevented).toBe(false);
		expect(press("Tab", { shiftKey: true }).defaultPrevented).toBe(false);
	});

	it("makes the dialog itself focusable, so a click on plain text keeps focus inside", async () => {
		await mountHost();
		await openDetails();
		expect($(".pcal-modal").getAttribute("tabindex")).toBe("-1");
	});

	it("wraps Tab from the dialog itself: Shift+Tab to the last control, Tab to the first", async () => {
		await mountHost();
		await openDetails();
		const dialog = $(".pcal-modal");
		dialog.focus();
		expect(document.activeElement).toBe(dialog);
		const back = press("Tab", { shiftKey: true });
		expect(back.defaultPrevented).toBe(true);
		expect(document.activeElement).toBe($("#last-button"));
		dialog.focus();
		const forward = press("Tab");
		expect(forward.defaultPrevented).toBe(true);
		expect(document.activeElement).toBe($('button[aria-label="Close invoice details"]'));
	});

	it("does not let a press on the backdrop pull focus out of the dialog", async () => {
		await mountHost();
		await openDetails();
		const press = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
		$(".pcal-overlay").dispatchEvent(press);
		expect(press.defaultPrevented).toBe(true);
	});

	it("puts focus back on the dialog when a click ends with focus outside it", async () => {
		await mountHost();
		await openDetails();
		document.activeElement.blur();
		expect(document.activeElement).toBe(document.body);
		$(".pcal-overlay").dispatchEvent(new MouseEvent("click", { bubbles: true }));
		expect(document.activeElement).toBe($(".pcal-modal"));
	});

	const mouse = (node, type) =>
		node.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }));
	const click = (node) => {
		mouse(node, "mousedown");
		mouse(node, "mouseup");
	};

	it("closes when the press and the release are both on the backdrop", async () => {
		await mountHost();
		await openDetails();
		click($("#first-field"));
		await nextTick();
		expect(wrapper.vm.details).toBe(true);
		click($(".pcal-overlay"));
		await nextTick();
		expect(wrapper.vm.details).toBe(false);
		expect($(".pcal-overlay")).toBeNull();
	});

	it("stays open when a drag starts inside the dialog and ends on the backdrop, or the reverse", async () => {
		await mountHost();
		await openDetails();
		mouse($("#first-field"), "mousedown");
		mouse($(".pcal-overlay"), "mouseup");
		await nextTick();
		expect(wrapper.vm.details).toBe(true);
		mouse($(".pcal-overlay"), "mousedown");
		mouse($("#first-field"), "mouseup");
		await nextTick();
		expect(wrapper.vm.details).toBe(true);
		// the aborted press does not arm a later stray release
		mouse($(".pcal-overlay"), "mouseup");
		await nextTick();
		expect(wrapper.vm.details).toBe(true);
	});

	it("leaves focus alone when it mounts closed", async () => {
		const outside = document.createElement("input");
		document.body.appendChild(outside);
		outside.focus();
		await mountHost();
		await nextTick();
		await nextTick();
		expect(document.activeElement).toBe(outside);
	});

	it("closes with the Close button and returns focus to the opener", async () => {
		await mountHost();
		await openDetails();
		$('button[aria-label="Close invoice details"]').click();
		await nextTick();
		await nextTick();
		expect(document.activeElement).toBe($("#opener"));
	});

	it("returns focus to #selected-invoices when the opener is gone", async () => {
		await mountHost();
		await openDetails();
		wrapper.vm.openerGone = true;
		await nextTick();
		expect($("#opener")).toBeNull();
		wrapper.vm.details = false;
		await nextTick();
		await nextTick();
		expect(document.activeElement).toBe($("#selected-invoices"));
	});

	it("Escape closes the aux modal first, then the details modal", async () => {
		await mountHost();
		await openDetails();
		$("#open-aux").focus();
		wrapper.vm.aux = true;
		await nextTick();
		await nextTick();
		press("Escape");
		await nextTick();
		expect(wrapper.vm.aux).toBe(false);
		expect(wrapper.vm.details).toBe(true);
		await nextTick();
		expect(document.activeElement).toBe($("#open-aux"));
		press("Escape");
		await nextTick();
		expect(wrapper.vm.details).toBe(false);
	});

	it("labels each modal by its own heading", async () => {
		await mountHost();
		await openDetails();
		wrapper.vm.aux = true;
		await nextTick();
		const ids = [...document.querySelectorAll('[role="dialog"]')].map((node) =>
			node.getAttribute("aria-labelledby")
		);
		expect(new Set(ids).size).toBe(2);
	});
});
