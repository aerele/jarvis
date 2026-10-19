import frappeUIPreset from "frappe-ui/tailwind";

// No frappe-ui content globs here (unlike the desktop SPA's config): the PWA
// uses frappe-ui for its request layer, not its components, so scanning them
// would only emit unused CSS.
export default {
	presets: [frappeUIPreset],
	content: ["./index.html", "./src/**/*.{vue,js}"],
};
