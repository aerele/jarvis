/**
 * Jarvis PWA service worker (injectManifest source — vite-plugin-pwa injects
 * the precache list into self.__WB_MANIFEST at build).
 *
 * Scope note, and the reason this file is served from jarvis/pwa.py rather than
 * straight out of /assets: a worker's default scope is the directory it is
 * served from. Shipped as /assets/jarvis/pwa/sw.js it could only ever control
 * /assets/jarvis/pwa/ — never the app itself at /jarvis-mobile — which is the
 * trap the Frappe HR PWA falls into (its worker exists for push, and never
 * controls the app page). Frappe refuses to serve .js out of www/, so the app
 * exposes this file at the root-level path /jarvis-mobile.sw.js and main.js
 * registers it with an explicit {scope: "/jarvis-mobile"}: a script served from
 * / may claim any narrower scope, so the worker controls exactly the app and
 * nothing else — never the Desk.
 */
import { precacheAndRoute, cleanupOutdatedCaches, matchPrecache } from "workbox-precaching";
import { clientsClaim } from "workbox-core";
import { NavigationRoute, registerRoute, setCatchHandler } from "workbox-routing";
import { NetworkOnly } from "workbox-strategies";

const BASE = "/assets/jarvis/pwa/";
const OFFLINE_URL = `${BASE}offline.html`;

// Every precache URL must be ABSOLUTE. Workbox resolves relative entries against
// the worker's own location, and this worker is served from the site root
// (/jarvis-mobile.sw.js — see jarvis/pwa.py), not from the bundle directory, so
// a relative "foo.js" would resolve to /foo.js and 404.
//
// vite.config.js pins the globbed entries with modifyURLPrefix, but that is not
// enough on its own: vite-plugin-pwa APPENDS its own manifest.webmanifest entry
// to the list AFTER those transforms run, so that one entry stays relative. A
// single 404 rejects the install event, Chrome discards the registration, and
// the app silently ends up with no service worker at all — which is exactly what
// happened ("bad-precaching-response: /manifest.webmanifest, status 404").
// Normalising here makes the worker correct regardless of plugin ordering.
const precacheManifest = (self.__WB_MANIFEST || []).map((entry) => {
	const e = typeof entry === "string" ? { url: entry, revision: null } : entry;
	return e.url.startsWith("/") ? e : { ...e, url: BASE + e.url.replace(/^\.?\//, "") };
});

// JS/CSS/icons + the offline card. index.html is excluded at build (see
// vite.config.js): the real shell is Jinja-rendered per request.
precacheAndRoute(precacheManifest);
cleanupOutdatedCaches();

// Navigations always go to the network — the shell carries a fresh CSRF token
// and boot data, so serving a cached copy would hand the app a dead token.
// When the network is gone, setCatchHandler below answers with the offline card.
registerRoute(new NavigationRoute(new NetworkOnly(), { allowlist: [/^\/jarvis-mobile/] }));

// Anything the routes above threw on. A failed navigation gets the offline card
// (this is what makes the app openable from the home screen with no network);
// everything else fails as it normally would. Chat/auth calls to /api are not
// routed at all, so they never touch the cache.
setCatchHandler(async ({ request }) => {
	if (request.mode === "navigate") {
		const offline = await matchPrecache(OFFLINE_URL);
		if (offline) return offline;
	}
	return Response.error();
});

// registerType: "autoUpdate" — a new build takes over on next load instead of
// leaving the user on a stale bundle behind a "reload?" prompt.
self.skipWaiting();
clientsClaim();
