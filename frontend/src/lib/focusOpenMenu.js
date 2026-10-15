// The menu library (reka-ui) does not move focus into a menu it opens, so the
// arrow keys and Enter would not reach the items. Call this when a menu opens:
// it focuses the menu once it is drawn.
//
// `target` says which menu:
//   - a trigger element: the menu that trigger controls (its aria-controls id), so
//     another open menu is never taken by mistake;
//   - a selector string: the first match, for a menu with its own marker;
//   - nothing: the library's menu (REKA_MENU).
export const REKA_MENU = "[data-reka-menu-content]";

export function focusOpenMenu(target = REKA_MENU) {
	requestAnimationFrame(() => menuOf(target)?.focus());
}

function menuOf(target) {
	if (typeof target === "string") return document.querySelector(target);
	const id = target?.getAttribute?.("aria-controls");
	return id ? document.getElementById(id) : null;
}
