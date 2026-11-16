// The keyboard of the panels of a SERVER draft card on the desktop (its edit form and its
// Preview): what Escape does, where Tab goes, where focus lands when a panel opens and where it
// returns when it closes. Pure functions over DOM nodes handed in: no Vue, no store. Relative
// imports only.
//
// There is no focus trap in the app to reuse: frappe-ui's Dialog traps focus inside its own
// headlessui markup, and the panels here are overlays of their own (one is a shared component
// the typed path also mounts), so a keydown handler on the window does the job, active only
// while one of these panels is open.

const FOCUSABLE =
	'a[href], button, input:not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"])';

const visible = (el) => (typeof el.checkVisibility === "function" ? el.checkVisibility() : true);

// The controls of `root` that Tab can reach, in document order.
export function tabbables(root) {
	if (!root || typeof root.querySelectorAll !== "function") return [];
	return [...root.querySelectorAll(FOCUSABLE)].filter(
		(el) =>
			!el.disabled &&
			!el.hidden &&
			el.getAttribute("aria-hidden") !== "true" &&
			el.getAttribute("tabindex") !== "-1" &&
			visible(el)
	);
}

// Where Tab must go to stay inside `root`, or null to let the browser move focus. Focus outside
// the panel comes in at its first (Shift+Tab: last) control; at the last control Tab wraps to the
// first, and Shift+Tab at the first wraps to the last.
export function tabWrapTarget(root, active, shift) {
	const list = tabbables(root);
	if (!list.length) return null;
	const first = list[0];
	const last = list[list.length - 1];
	if (!active || !root.contains(active) || active === root) return shift ? last : first;
	if (shift && active === first) return last;
	if (!shift && active === last) return first;
	return null;
}

// The control the edit form puts focus in when it opens: its first editable field. A Link input
// is skipped: focusing it opens its suggestions menu (and a search) before the person asked for
// it. With no such field, the first control of the panel, else the panel itself.
export function firstEditable(panel) {
	if (!panel || typeof panel.querySelector !== "function") return null;
	const body = panel.querySelector(".jv-draft-body") || panel;
	const fields = [...body.querySelectorAll("input, select, textarea")].filter(
		(el) =>
			!el.disabled &&
			!el.readOnly &&
			el.type !== "hidden" &&
			!(el.getAttribute("autocomplete") === "off" && /^Search /.test(el.placeholder || ""))
	);
	return fields[0] || tabbables(panel)[0] || panel;
}

// What a key press does while a panel of a server draft card is open:
//   "close-preview", "close-form", "refocus-form" (a form with edits stays open, as a click on the
//   backdrop does), or null (not ours). `key` is the event's; a key already handled by something
//   inside the panel (a link menu, a row form), an IME composition, or an open dialog is not ours.
export function toolPanelKeyAction({
	key,
	defaultPrevented = false,
	composing = false,
	dialogOpen = false,
	preview = false,
	form = false,
	dirty = false,
}) {
	if (key !== "Escape" || defaultPrevented || composing || dialogOpen) return null;
	if (preview) return "close-preview";
	if (form) return dirty ? "refocus-form" : "close-form";
	return null;
}

// The card button that opened a panel, found again in the DOM (the card re-renders, so a saved
// element reference would be stale): `label` is "Preview" or "Edit". null when the card is gone
// or its button is disabled (a disabled control cannot take focus).
export function openerButton(root, token, label) {
	if (!root || typeof root.querySelectorAll !== "function") return null;
	const card = [...root.querySelectorAll("[data-draft-token]")].find(
		(el) => el.getAttribute("data-draft-token") === token
	);
	if (!card) return null;
	const button = [...card.querySelectorAll("button.dc-btn")].find(
		(b) => (b.textContent || "").trim() === label
	);
	return button && !button.disabled ? button : null;
}
