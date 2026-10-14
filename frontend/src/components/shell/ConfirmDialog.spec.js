import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The app-wide confirmation dialog and the keyboard: it takes focus when it opens,
 * keeps Tab on its two buttons, and gives focus back when it closes. The dialog is
 * teleported to <body>, so it is found on the document, not in the wrapper.
 */

vi.mock("@/theme", async () => {
	const { ref } = await import("vue");
	return { useJarvisTheme: () => ({ effectiveDark: ref(false), paletteVars: {} }) };
});

import { confirm, settleConfirm } from "@/composables/useConfirm";
import ConfirmDialog from "./ConfirmDialog.vue";

const dialog = () => document.querySelector('[role="alertdialog"]');
const buttons = () => [...dialog().querySelectorAll("button")];
const press = (key, extra = {}) =>
	window.dispatchEvent(
		new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...extra })
	);

let w;
let opener;
// What the app's own shortcut listeners would hear (they listen on window).
let heard = [];
const onAppKey = (e) => heard.push(e.key);
async function setup() {
	opener = document.createElement("button");
	opener.textContent = "Delete";
	document.body.appendChild(opener);
	w = mount(ConfirmDialog, { attachTo: document.body });
	opener.focus();
	// After the dialog's own listener, as the app's are: they are added later.
	heard = [];
	window.addEventListener("keydown", onAppKey);
}
async function ask(opts = {}) {
	const answer = confirm({
		title: "Delete this?",
		confirmLabel: "Delete",
		danger: true,
		...opts,
	});
	await flushPromises();
	return answer;
}

afterEach(async () => {
	window.removeEventListener("keydown", onAppKey);
	settleConfirm(false);
	await flushPromises();
	if (w) w.unmount();
	if (opener) opener.remove();
	document.body.style.pointerEvents = "";
});

describe("ConfirmDialog, keyboard focus", () => {
	it("takes focus on Cancel when it opens", async () => {
		await setup();
		ask();
		await flushPromises();
		expect(dialog()).not.toBeNull();
		expect(document.activeElement).toBe(buttons()[0]);
		expect(buttons()[0].textContent.trim()).toBe("Cancel");
	});

	it("keeps Tab and Shift+Tab on its two buttons", async () => {
		await setup();
		ask();
		await flushPromises();
		const [cancel, ok] = buttons();
		press("Tab");
		expect(document.activeElement).toBe(ok);
		press("Tab");
		expect(document.activeElement).toBe(cancel);
		press("Tab", { shiftKey: true });
		expect(document.activeElement).toBe(ok);
		press("Tab", { shiftKey: true });
		expect(document.activeElement).toBe(cancel);
	});

	it("gives focus back to what opened it, whichever way it closes", async () => {
		await setup();
		for (const close of [
			() => buttons()[0].click(),
			() => buttons()[1].click(),
			() => press("Escape"),
		]) {
			opener.focus();
			ask();
			await flushPromises();
			expect(document.activeElement).not.toBe(opener);
			close();
			await flushPromises();
			expect(dialog()).toBeNull();
			expect(document.activeElement).toBe(opener);
		}
	});

	it("answers true for the confirm button and false for Cancel and Escape", async () => {
		await setup();
		let answer = ask();
		await flushPromises();
		buttons()[1].click();
		expect(await answer).toBe(true);
		answer = ask();
		await flushPromises();
		buttons()[0].click();
		expect(await answer).toBe(false);
		answer = ask();
		await flushPromises();
		press("Escape");
		expect(await answer).toBe(false);
	});

	it("pulls Tab into the dialog from wherever focus is, and leaves Ctrl+Tab alone", async () => {
		await setup();
		ask();
		await flushPromises();
		const [cancel, ok] = buttons();
		// Focus got out (a click on the page behind): Tab comes back in.
		opener.focus();
		press("Tab");
		expect(document.activeElement).toBe(cancel);
		opener.focus();
		press("Tab", { shiftKey: true });
		expect(document.activeElement).toBe(ok);
		const ctrlTab = new KeyboardEvent("keydown", {
			key: "Tab",
			ctrlKey: true,
			bubbles: true,
			cancelable: true,
		});
		window.dispatchEvent(ctrlTab);
		expect(ctrlTab.defaultPrevented).toBe(false);
		expect(document.activeElement).toBe(ok);
	});

	it("says what it asks to a screen reader when focus lands on Cancel", async () => {
		await setup();
		ask({ message: "This cannot be undone.", warning: "Three runs will stop." });
		await flushPromises();
		const described = document.getElementById(dialog().getAttribute("aria-describedby"));
		expect(described.textContent).toContain("This cannot be undone.");
		expect(described.textContent).toContain("Three runs will stop.");
		buttons()[0].click();
		await flushPromises();
		ask(); // a title alone: nothing to point at
		await flushPromises();
		expect(dialog().hasAttribute("aria-describedby")).toBe(false);
	});

	it("does not take focus back from a caller that moved it", async () => {
		await setup();
		const elsewhere = document.createElement("button");
		document.body.appendChild(elsewhere);
		// The caller's continuation hangs on the confirm() promise itself, so it runs
		// before the dialog decides where focus goes: it focuses something of its own.
		const answered = confirm({ title: "Delete this?" }).then(() => elsewhere.focus());
		await flushPromises();
		const backToOpener = vi.spyOn(opener, "focus");
		buttons()[1].click();
		await answered;
		await flushPromises();
		expect(document.activeElement).toBe(elsewhere);
		expect(backToOpener).not.toHaveBeenCalled();
		backToOpener.mockRestore();
		elsewhere.remove();
	});

	it("does not mind when what opened it is gone or disabled by the time it closes", async () => {
		await setup();
		ask();
		await flushPromises();
		opener.remove();
		const focusGone = vi.spyOn(opener, "focus");
		buttons()[1].click();
		await flushPromises();
		expect(dialog()).toBeNull();
		expect(focusGone).not.toHaveBeenCalled();
		focusGone.mockRestore();

		document.body.appendChild(opener);
		opener.focus();
		ask();
		await flushPromises();
		opener.disabled = true;
		const focusDisabled = vi.spyOn(opener, "focus");
		buttons()[0].click();
		await flushPromises();
		expect(focusDisabled).not.toHaveBeenCalled();
		expect(document.activeElement).not.toBe(opener);
		focusDisabled.mockRestore();
		opener.disabled = false;
	});

	it("does not pass a key on to the app's shortcuts while it is open", async () => {
		await setup();
		ask();
		await flushPromises();
		press("k", { metaKey: true });
		press("o", { ctrlKey: true, shiftKey: true });
		press("n");
		expect(heard).toEqual([]);
		// A function key still gets through: one of them moves to the notifications.
		press("F8");
		expect(heard).toEqual(["F8"]);
		settleConfirm(false);
		await flushPromises();
		press("k", { metaKey: true });
		expect(heard).toEqual(["F8", "k"]);
	});

	it("leaves what a key does by itself alone", async () => {
		// Only the journey to the app's listeners is stopped: Enter or Space on the
		// focused button, and the browser's own shortcuts, are not cancelled.
		await setup();
		ask();
		await flushPromises();
		for (const [key, extra] of [["Enter"], [" "], ["r", { metaKey: true }], ["a"]])
			expect(press(key, extra)).toBe(true);
	});

	it("takes focus without scrolling a long question past its title", async () => {
		await setup();
		ask();
		const cancel = () => buttons()[0];
		let options;
		const real = HTMLElement.prototype.focus;
		HTMLElement.prototype.focus = function (o) {
			if (this.textContent.trim() === "Cancel") options = o;
			return real.call(this, o);
		};
		try {
			await flushPromises();
		} finally {
			HTMLElement.prototype.focus = real;
		}
		expect(document.activeElement).toBe(cancel());
		expect(options).toEqual({ preventScroll: true });
	});
});
