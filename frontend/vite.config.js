import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import frappeui from "frappe-ui/vite";
import path from "path";

// DEV-ONLY SHIM — grants no server-side privilege whatsoever.
//
// Frappe injects window.is_system_manager / window.is_jarvis_admin (and
// csrf_token, etc.) into jarvis/www/jarvis.html at render time via
// jinjaBootData (see jarvis/www/jarvis.py). `vite serve` serves its own
// frontend/index.html instead, which never goes through that Jinja render,
// so those flags are simply undefined. SettingsDialog.vue, OnboardingGate.vue,
// AiModelsPane.vue, etc. read window.is_system_manager / window.is_jarvis_admin
// at component-setup time (before any user interaction), so the ACCOUNT AND
// BILLING settings rail (Plan and billing, AI models, Connection, Billing and
// metering, Branding) never renders on the dev server without this.
//
// This plugin fills ONLY those two flags, ONLY on `vite serve` (never
// `vite build`), and ONLY when a flag isn't already set. It does not touch
// the backend, does not create a session, and does not forge a CSRF token —
// every admin API these panes call still re-checks require_jarvis_admin() /
// System Manager server-side (jarvis/permissions.py). A user who is not
// actually an admin still gets 403s from the real API calls; this only
// unlocks the client-side rail so the panes are reachable to look at.
//
// csrf_token is deliberately NOT part of this shim: it's read at API-call
// time (not at module-load time, unlike the two flags above), Frappe has no
// clean endpoint to fetch a bare token for an SPA that skipped the
// server-rendered boot, and a fabricated value would silently fail Frappe's
// session-bound CSRF check. Net effect: panes render for visual review, but
// Save/write actions through the dev server still fail — verify writes
// against a served page (e.g. http://jarvis.proxy:8002/jarvis) instead.
function devBootFlags() {
	const FLAGS = { is_system_manager: true, is_jarvis_admin: true };
	return {
		name: "jarvis-dev-boot-flags",
		apply: "serve", // vite never invokes this hook for `vite build`
		transformIndexHtml(html) {
			const assignments = Object.entries(FLAGS)
				.map(
					([key, value]) =>
						`if (typeof window.${key} === "undefined") window.${key} = ${JSON.stringify(
							value
						)};`
				)
				.join("\n\t\t\t");
			return html.replace(
				'<div id="app"></div>',
				[
					'<div id="app"></div>',
					"\t\t<!-- DEV-ONLY SHIM (jarvis-dev-boot-flags, frontend/vite.config.js).",
					"\t\t     NOT auth — grants zero server-side privilege, every admin API",
					"\t\t     still re-checks permissions. Fills the boot flags Frappe would",
					"\t\t     inject via www/jarvis.html so admin-gated settings panes are",
					"\t\t     reachable on `vite serve`. Never present in a build. -->",
					"\t\t<script>",
					`\t\t\t${assignments}`,
					"\t\t</script>",
				].join("\n")
			);
		},
	};
}

export default defineConfig({
	plugins: [
		frappeui({
			frontendRoute: "/jarvis",
			buildConfig: {
				outDir: path.resolve(__dirname, "../jarvis/public/frontend"),
				indexHtmlPath: path.resolve(__dirname, "../jarvis/www/jarvis.html"),
				baseUrl: "/assets/jarvis/frontend/",
			},
		}),
		vue(),
		devBootFlags(),
	],
	resolve: {
		alias: {
			"@": path.resolve(__dirname, "src"),
		},
		// wiki-graph-core is a self-contained file: package (own vue in its
		// node_modules); dedupe keeps a single Vue instance shared with the app.
		dedupe: ["vue"],
	},
	optimizeDeps: {
		include: ["frappe-ui > feather-icons", "showdown", "engine.io-client"],
		exclude: ["frappe-ui"],
	},
	server: {
		allowedHosts: true,
	},
});
