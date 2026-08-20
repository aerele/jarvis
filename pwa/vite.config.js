import { createRequire } from "node:module";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import frappeui from "frappe-ui/vite";
import { VitePWA } from "vite-plugin-pwa";
import path from "path";

// vite-plugin-pwa lazy-loads workbox-build through an esbuild `require` shim that
// throws under ESM on Node < 20 ("Dynamic require of workbox-build is not
// supported"), which broke `bench build` on Frappe Cloud v15 benches (Node
// 18.16.0). Provide a real CommonJS require so that lookup resolves. Build-time
// only; the browser bundle is unaffected. Paired with the workbox-build pin in
// package.json (7.0.0, whose tree stays on the Node-18-safe lru-cache line), this
// lets the mobile PWA build on Node 18 without forcing customers onto Node 20.
globalThis.require ??= createRequire(import.meta.url);

// The phone surface. Builds to jarvis/public/pwa/ and drops its shell at
// jarvis/www/jarvis_mobile.html, which Frappe serves at /jarvis-mobile (see
// website_route_rules in hooks.py). The desktop SPA under ../frontend is
// untouched and keeps /jarvis.
export default defineConfig({
	plugins: [
		frappeui({
			frontendRoute: "/jarvis-mobile",
			buildConfig: {
				outDir: path.resolve(__dirname, "../jarvis/public/pwa"),
				indexHtmlPath: path.resolve(__dirname, "../jarvis/www/jarvis_mobile.html"),
				baseUrl: "/assets/jarvis/pwa/",
			},
		}),
		vue(),
		VitePWA({
			// injectManifest (not generateSW): src/sw.js is ours, so the precache
			// list is injected into a worker we control. Same strategy as HRMS.
			strategies: "injectManifest",
			srcDir: "src",
			filename: "sw.js",
			// We register the worker by hand in main.js. It MUST be served from a
			// root-level path with an explicit narrow scope, so the plugin's own
			// auto-registration (which would point at /assets/... and inherit that
			// useless scope) is switched off. See jarvis/pwa.py.
			injectRegister: null,
			registerType: "autoUpdate",
			manifest: {
				id: "/jarvis-mobile",
				name: "Jarvis",
				short_name: "Jarvis",
				description:
					"Your AI teammate. Ask for anything across your ERP, in plain language.",
				start_url: "/jarvis-mobile",
				scope: "/jarvis-mobile",
				display: "standalone",
				orientation: "portrait",
				background_color: "#F4F4F5",
				theme_color: "#F4F4F5",
				categories: ["business", "productivity"],
				// ?v=2 cache-busts the install/home-screen icon after the blue-A →
				// spark swap (same filenames); bump alongside index.html when icons change.
				icons: [
					{
						src: "/assets/jarvis/manifest/icon-192.png?v=2",
						sizes: "192x192",
						type: "image/png",
						purpose: "any",
					},
					{
						src: "/assets/jarvis/manifest/icon-512.png?v=2",
						sizes: "512x512",
						type: "image/png",
						purpose: "any",
					},
					{
						src: "/assets/jarvis/manifest/icon-192-maskable.png?v=2",
						sizes: "192x192",
						type: "image/png",
						purpose: "maskable",
					},
					{
						src: "/assets/jarvis/manifest/icon-512-maskable.png?v=2",
						sizes: "512x512",
						type: "image/png",
						purpose: "maskable",
					},
				],
			},
			injectManifest: {
				// Assets only. index.html is deliberately NOT precached: the shell
				// Frappe serves is Jinja-rendered per request (CSRF token + boot
				// data), so a cached copy would go stale and hand the app a dead
				// token. Offline navigations fall back to offline.html instead.
				globPatterns: ["**/*.{js,css,woff,woff2,svg,png,ico}", "offline.html"],
				globIgnores: ["**/index.html"],
				// Workbox emits precache URLs relative to the worker, and resolves
				// them against wherever the worker is SERVED from. Ours is served
				// from the site root (/jarvis-mobile.sw.js — see jarvis/pwa.py), not
				// from the bundle directory, so a relative "assets/index-x.js" would
				// resolve to /assets/index-x.js and 404. Pin every entry absolute.
				modifyURLPrefix: { "": "/assets/jarvis/pwa/" },
			},
		}),
	],
	resolve: {
		alias: {
			"@": path.resolve(__dirname, "src"),
			// The markdown renderer is shared with the desktop SPA — one renderer
			// means agent replies look the same on both surfaces. It is a
			// dependency-free module, so importing across apps costs nothing.
			"@shared": path.resolve(__dirname, "../frontend/src"),
			// Surface-agnostic chat modules (the Relay-Pump event fence). They live
			// under jarvis/public/js rather than frontend/src because the Desk widget
			// is an esbuild bundle that can only reach that tree — see
			// jarvis/public/js/shared/pump_fence.mjs.
			"@jsshared": path.resolve(__dirname, "../jarvis/public/js/shared"),
		},
		dedupe: ["vue"],
	},
	server: {
		// @shared and @jsshared reach outside this app's root.
		fs: { allow: [".."] },
	},
	optimizeDeps: {
		include: ["frappe-ui > feather-icons", "engine.io-client"],
	},
});
