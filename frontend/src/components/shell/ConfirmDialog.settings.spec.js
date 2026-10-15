import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { DialogRoot, DialogPortal, DialogContent, DialogTitle } from "reka-ui";

/**
 * A confirmation opened inside Settings can be operated by keyboard. Settings is a
 * modal reka dialog, which traps focus and pulls it back the moment it leaves; the
 * confirmation sits beside it in <body>, so it has to take the trap over.
 */

vi.mock("@/theme", async () => {
	const { ref } = await import("vue");
	return { useJarvisTheme: () => ({ effectiveDark: ref(false), paletteVars: {} }) };
});

import { confirm, settleConfirm } from "@/composables/useConfirm";
import ConfirmDialog from "./ConfirmDialog.vue";

// A modal dialog like Settings: a real reka DialogContent around a "Remove" button.
const Settings = defineComponent({
	render: () =>
		h(DialogRoot, { open: true }, () =>
			h(DialogPortal, null, () =>
				h(DialogContent, { "aria-describedby": undefined }, () => [
					h(DialogTitle, null, () => "Settings"),
					h("button", { class: "remove" }, "Remove"),
				])
			)
		),
});

const confirmDialog = () => document.querySelector('[role="alertdialog"]');
const confirmButtons = () => [...confirmDialog().querySelectorAll("button")];
const press = (key, extra = {}) => {
	const e = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...extra });
	window.dispatchEvent(e);
	return e;
};
const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));

let settings;
let shell;
async function openSettingsWithRemoveFocused() {
	settings = mount(Settings, { attachTo: document.body });
	shell = mount(ConfirmDialog, { attachTo: document.body });
	await flushPromises();
	await tick();
	const remove = document.querySelector(".remove");
	remove.focus();
	return remove;
}

afterEach(async () => {
	settleConfirm(false);
	await flushPromises();
	await tick();
	shell?.unmount();
	settings?.unmount();
	await flushPromises();
	document.body.style.pointerEvents = "";
});

describe("ConfirmDialog over Settings", () => {
	it("takes focus on Cancel and keeps it there, though the dialog underneath traps focus", async () => {
		const remove = await openSettingsWithRemoveFocused();
		confirm({ title: "Remove model?", danger: true, confirmLabel: "Remove" });
		await flushPromises();
		await tick();
		expect(document.body.style.pointerEvents).toBe("none");
		expect(document.activeElement).toBe(confirmButtons()[0]);
		expect(document.activeElement).not.toBe(remove);
	});

	it("cycles Tab and Shift+Tab over its buttons", async () => {
		await openSettingsWithRemoveFocused();
		confirm({ title: "Remove model?" });
		await flushPromises();
		await tick();
		const [cancel, ok] = confirmButtons();
		press("Tab");
		await tick();
		expect(document.activeElement).toBe(ok);
		press("Tab");
		await tick();
		expect(document.activeElement).toBe(cancel);
		press("Tab", { shiftKey: true });
		await tick();
		expect(document.activeElement).toBe(ok);
	});

	it("lets Enter and Space act on the focused button, and answers true when it is pressed", async () => {
		await openSettingsWithRemoveFocused();
		const answer = confirm({ title: "Remove model?" });
		await flushPromises();
		await tick();
		press("Tab");
		await tick();
		// Not cancelled: the browser turns these into a click on the focused button.
		expect(press("Enter").defaultPrevented).toBe(false);
		expect(press(" ").defaultPrevented).toBe(false);
		confirmButtons()[1].click();
		expect(await answer).toBe(true);
	});

	it("gives focus back to the button that opened it, inside Settings", async () => {
		const remove = await openSettingsWithRemoveFocused();
		const answer = confirm({ title: "Remove model?" });
		await flushPromises();
		await tick();
		confirmButtons()[0].click();
		expect(await answer).toBe(false);
		await flushPromises();
		await tick();
		expect(document.activeElement).toBe(remove);
	});

	it("hands Tab back to Settings' own trap once it is closed", async () => {
		const remove = await openSettingsWithRemoveFocused();
		confirm({ title: "Remove model?" });
		await flushPromises();
		await tick();
		settleConfirm(false);
		await flushPromises();
		await tick(50);
		expect(confirmDialog()).toBeNull();
		// Focus that strays out of Settings is pulled back in again.
		const outside = document.createElement("button");
		document.body.appendChild(outside);
		try {
			outside.focus();
			await tick();
			expect(document.activeElement).toBe(remove);
		} finally {
			outside.remove();
		}
	});
});
