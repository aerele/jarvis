// Shared Jarvis theme: the palette CSS variables + a composable that tracks
// the per-user theme choice (Frappe User.desk_theme, seeded from
// window.jarvis_desk_theme) and the OS color scheme.
//
// v3 (DESIGN-V3 §2.5 / D34): state is a MODULE-SCOPE singleton so the shell
// (UserMenu toggle), ChatView (header toggle + settings Appearance tab) and
// the feature pages all share one live theme. applyTheme() additionally
// bridges to frappe-ui by setting `data-theme` on <html> (the preset's
// darkMode selector) and `color-scheme` on :root, so frappe-ui components and
// the jv-* chat surface flip together.
import { ref, computed, watch } from "vue";

export const LIGHT_VARS = {
	"--surface": "#ffffff",
	"--surface-1": "#f7f7f8",
	"--surface-2": "#f1f1f3",
	"--surface-3": "#ececef",
	"--border": "#e8e8ec",
	"--border-2": "#dfdfe4",
	"--text": "#171717",
	"--text-2": "#4a4a4f",
	"--text-3": "#6d6d76",
	"--cta": "#1c1c22",
	"--cta-fg": "#ffffff",
	"--cta-bg": "#f1f1f4",
	"--cta-bd": "#e2e2e8",
	"--link": "#1579d0",
	"--green": "#16a34a",
	"--green-bg": "#edf8f0",
	"--green-bd": "#cdeed8",
	"--red": "#dc2626",
	"--red-bg": "#fdf0ef",
	"--red-bd": "#f5d4d1",
	"--amber": "#d97706",
	"--amber-bg": "#fdf6ec",
	"--amber-bd": "#f3e2c2",
};
// Dark: neutral charcoal surfaces.
//
// --cta is the near-black CTA fill and it INVERTS here (near-white fill, dark
// foreground). It is NOT an accent and NOT a blue — it was called `--blue` until
// #294 repointed it from indigo to near-black, and every consumer that kept
// reading it as a blue (links, gradient stops, icon strokes) silently broke.
// ALWAYS pair it: `background: var(--cta); color: var(--cta-fg)`. Pairing --cta
// with a hard-coded #fff is white-on-near-white here.
// The one sanctioned blue is --link.
export const DARK_VARS = {
	"--surface": "#16161a",
	"--surface-1": "#1d1d22",
	"--surface-2": "#26262d",
	"--surface-3": "#30303a",
	"--border": "#2c2c34",
	"--border-2": "#3a3a45",
	"--text": "#ededf2",
	"--text-2": "#b6b6c0",
	"--text-3": "#7e7e8a",
	"--cta": "#ececf0",
	"--cta-fg": "#1c1c22",
	"--cta-bg": "#26262d",
	"--cta-bd": "#3a3a45",
	"--link": "#6e8bff",
	"--green": "#34d399",
	"--green-bg": "#15271f",
	"--green-bd": "#214b38",
	"--red": "#f87171",
	"--red-bg": "#2a1818",
	"--red-bd": "#4a2a2a",
	"--amber": "#fbbf24",
	"--amber-bg": "#2a2315",
	"--amber-bd": "#4a3d1f",
};

/** Returns true when the effective theme should be dark. (Kept for the
 * pre-v3 useTheme composable that Account/Onboarding/Monitor still use.) */
export function isDark(pref, prefersDark) {
	return pref === "dark" || (pref === "system" && prefersDark);
}

// ---- module-scope singleton state ------------------------------------------
const _DESK_TO_PREF = { Light: "light", Dark: "dark", Automatic: "system" };
let _stored = "system";
try {
	_stored = _DESK_TO_PREF[window.jarvis_desk_theme] || "system";
} catch (e) {
	/* boot missing → system */
}
const theme = ref(_stored);
const prefersDark = ref(false);

const effectiveDark = computed(
	() => theme.value === "dark" || (theme.value === "system" && prefersDark.value)
);
const paletteVars = computed(() => (effectiveDark.value ? DARK_VARS : LIGHT_VARS));

function applyTheme() {
	if (typeof document === "undefined") return;
	const dark = effectiveDark.value;
	document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
	document.documentElement.style.colorScheme = dark ? "dark" : "light";
}

let _started = false;
function _start() {
	if (_started || typeof window === "undefined") return;
	_started = true;
	const mq = window.matchMedia("(prefers-color-scheme: dark)");
	prefersDark.value = mq.matches;
	mq.addEventListener("change", (e) => {
		prefersDark.value = e.matches;
	});
	// Singleton watcher (never stopped - lives for the page's lifetime).
	watch(effectiveDark, applyTheme);
	applyTheme();
}

function setTheme(t) {
	theme.value = t;
	applyTheme();
	// Anti-FOUC cache: frontend/index.html reads this synchronously, before Vue
	// mounts, to paint the right theme on first frame. desk_theme (below) stays
	// the cross-device source of truth; this is just the local, pre-mount copy.
	try {
		localStorage.setItem("jarvis-theme", t);
	} catch (e) {
		/* private mode */
	}
	// Persist to Frappe's native per-user desk_theme (fire-and-forget; the
	// in-memory value already drives the UI, so a failed write just isn't saved).
	import("@/api")
		.then(({ setUserTheme }) => setUserTheme(t))
		.catch(() => {});
}
// The toggle is a strict two-state flip of the EFFECTIVE appearance. It used
// to cycle light → dark → system, but with a dark OS the dark → system step
// rendered identically, so that click looked dead and reaching light took two
// clicks. "system" also was not truly kept reachable by the cycle (the skip
// that fixed the dead click made it unreachable on a dark OS anyway, and no
// other UI offers it since the Appearance pane was removed), so the cycle was
// dropped rather than half-kept: "system" remains the default for users who
// never touch the toggle, and a deliberate three-way choice belongs in a
// Settings picker if it ever comes back. Exported pure so theme.test.js can
// pin the every-click-flips invariant.
export function nextTheme(current, prefersDark) {
	return isDark(current, prefersDark) ? "light" : "dark";
}
function toggleTheme() {
	setTheme(nextTheme(theme.value, prefersDark.value));
}

export function useJarvisTheme() {
	_start();
	return { theme, effectiveDark, paletteVars, setTheme, toggleTheme };
}
