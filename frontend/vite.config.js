import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import frappeui from "frappe-ui/vite";
import path from "path";
import fs from "fs";
import { AsyncLocalStorage } from "async_hooks";

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
// csrf_token used to be deliberately excluded from this shim (Frappe has no
// clean bare-token endpoint, and a fabricated value would silently fail the
// session-bound CSRF check). devCsrfToken() below closes that gap for real,
// by fetching the actual per-session token instead of fabricating one.
function devBootFlags() {
	const FLAGS = { is_system_manager: true, is_jarvis_admin: true };
	return {
		name: "jarvis-dev-boot-flags",
		apply: "serve", // vite never invokes this hook for `vite build`
		transformIndexHtml() {
			const assignments = Object.entries(FLAGS)
				.map(
					([key, value]) =>
						`if (typeof window.${key} === "undefined") window.${key} = ${JSON.stringify(
							value
						)};`
				)
				.join("\n");
			// Returning tags (rather than a string.replace on the raw html) means
			// this can't silently no-op if index.html's markup ever changes shape.
			return [
				{
					tag: "script",
					injectTo: "body", // same spot jinjaBootData uses in the real build
					children: [
						"// DEV-ONLY SHIM (jarvis-dev-boot-flags, frontend/vite.config.js).",
						"// NOT auth: grants zero server-side privilege, every admin API",
						"// still re-checks permissions. Fills the boot flags Frappe would",
						"// inject via www/jarvis.html so admin-gated settings panes are",
						"// reachable on `vite serve`. Never present in a build.",
						assignments,
					].join("\n"),
				},
			];
		},
	};
}

// DEV-ONLY SHIM — grants no server-side privilege whatsoever.
//
// window.csrf_token is undefined on `vite serve` for the same reason the two
// boot flags above are: the real value only ever gets baked in by Frappe's
// Jinja render of jarvis/www/jarvis.html (context.boot.csrf_token =
// frappe.sessions.get_csrf_token(), see jarvis/www/jarvis.py), and vite serves
// its own frontend/index.html instead. frappe-ui's call() (utils/call.js)
// reads window.csrf_token at CALL time, not at module load, so every write
// AND every whitelisted read that isn't allow_guest gets rejected with
// frappe.exceptions.CSRFTokenError before this shim: settings panes render
// their chrome but stay empty or show an error state forever.
//
// The fix is to fetch the token for real rather than fabricate one — a fake
// value would fail the session-bound check just as loudly as no value, but
// more confusingly. So this asks the SAME backend for the SAME thing it would
// hand the browser on a normal page load: a request to this site's own
// server-rendered /jarvis, with the browser's session cookie forwarded,
// scrape the csrf_token straight out of the boot script jinjaBootData put
// there. That means this only ever succeeds for a request carrying a cookie
// for a session that already has Jarvis access (frappe.sessions.get_csrf_token
// is session-scoped, not per-page) — no new authorization boundary is opened.
//
// This has to be split across two plugin hooks because Vite's dev-mode
// transformIndexHtml hook (unlike the build-mode one) is only ever handed
// {path, filename, server, originalUrl} — no request object, so no cookies.
// configureServer() below registers a middleware (added directly, so Vite
// installs it BEFORE its own internal ones, including the one that serves
// index.html and fires transformIndexHtml) that does the fetch and stashes
// the result in an AsyncLocalStorage store for transformIndexHtml to read
// back. A plain module-level variable would do the same job on a single
// serialized request stream, but two page loads racing their backend fetches
// (e.g. two tabs opened together) could then clobber each other's token
// before the slower one's transformIndexHtml ran; AsyncLocalStorage gives
// each request its own store instead, so there is no cross-request race to
// reason about.
const csrfRequestContext = new AsyncLocalStorage();

function devCsrfToken() {
	return {
		name: "jarvis-dev-csrf-token",
		apply: "serve", // vite never invokes this hook for `vite build`
		configureServer(server) {
			server.middlewares.use((req, res, next) => {
				// Only a real document navigation (full load / hard refresh) needs
				// this: SPA route changes never hit the server, and the app's own
				// XHR/fetch calls to /api/... ask for JSON, not text/html, so this
				// never fires per API call — only once per page load.
				if (req.method !== "GET" || !(req.headers.accept || "").includes("text/html")) {
					return next();
				}
				const store = { token: null };
				csrfRequestContext.run(store, () => {
					fetchSessionCsrfToken(req, server.config.logger)
						.then((token) => {
							store.token = token;
						})
						.finally(next);
				});
			});
		},
		transformIndexHtml() {
			const token = csrfRequestContext.getStore()?.token;
			if (!token) return []; // no session cookie, no access, or the backend fetch failed — leave undefined, same as before this shim
			return [
				{
					tag: "script",
					injectTo: "body", // same spot jinjaBootData uses in the real build
					children: [
						"// DEV-ONLY SHIM (jarvis-dev-csrf-token, frontend/vite.config.js).",
						"// NOT auth: this is the real per-session token, fetched server-side",
						"// from this same backend's own /jarvis render — the backend still",
						"// authenticates the session cookie and authorizes every request",
						"// exactly as it does for a served page. Never present in a build.",
						`if (typeof window.csrf_token === "undefined") window.csrf_token = ${JSON.stringify(
							token
						)};`,
					].join("\n"),
				},
			];
		},
	};
}

// Walks up from `dir` looking for the bench root (the directory that has both
// `sites/` and `apps/` as children) — same signal frappe-ui's own vite plugin
// uses (frappe-ui/vite/utils.js findBenchPath), reimplemented here because
// that file isn't part of frappe-ui's package "exports" map. Walking up
// (rather than a fixed relative depth) is what makes this work unchanged from
// a git worktree nested arbitrarily deeper than frontend/../../..
function findBenchRoot(dir) {
	for (;;) {
		if (fs.existsSync(path.join(dir, "sites")) && fs.existsSync(path.join(dir, "apps"))) {
			return dir;
		}
		const parent = path.dirname(dir);
		if (parent === dir) return null; // reached filesystem root without finding one
		dir = parent;
	}
}

function getWebserverPort() {
	if (process.env.FRAPPE_WEB_SERVER_PORT) {
		return Number(process.env.FRAPPE_WEB_SERVER_PORT);
	}
	const benchRoot = findBenchRoot(__dirname);
	const configPath = benchRoot && path.join(benchRoot, "sites", "common_site_config.json");
	if (!configPath || !fs.existsSync(configPath)) return 8000; // matches frappeProxy()'s own fallback
	try {
		return JSON.parse(fs.readFileSync(configPath, "utf-8")).webserver_port || 8000;
	} catch {
		return 8000;
	}
}

async function fetchSessionCsrfToken(req, logger) {
	const cookie = req.headers.cookie;
	const host = (req.headers.host || "").split(":")[0];
	if (!cookie || !host) return null; // no session cookie forwarded (not logged in yet) — nothing to look up

	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), 5000);
	try {
		// Bench is Host-routed, so hit the SAME site the browser asked vite for.
		// This is a direct request to the real Frappe webserver, not through
		// vite's own /api proxy: that proxy only covers desk|app|login|api|assets
		// |files|private (see frappe-ui/vite/frappeProxy.js) — /jarvis is vite's
		// own SPA route, never proxied.
		const response = await fetch(`http://${host}:${getWebserverPort()}/jarvis`, {
			headers: { Cookie: cookie },
			// A guest or a session without Jarvis access gets a 30x from
			// jarvis/www/jarvis.py's has_jarvis_access() gate, to /app or
			// /jarvis-no-access. Treat that as "no token" rather than following
			// it — the redirect target's markup carries no csrf_token of ours.
			redirect: "manual",
			signal: controller.signal,
		});
		if (response.status !== 200) return null;
		const html = await response.text();
		// frappe-ui's jinjaBootData plugin renders context.boot.csrf_token as
		// window["csrf_token"] = <tojson>; — see jarvis/www/jarvis.py.
		const match = html.match(/window\["csrf_token"\]\s*=\s*("(?:[^"\\]|\\.)*")/);
		return match ? JSON.parse(match[1]) : null;
	} catch (error) {
		logger.warn(`[jarvis-dev-csrf-token] could not fetch a real token: ${error.message}`, {
			timestamp: true,
		});
		return null;
	} finally {
		clearTimeout(timeout);
	}
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
		devCsrfToken(),
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
