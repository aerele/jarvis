import { ref } from "vue";

// Light / dark / follow-the-OS. The palette is CSS variables, so switching is a
// single attribute on <html> — no re-render, no flash between screens.
//
// Applied from main.js BEFORE the app mounts: setting it after would paint one
// frame in the wrong theme, which on an OLED phone is a visible flash.
const KEY = "jarvis.theme"; // "system" | "light" | "dark"

export const theme = ref(read());

function read() {
	try {
		const v = localStorage.getItem(KEY);
		return v === "light" || v === "dark" ? v : "system";
	} catch {
		return "system";
	}
}

export function applyTheme(value = theme.value) {
	theme.value = value;
	const root = document.documentElement;
	if (value === "system") root.removeAttribute("data-theme");
	else root.setAttribute("data-theme", value);
	try {
		localStorage.setItem(KEY, value);
	} catch {
		/* private mode: the choice just won't survive the session */
	}
}
