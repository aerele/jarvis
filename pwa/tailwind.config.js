import frappeUIPreset from "frappe-ui/tailwind";

// The shared export card renders frappe-ui controls. Scan those controls and
// their small dependency set so their classes survive the production build.
export default {
	presets: [frappeUIPreset],
	content: [
		"./index.html",
		"./src/**/*.{vue,js}",
		"../frontend/src/components/chat/ExportCard.vue",
		"./node_modules/frappe-ui/src/components/{Button,Tooltip}/**/*.{vue,js,ts}",
		"./node_modules/frappe-ui/src/components/{Button,Tooltip,LoadingIndicator,FeatherIcon}.vue",
	],
};
