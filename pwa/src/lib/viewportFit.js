// Keeps a fixed, full-screen element inside what the person can see. On iOS the virtual keyboard
// shrinks the VISUAL viewport and not the layout one, so a `position: fixed; bottom: 0` footer
// ends up under the keyboard. Android Chrome (interactive-widget=resizes-content) shrinks both,
// and the numbers agree. The element gets two CSS variables, `--jv-vv-top` and `--jv-vv-height`
// (its stylesheet falls back to 0 and 100dvh when they are not set, as on a browser with no
// visualViewport). With the keyboard up (the visual viewport is much shorter than the window, with
// its bottom above the window's) the element also gets `--jv-kb-bottom: 0px`: the home-bar padding
// at the foot would be dead space between the footer and the keyboard. `win` is the window; pure
// otherwise, so node can run it with a fake one.
const KEYBOARD_MIN_PX = 120;
export function fitViewport(el, win) {
	const vv = win && win.visualViewport;
	if (!el || !vv) return null;
	const apply = () => {
		el.style.setProperty("--jv-vv-top", `${Math.max(0, Math.round(vv.offsetTop))}px`);
		el.style.setProperty("--jv-vv-height", `${Math.round(vv.height)}px`);
		const covered = (win.innerHeight || vv.height) - vv.height - vv.offsetTop;
		if (covered > KEYBOARD_MIN_PX) el.style.setProperty("--jv-kb-bottom", "0px");
		else el.style.removeProperty("--jv-kb-bottom");
	};
	apply();
	vv.addEventListener("resize", apply);
	vv.addEventListener("scroll", apply);
	return () => {
		vv.removeEventListener("resize", apply);
		vv.removeEventListener("scroll", apply);
		el.style.removeProperty("--jv-vv-top");
		el.style.removeProperty("--jv-vv-height");
		el.style.removeProperty("--jv-kb-bottom");
	};
}

// A focused field must not hide under the keyboard: scroll it to the middle of what is visible
// once the keyboard has had time to open. Only for a text-like control; a button or a link is left
// alone. `defer(fn)` is the timer (injected for tests).
const TEXT_LIKE = /^(INPUT|TEXTAREA|SELECT)$/;
export function scrollFocusedIntoView(target, defer = (fn) => setTimeout(fn, 300)) {
	if (!target || !TEXT_LIKE.test(target.tagName || "")) return false;
	defer(() => {
		if (typeof target.scrollIntoView === "function" && target.isConnected !== false)
			target.scrollIntoView({ block: "center", behavior: "smooth" });
	});
	return true;
}
