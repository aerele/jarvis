/**
 * Shared, framework-agnostic error reporter for the Jarvis SPA and PWA (imported
 * by the PWA via the `@shared` alias, so there is exactly one copy). It batches,
 * dedupes and bounds errors, then POSTs them to the customer bench, which scrubs
 * and forwards them to the Jarvis admin.
 *
 * Two entry points:
 *   - install()          - global window/Vue handlers for UNCAUGHT errors.
 *   - report(context)    - explicit, CLASSIFIED capture at the chokepoints where
 *                          the app already surfaces an error to the user (chat
 *                          run:error, payment failures, ...) - these are not
 *                          thrown, so the global handlers can't see them.
 *
 * It never throws and never reports its own failures (a re-entrancy guard stops
 * an error during a flush from queueing another error - the classic loop).
 */

const ENDPOINT = "/api/method/jarvis.api_errors.report_client_errors";
const BUFFER_KEY = "jarvis.errorBuffer";

const MAX_PER_SESSION = 100; // stop after this many, so one broken build can't spam
const MAX_QUEUE = 50;
const FLUSH_DEBOUNCE_MS = 4000;

let _surface = "spa";
let _offline = false; // PWA sets true: buffer to localStorage when the POST fails
let _installed = false;
let _reporting = false; // true while a flush is in flight - suppress re-entry
let _sent = 0;
let _queue = [];
let _seen = new Set(); // client fingerprints already queued in this window
let _timer = null;

const _BENIGN = [
	/ResizeObserver loop/i,
	// Stale-chunk load failures are already recovered by the router's reload.
	/Failed to fetch dynamically imported module/i,
	/Importing a module script failed/i,
	/Loading chunk \d+ failed/i,
];

export function configure(opts = {}) {
	if (opts.surface) _surface = opts.surface;
	if (typeof opts.offline === "boolean") _offline = opts.offline;
}

function _csrf() {
	try {
		return window.csrf_token || (window.frappe && window.frappe.csrf_token) || "";
	} catch {
		return "";
	}
}

function _fingerprint(e) {
	const msg = (e.message || "").replace(/\d+/g, "#").slice(0, 200);
	return `${e.error_code || ""}|${e.error_class || ""}|${e.surface}|${msg}`;
}

function _isBenign(text) {
	return !!text && _BENIGN.some((re) => re.test(text));
}

/** Explicit, classified report. Safe to call from anywhere; cheap and silent. */
export function report(ctx = {}) {
	try {
		if (_reporting || _sent >= MAX_PER_SESSION) return;
		const message = String(ctx.message || "").slice(0, 2000);
		if (_isBenign(message) || _isBenign(ctx.stack)) return;
		const e = {
			surface: ctx.surface || _surface,
			route: ctx.route || (typeof location !== "undefined" ? location.pathname : ""),
			error_code: String(ctx.error_code || "").slice(0, 64),
			error_class: String(ctx.error_class || ctx.name || "").slice(0, 140),
			message,
			stack: String(ctx.stack || "").slice(0, 4000),
			conversation: String(ctx.conversation || "").slice(0, 140),
			run_id: String(ctx.run_id || "").slice(0, 140),
		};
		const fp = _fingerprint(e);
		if (_seen.has(fp) || _queue.length >= MAX_QUEUE) return;
		_seen.add(fp);
		_queue.push(e);
		_schedule();
	} catch {
		// A reporter that throws is worse than a missing report.
	}
}

function _schedule() {
	if (_timer) return;
	_timer = setTimeout(() => {
		_timer = null;
		void flush();
	}, FLUSH_DEBOUNCE_MS);
}

async function _post(batch) {
	const res = await fetch(ENDPOINT, {
		method: "POST",
		credentials: "include",
		headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": _csrf() },
		body: JSON.stringify({ errors: batch }),
		keepalive: true,
	});
	if (!res.ok) throw new Error("report failed " + res.status);
}

export async function flush() {
	if (!_queue.length || _reporting) return;
	const batch = _queue.splice(0, MAX_QUEUE);
	_seen = new Set();
	_reporting = true;
	try {
		await _post(batch);
		_sent += batch.length;
	} catch {
		if (_offline) _buffer(batch);
		// else: drop. Errors are best-effort telemetry, never worth retrying hard.
	} finally {
		_reporting = false;
	}
}

function _buffer(batch) {
	try {
		const prev = JSON.parse(localStorage.getItem(BUFFER_KEY) || "[]");
		localStorage.setItem(BUFFER_KEY, JSON.stringify(prev.concat(batch).slice(-100)));
	} catch {
		// no localStorage / quota - nothing else we can do.
	}
}

/** PWA: resend anything buffered while offline. Call on reconnect. */
export async function flushBuffered() {
	if (!_offline || _reporting) return;
	let buf;
	try {
		buf = JSON.parse(localStorage.getItem(BUFFER_KEY) || "[]");
	} catch {
		buf = [];
	}
	if (!buf.length) return;
	localStorage.removeItem(BUFFER_KEY);
	_reporting = true;
	try {
		await _post(buf.slice(0, MAX_QUEUE));
	} catch {
		_buffer(buf); // put it back for next time
	} finally {
		_reporting = false;
	}
}

/** Vue app.config.errorHandler - render/lifecycle errors Vue swallows. */
export function vueErrorHandler(err) {
	report({
		error_class: (err && err.name) || "VueError",
		error_code: "vue",
		message: (err && err.message) || String(err),
		stack: err && err.stack,
	});
}

/** Test-only: reset module state so unit tests isolate. Not used at runtime. */
export function _resetForTests() {
	_surface = "spa";
	_offline = false;
	_installed = false;
	_reporting = false;
	_sent = 0;
	_queue = [];
	_seen = new Set();
	if (_timer) {
		clearTimeout(_timer);
		_timer = null;
	}
}

/** Install the global uncaught-error handlers. Idempotent. */
export function install(opts = {}) {
	configure(opts);
	if (_installed || typeof window === "undefined") return;
	_installed = true;

	window.addEventListener("error", (ev) => {
		const err = ev && ev.error;
		report({
			error_class: (err && err.name) || "Error",
			error_code: "uncaught",
			message: (err && err.message) || (ev && ev.message) || "Uncaught error",
			stack: err && err.stack,
		});
	});
	window.addEventListener("unhandledrejection", (ev) => {
		const r = ev && ev.reason;
		report({
			error_class: (r && r.name) || "UnhandledRejection",
			error_code: "unhandledrejection",
			message: (r && r.message) || String(r),
			stack: r && r.stack,
		});
	});
	// Best-effort flush before the tab goes away.
	window.addEventListener("pagehide", () => void flush());
	if (typeof document !== "undefined" && document.addEventListener) {
		document.addEventListener("visibilitychange", () => {
			if (document.visibilityState === "hidden") void flush();
		});
	}
}
