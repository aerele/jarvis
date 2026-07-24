// Named render themes for dashboard canvases. The shell (buildSrcdoc) injects
// the selected theme as CSS custom properties + a base stylesheet of named
// component classes, wrapped in a winning `@layer theme` (the author's own
// <style> is wrapped in `@layer author`, declared first, so the theme wins the
// CSS-vs-CSS battle regardless of author source order or specificity). The SAME
// generated HTML re-skins when the user switches themes: the skill instructs the
// agent to compose the jd-* classes and color everything with the --jd-*
// variables / window.JARVIS_THEME.palette instead of hardcoding.
//
// The machine-enforceable copy of each theme's tokens/palette/scales + the
// save-time validator's allow-lists lives at jarvis/dashboards/themes/<key>/
// theme.json (the render_tokens there mirror the --jd-* VARS emits below,
// byte-for-byte — a sync-guard test fails loudly on drift).
//
// Keys are lowercase (API/theme picker); the DocType stores the capitalized
// label ("Jarvis" | "Insight" | "Claude" | "Graphite" | "Custom") — use
// themeKey()/themeLabel() to convert. "Jarvis" is the product default: the
// app's own design language (gray-on-white, ink-gray scale, hairlines,
// near-black accent) so an unprompted dashboard looks native. "Custom" is the
// deliberate bespoke escape hatch: no winning theme layer, so the author's own
// design wins (the validator relaxes the design rules but keeps the safety bans).
// For a standard theme the agent only deviates when the user picks Custom.

const VARS = (v) => `:root{
--jd-bg:${v.bg};--jd-surface:${v.surface};--jd-surface-2:${v.surface2};--jd-ink:${v.ink};--jd-heading:${v.heading || v.ink};--jd-muted:${v.muted};
--jd-line:${v.line};--jd-accent:${v.accent};
--jd-positive:${v.positive};--jd-negative:${v.negative};--jd-warning:${v.warning};--jd-info:${v.info};
--jd-radius:10px;--jd-shadow:${v.shadow};
--jd-font:${v.font};--jd-font-display:${v.fontDisplay};
}`

// Base rules: the original element defaults (so an under-styled document looks
// decent) PLUS the named component classes (jd-page/-header, jd-kpi*, jd-card/
// -chart-card, jd-table, jd-empty/-error) the skill composes so structure — not
// just color — stays standard. All color/font/type come from --jd-* tokens.
// Emitted inside `@layer theme` by buildSrcdoc, so these win over author CSS.
const BASE = `
html,body{background:var(--jd-bg);color:var(--jd-ink);font-family:var(--jd-font);margin:0;}
h1,h2,h3{font-family:var(--jd-font-display);color:var(--jd-heading,var(--jd-ink));}
table{border-collapse:collapse;width:100%;}
th{color:var(--jd-muted);font-weight:600;text-align:left;}
th,td{padding:8px 12px;border-bottom:1px solid var(--jd-line);}
section.slide{background:var(--jd-bg);}
.jd-page{max-width:1200px;margin:0 auto;padding:24px;}
.jd-page-header{margin-bottom:24px;}
.jd-page-header h1{font-size:24px;font-weight:700;line-height:1.2;letter-spacing:-.01em;margin:0 0 4px;}
.jd-subtitle{color:var(--jd-muted);font-size:13px;}
.jd-kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px;}
.jd-kpi{background:var(--jd-surface);border:1px solid var(--jd-line);border-radius:var(--jd-radius);padding:16px;box-shadow:var(--jd-shadow);}
.jd-kpi-label{color:var(--jd-muted);font-size:13px;font-weight:600;letter-spacing:.01em;margin-bottom:8px;}
.jd-kpi-value{color:var(--jd-heading);font-family:var(--jd-font-display);font-size:32px;font-weight:700;line-height:1.15;}
.jd-kpi-delta{font-size:13px;font-weight:600;}
.jd-kpi-delta.up{color:var(--jd-positive);}
.jd-kpi-delta.down{color:var(--jd-negative);}
.jd-card,.jd-chart-card{background:var(--jd-surface);border:1px solid var(--jd-line);border-radius:var(--jd-radius);padding:16px;box-shadow:var(--jd-shadow);}
.jd-card-header{font-family:var(--jd-font-display);color:var(--jd-heading);font-size:18px;font-weight:600;margin-bottom:12px;}
.jd-card-footnote{color:var(--jd-muted);font-size:12px;margin-top:8px;}
.jd-chart-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;}
.jd-table{border-collapse:collapse;width:100%;font-size:14px;}
.jd-table th{color:var(--jd-muted);font-weight:600;text-align:left;background:var(--jd-surface-2);}
.jd-table th,.jd-table td{padding:8px 12px;border-bottom:1px solid var(--jd-line);}
.jd-empty{color:var(--jd-muted);font-size:13px;text-align:center;padding:24px;}
.jd-error{color:var(--jd-negative);font-size:13px;text-align:center;padding:24px;}
`

// System stack only: the sandbox's CSP is `font-src data:` with no network, so
// a webfont (Inter) can't load inside the canvas - claiming it would silently
// fall back anyway. The system sans matches design.md's own fallback chain.
const SANS = `-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif`

export const THEMES = {
	jarvis: {
		key: "jarvis",
		label: "Jarvis",
		dark: false,
		accent: "#383838",
		// Gray-anchored, muted series colors — hue stays informational, the
		// near-black primary matches the app's solid-CTA gray (design.md §2.1).
		palette: ["#383838", "#5d78d1", "#4c9a7a", "#d9a53f", "#c25d5d", "#8a7bb8", "#5a99ae", "#a0693f"],
		css:
			VARS({
				bg: "#f8f8f8", surface: "#ffffff", surface2: "#f2f2f2", ink: "#383838",
				heading: "#171717", muted: "#7c7c7c", line: "#ededed", accent: "#383838",
				positive: "#4c9a7a", negative: "#c25d5d", warning: "#d9a53f", info: "#5d78d1",
				shadow: "0 1px 2px rgba(0,0,0,.05)",
				font: SANS, fontDisplay: SANS,
			}) + BASE,
	},
	insight: {
		key: "insight",
		label: "Insight",
		dark: false,
		accent: "#2490ef",
		palette: ["#2490ef", "#25b885", "#f5a623", "#e24c4c", "#9b59b6", "#17a2b8", "#fd7e14", "#5856d6"],
		css:
			VARS({
				bg: "#f3f3f3", surface: "#ffffff", surface2: "#f6f8fa", ink: "#1f272e",
				heading: "#1f272e", muted: "#6c7680", line: "#e2e2e2", accent: "#2490ef",
				positive: "#25b885", negative: "#e24c4c", warning: "#f5a623", info: "#2490ef",
				shadow: "0 1px 2px rgba(25,39,52,.06)",
				font: SANS, fontDisplay: SANS,
			}) + BASE,
	},
	claude: {
		key: "claude",
		label: "Claude",
		dark: false,
		accent: "#d97757",
		palette: ["#d97757", "#7d9b76", "#dfa32e", "#6a9bcc", "#b0603f", "#8a7bb8", "#5c8a72", "#c2542e"],
		css:
			VARS({
				bg: "#faf9f5", surface: "#ffffff", surface2: "#f5f3ec", ink: "#3d3929",
				heading: "#3d3929", muted: "#83827d", line: "#e8e6dc", accent: "#d97757",
				positive: "#7d9b76", negative: "#c2542e", warning: "#dfa32e", info: "#6a9bcc",
				shadow: "0 1px 2px rgba(61,57,41,.06)",
				font: SANS, fontDisplay: `Georgia,"Times New Roman",serif`,
			}) + BASE,
	},
	graphite: {
		key: "graphite",
		label: "Graphite",
		dark: true,
		accent: "#8ab4f8",
		palette: ["#8ab4f8", "#4ade80", "#fbbf24", "#f87171", "#c084fc", "#22d3ee", "#fb923c", "#a3e635"],
		css:
			VARS({
				bg: "#191919", surface: "#222222", surface2: "#2a2a2a", ink: "#ececec",
				heading: "#ececec", muted: "#9a9a9a", line: "#333333", accent: "#8ab4f8",
				positive: "#4ade80", negative: "#f87171", warning: "#fbbf24", info: "#8ab4f8",
				shadow: "none",
				font: SANS, fontDisplay: SANS,
			}) + BASE,
	},
	custom: {
		key: "custom",
		label: "Custom",
		dark: false,
		// Bespoke escape hatch: buildSrcdoc injects the tokens + component classes
		// UNLAYERED (no winning @layer) and leaves the author's <style> unlayered
		// too, so the author's own design wins by source order — while a dashboard
		// that WAS built with the jd-* classes keeps them as defaults instead of
		// rendering unstyled. The save-time validator relaxes the design rules for
		// this theme but keeps the safety bans (no external URLs, no @font-face)
		// absolute. Not synced against a theme.json — it has no design spec.
		bespoke: true,
		accent: "#383838",
		palette: ["#383838", "#5d78d1", "#4c9a7a", "#d9a53f", "#c25d5d", "#8a7bb8", "#5a99ae", "#a0693f"],
		css:
			VARS({
				bg: "#f8f8f8", surface: "#ffffff", surface2: "#f2f2f2", ink: "#383838",
				heading: "#171717", muted: "#7c7c7c", line: "#ededed", accent: "#383838",
				positive: "#4c9a7a", negative: "#c25d5d", warning: "#d9a53f", info: "#5d78d1",
				shadow: "0 1px 2px rgba(0,0,0,.05)",
				font: SANS, fontDisplay: SANS,
			}) + BASE,
	},
}

export const DEFAULT_THEME = "jarvis"

export const THEME_OPTIONS = Object.values(THEMES).map((t) => ({
	key: t.key,
	label: t.label,
}))

// DocType stores the capitalized label; the SPA works in lowercase keys.
export function themeKey(label) {
	const k = String(label || "").toLowerCase()
	return THEMES[k] ? k : DEFAULT_THEME
}

export function themeLabel(key) {
	return (THEMES[themeKey(key)] || THEMES[DEFAULT_THEME]).label
}
