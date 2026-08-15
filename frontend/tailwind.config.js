import frappeUIPreset from "frappe-ui/tailwind";
import { frappeUIContentGlobs } from "./scripts/frappe-ui-content.mjs";

export default {
	presets: [frappeUIPreset],
	content: [
		"./index.html",
		"./src/**/*.{vue,js,ts,jsx,tsx}",
		// Only the frappe-ui sources this app actually renders (import
		// closure computed on every build — see scripts/frappe-ui-content.mjs).
		// Globbing all of frappe-ui/src (the ecosystem default) emits ~2.6 MB
		// of CSS, ~99% unused.
		...frappeUIContentGlobs(),
	],
	// Safety net for important text/bg classes that might not appear
	// verbatim in scanned sources. Narrowed from /!(text|bg)-/ (CRM
	// boilerplate), which force-generated ~36k rules / ~2.4 MB (91% of the
	// bundle) across the whole palette + hover/active variants. Audit
	// (app src, frappe-ui import closure, backend py/json) found no runtime
	// construction of !text-*/!bg-* at all — every use is a static literal
	// in files Tailwind already scans — and only the ink/surface families
	// are used. Widen the pattern again if a feature starts composing
	// !text-*/!bg-* class names at runtime.
	safelist: [{ pattern: /!(text-ink|bg-surface)-/, variants: ["hover", "active"] }],
};
