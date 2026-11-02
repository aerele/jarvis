import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import frappeui from "frappe-ui/vite";
import { VitePWA } from "vite-plugin-pwa";
import path from "path";
import fs from "fs";

// Walks up from `dir` to the bench root (the directory with both `sites/` and `apps/`),
// the same signal frontend/vite.config.js uses. Walking up, not counting levels, is what
// lets the build run from a git worktree nested deeper than apps/<app>/pwa.
function findBenchRoot(dir) {
	for (;;) {
		if (fs.existsSync(path.join(dir, "sites")) && fs.existsSync(path.join(dir, "apps"))) {
			return dir;
		}
		const parent = path.dirname(dir);
		if (parent === dir) return null;
		dir = parent;
	}
}

// src/socket.js imports the bench's common_site_config.json by a FIXED relative depth
// ("../../../../sites/..."), which only resolves when pwa/ sits at apps/<app>/pwa. Point
// the alias at the file findBenchRoot found. With no bench above us (a bare checkout, CI)
// fall back to the original literal, so the failure mode is unchanged: CI provides a stub
// at $HOME/sites/common_site_config.json, which is exactly where that literal lands.
function commonSiteConfigPath() {
	const benchRoot = findBenchRoot(__dirname);
	return benchRoot
		? path.join(benchRoot, "sites", "common_site_config.json")
		: path.resolve(__dirname, "../../../../sites/common_site_config.json");
}

// The holiday art is lazy and must stay out of the service worker's precache, so its chunks
// are named `holiday-*` (globIgnores below skips them and the build gate looks for the name):
// the worn item, and each welcome scene with the runtime and art helpers the scenes share.
// Only a chunk made ENTIRELY of art modules gets the name: a chunk that mixes art with app
// code must not be hidden from the precache by its name. Add a module here only when it is
// art. The words, the answer reader, the cadence and the numbers the welcome needs before
// the art arrives (scene-geometry.mjs, scene-phone.mjs) are not art: the page imports them
// statically, so they live outside holiday-* chunks, which the precached view may import.
const HOLIDAY_ART = [
	"/js/shared/holiday/worn.mjs",
	"/js/shared/holiday/stage.mjs",
	"/js/shared/holiday/scenes/", // every scene, its art, runtime, layout, motion and placement
];
function isHolidayArt(id) {
	const path = id.replaceAll("\\", "/");
	return HOLIDAY_ART.some((art) =>
		art.endsWith("/") ? path.includes(art) : path.endsWith(art)
	);
}
function holidayChunkNames(chunk) {
	const ids = (chunk.moduleIds || []).filter((id) => !id.includes("\0"));
	const artOnly = ids.length > 0 && ids.every(isHolidayArt);
	return artOnly ? "assets/holiday-[name]-[hash].js" : "assets/[name]-[hash].js";
}

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
				// holiday-* is the lazy worn-item and scene art: not precached, so a cold
				// offline open has no item or scene (and no 404 for one).
				globIgnores: ["**/index.html", "**/holiday-*"],
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
			"../../../../sites/common_site_config.json": commonSiteConfigPath(),
		},
		dedupe: ["vue"],
	},
	build: {
		rollupOptions: {
			output: { chunkFileNames: holidayChunkNames },
		},
	},
	server: {
		// @shared and @jsshared reach outside this app's root.
		fs: { allow: [".."] },
	},
	optimizeDeps: {
		include: ["frappe-ui > feather-icons", "engine.io-client"],
	},
});
