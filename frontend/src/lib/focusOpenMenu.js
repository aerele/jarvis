// The menu library (reka-ui) does not move focus into a menu it opens, so the
// arrow keys and Enter would not reach the items. Call this when a menu opens:
// it focuses the menu once it is drawn. `selector` names what to focus, the
// library's menu by default.
export const REKA_MENU = "[data-reka-menu-content]";

export function focusOpenMenu(selector = REKA_MENU) {
	requestAnimationFrame(() => document.querySelector(selector)?.focus());
}
