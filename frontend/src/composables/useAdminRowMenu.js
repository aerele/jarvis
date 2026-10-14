// What an Admin tab's rows need around their "Actions" menu: whether the list is
// drawn as cards (the menu then opens from the left), putting focus on an opened
// menu, running a picked item once the menu has gone, and finding a row's own
// button again after the list was re-read. Shared by the Macros and Skills tabs.
import { nextTick, onBeforeUnmount, ref, watch } from "vue";
import { focusLost } from "@/composables/useAdminList";

// Up to this width the list draws each row as a card (the styles' `max-width: 48rem`
// container query), with the Actions button at the card's start: its menu then opens
// from that side, or it would be pushed against the window's edge.
const CARD_MAX_REM = 48;
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
 * @param {Ref} o.rows                the list's rows (each has `name`, `admin_hold`)
 * @param {Ref} o.error               the list's load error text
 * @param {Function} o.settled        () -> promise, resolved when no load is in flight
 * @param {Function} o.searchBox      () -> the search input, or null
 */
export function useAdminRowMenu(o) {
	const { listEl, dialogOpen } = o;

	const cardLayout = ref(false);
	let listWidth = null;
	watch(listEl, (el) => {
		if (listWidth) listWidth.disconnect();
		listWidth = null;
		if (!el) return;
		// The root font size is read once per list box, not on every resize tick.
		const limit = cardLimitPx();
		const measure = () => {
			cardLayout.value = listPx(el) <= limit;
		};
		measure();
		if (typeof ResizeObserver === "undefined") return;
		listWidth = new ResizeObserver(measure);
		listWidth.observe(el);
	});
	onBeforeUnmount(() => {
		if (listWidth) listWidth.disconnect();
	});

	const rowEl = (name) =>
		listEl.value &&
		[...listEl.value.querySelectorAll(o.rowSelector)].find(
			(r) => r.dataset[o.rowKey] === name
		);
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
	//
	// Known gap, only seen on a real machine: the flag is held until the menu has gone
	// and the action has run, so a pick in a second row's menu within about half a
	// second of a pick in the first is dropped and must be made again.
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
			//
			// Known gap, only seen on a real machine: a cancelled question stays in the
			// page while it fades out, so a keyboard pick made within about 150 ms of
			// cancelling one finds it still asking and does nothing.
			if (menuStillOpen() || !listEl.value || dialogOpen()) return;
			const button = actionsButton(row.name);
			if (button && !button.disabled) button.focus();
			await run(row);
		} finally {
			picking = false;
		}
	}

	// After an action the button that had focus may be gone: put focus on the row's
	// own button, or on the list when the row went too (the search box when no list
	// is drawn), so a keyboard user is not dropped at the top of the page.
	async function focusRow(name) {
		await nextTick();
		const row = rowEl(name);
		const target =
			(row && row.querySelector(o.openSelector)) ||
			listEl.value ||
			(o.searchBox && o.searchBox());
		if (target) target.focus();
	}

	// The hold dialog closed. It gave focus back to the Actions button that opened it,
	// which is gone when the row now shows Release, or when the hold was made (`made`:
	// the dialog said so, even if it answered an error) and a filter drops the macro.
	// Decided once the reload has settled: mid-reload the list says nothing true, and
	// a plain Cancel (nothing made, nothing shown held) moves focus nowhere.
	async function focusAfterHold(name, { made = false } = {}) {
		await o.settled();
		const row = o.rows.value.find((r) => r.name === name);
		const shownHeld = !!row && !!row.admin_hold;
		const dropped = made && !row && !o.error.value;
		if (!shownHeld && !dropped) return;
		await nextTick();
		if (focusLost() && !dialogOpen()) await focusRow(name);
	}

	return { cardLayout, focusOpenMenu, fromMenu, focusRow, focusAfterHold };
}

// The styles count in rem, so the script must too: with a root font size other than
// 16px the same width is a card in one and not in the other. The query includes its
// limit, hence `<=`.
export function cardLimitPx() {
	const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
	return CARD_MAX_REM * rem;
}

// The list box's width as the container query sees it: fractional, not rounded as
// `clientWidth` is (the box has no padding or border). jsdom lays nothing out, so a
// zero rect falls back to `clientWidth`.
function listPx(el) {
	return el.getBoundingClientRect().width || el.clientWidth;
}
