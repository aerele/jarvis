// One stack for nested sheets: only the top sheet owns keys/focus and the last
// close restores the body's original scroll style.
const stack = [];
let overflow;
function controls(root) {
	return [
		...root.querySelectorAll("button, textarea, input, select, a[href], [tabindex]"),
	].filter(
		(el) =>
			!el.disabled &&
			el.tabIndex >= 0 &&
			!el.closest("[hidden], [inert]") &&
			getComputedStyle(el).display !== "none"
	);
}
function focusFirst(root) {
	(root.querySelector("[autofocus]") || controls(root)[0] || root).focus();
}
function keydown(event) {
	const top = stack[stack.length - 1];
	if (!top) return;
	if (event.key === "Escape") {
		event.preventDefault();
		event.stopImmediatePropagation();
		top.close();
	} else if (event.key === "Tab") {
		const items = controls(top.root);
		const first = items[0] || top.root;
		const last = items[items.length - 1] || top.root;
		if (
			!top.root.contains(document.activeElement) ||
			(event.shiftKey
				? document.activeElement === first
				: document.activeElement === last) ||
			document.activeElement === top.root
		) {
			event.preventDefault();
			(event.shiftKey ? last : first).focus();
		}
	}
}
function focusin(event) {
	const top = stack[stack.length - 1];
	if (top && !top.root.contains(event.target)) focusFirst(top.root);
}
export function registerSheet(root, close) {
	const entry = { root, close, opener: document.activeElement };
	if (!stack.length) {
		overflow = document.body.style.overflow;
		document.body.style.overflow = "hidden";
		document.addEventListener("keydown", keydown, true);
		document.addEventListener("focusin", focusin, true);
	}
	stack.push(entry);
	focusFirst(root);
	return () => {
		const index = stack.indexOf(entry);
		if (index < 0) return;
		const wasTop = index === stack.length - 1;
		stack.splice(index, 1);
		if (!stack.length) {
			document.body.style.overflow = overflow;
			document.removeEventListener("keydown", keydown, true);
			document.removeEventListener("focusin", focusin, true);
		}
		if (wasTop) {
			const top = stack[stack.length - 1];
			if (entry.opener?.isConnected && (!top || top.root.contains(entry.opener)))
				entry.opener.focus();
			else if (top) focusFirst(top.root);
		}
	};
}
