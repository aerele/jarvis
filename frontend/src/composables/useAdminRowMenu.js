// What an Admin tab's rows need around their "Actions" menu: whether the list is
// drawn as cards (the menu then opens from the left), putting focus on an opened
// menu, running a picked item once the menu has gone, and finding a row's own
// button again after the list was re-read. Shared by the Macros and Skills tabs.
import { nextTick, onBeforeUnmount, ref, watch } from "vue";

// Below this width the list draws each row as a card (the styles' 48rem), with
// the Actions button at the card's start: its menu then opens from that side, or
// it would be pushed against the window's edge.
const CARD_BELOW_PX = 768;
const MENU_CLOSE_FRAMES = 40;
const menuStillOpen = () => !!document.querySelector("[data-reka-menu-content]");

/**
 * @param {object} o
 * @param {Ref} o.listEl          the list box
 * @param {Function} o.dialogOpen () -> true while a dialog is open over the tab
 * @param {string} o.rowSelector      each row, inside listEl
 * @param {string} o.rowKey           the row's `data-` key holding its name (dataset key)
 * @param {string} o.actionsSelector  a row's Actions button, inside the row
 * @param {string} o.openSelector     a row's "open" button, inside the row
 */
export function useAdminRowMenu(o) {
	const { listEl, dialogOpen } = o;

	const cardLayout = ref(false);
	let listWidth = null;
	watch(listEl, (el) => {
		if (listWidth) listWidth.disconnect();
		listWidth = null;
		if (!el) return;
		cardLayout.value = el.clientWidth < CARD_BELOW_PX;
		if (typeof ResizeObserver === "undefined") return;
		listWidth = new ResizeObserver(() => {
			cardLayout.value = el.clientWidth < CARD_BELOW_PX;
		});
		listWidth.observe(el);
	});
	onBeforeUnmount(() => {
		if (listWidth) listWidth.disconnect();
	});

	const rowEl = (name) =>
		listEl.value &&
		[...listEl.value.querySelectorAll(o.rowSelector)].find((r) => r.dataset[o.rowKey] === name);
	const actionsButton = (name) => {
		const el = rowEl(name);
		return el ? el.querySelector(o.actionsSelector) : null;
	};

	// The menu library does not move focus into a menu it opens, so the arrow keys and
	// Enter would not reach the items: focus it once it is drawn (as the list filters do).
	function focusOpenMenu() {
		requestAnimationFrame(() => document.querySelector("[data-reka-menu-content]")?.focus());
	}

	// An item was chosen. Two things have to settle before its action runs. The menu is
	// still closing, and a confirmation opened while it is there would not take the
	// keyboard. And the item that had focus goes away with the menu: focus is put on the
	// row's Actions button, so the dialog or confirmation has somewhere to give focus
	// back to when it is cancelled.
	//
	// One pick at a time: an item still takes a press while its menu fades out, and a
	// second pick would move focus out of the confirmation the first one opened.
	let picking = false;
	async function fromMenu(row, run) {
		if (picking) return;
		picking = true;
		try {
			await nextTick();
			let waited = false;
			for (let i = 0; i < MENU_CLOSE_FRAMES && menuStillOpen(); i++) {
				waited = true;
				await new Promise((done) => requestAnimationFrame(done));
			}
			// Once it is gone the menu hands focus back to its button, a moment later: let
			// that happen first, or it would take focus out of what opens next.
			if (waited) await new Promise((done) => setTimeout(done, 0));
			// A menu is open after all (another row's, opened meanwhile), the tab was
			// left, or something is already asking: this pick is dropped.
			if (menuStillOpen() || !listEl.value || dialogOpen()) return;
			const button = actionsButton(row.name);
			if (button && !button.disabled) button.focus();
			await run(row);
		} finally {
			picking = false;
		}
	}

	// After an action the button that had focus may be gone: put focus on the row's
	// own button, or on the list when the row went too, so a keyboard user is not
	// dropped at the top of the page.
	async function focusRow(name) {
		await nextTick();
		if (!listEl.value) return;
		const row = rowEl(name);
		const target = (row && row.querySelector(o.openSelector)) || listEl.value;
		target.focus();
	}

	return { cardLayout, focusOpenMenu, fromMenu, focusRow };
}
