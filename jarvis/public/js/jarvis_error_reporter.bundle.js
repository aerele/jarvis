// Desk error reporter - the vanilla twin of frontend/src/lib/errorReporter.js.
// Loaded on every Desk page via app_include_js. Captures uncaught JS errors on
// the Desk-embedded Jarvis surfaces (onboarding wizard, account, chat widget)
// and forwards them, scrubbed on the bench, to the Jarvis admin. Desk XHR /
// server failures already land in Frappe's Error Log and are forwarded from
// there; this covers the browser-side gap. Exposes window.jarvisReportError()
// so the widget / onboarding page can report the errors they catch and show
// (which are not thrown, so the global handlers never see them).
(function () {
	if (window.__jarvisErrorReporter) return;

	var ENDPOINT = "/api/method/jarvis.api_errors.report_client_errors";
	var MAX_PER_SESSION = 100;
	var MAX_QUEUE = 50;
	// A POST this large still fits a proxy's body limit; the rest waits its turn.
	var MAX_BATCH_BYTES = 60000;
	var FETCH_TIMEOUT_MS = 15000;
	var DEBOUNCE_MS = 4000;

	var sent = 0;
	var queue = [];
	var seen = {};
	var timer = null;
	var reporting = false;
	var failures = 0;
	var MAX_RETRIES = 3;

	var BENIGN = [
		/ResizeObserver loop/i,
		/Failed to fetch dynamically imported module/i,
		/Loading chunk \d+ failed/i,
	];

	function benign(t) {
		if (!t) return false;
		for (var i = 0; i < BENIGN.length; i++) if (BENIGN[i].test(t)) return true;
		return false;
	}

	function fingerprint(e) {
		var msg = (e.message || "").replace(/\d+/g, "#").slice(0, 200);
		return (e.error_code || "") + "|" + (e.error_class || "") + "|desk|" + msg;
	}

	// This bundle loads on EVERY Desk page (app_include_js), but must only forward
	// JARVIS errors - an ERPNext / HRMS / client-script error on a plain Desk form
	// must never cross to the control plane. An error is Jarvis-origin if it came
	// from a Jarvis asset (the chat widget, injected everywhere, always shows one
	// in its stack) or fired on a full-page Jarvis Desk surface. Explicit
	// window.jarvisReportError() calls skip this gate - they are Jarvis by
	// construction (only Jarvis code calls it).
	function isJarvisOrigin(ev, err) {
		var file = (ev && ev.filename) || "";
		var stack = (err && err.stack) || "";
		if (/\/assets\/jarvis\//.test(file) || /\/assets\/jarvis\//.test(stack)) return true;
		var path = (location && location.pathname) || "";
		return /jarvis/i.test(path);
	}

	function csrf() {
		try {
			return (window.frappe && frappe.csrf_token) || window.csrf_token || "";
		} catch (_) {
			return "";
		}
	}

	function report(ctx) {
		try {
			ctx = ctx || {};
			// Not gated on an in-flight flush: report() only queues, and gating
			// dropped every error raised while a slow POST was out. `sent` counts
			// attempts, so a failing server cannot be hit without bound.
			if (sent >= MAX_PER_SESSION) return;
			var message = String(ctx.message || "").slice(0, 2000);
			if (benign(message) || benign(ctx.stack)) return;
			var e = {
				surface: ctx.surface || "desk",
				route: ctx.route || (location ? location.pathname : ""),
				error_code: String(ctx.error_code || "").slice(0, 64),
				error_class: String(ctx.error_class || ctx.name || "").slice(0, 140),
				message: message,
				stack: String(ctx.stack || "").slice(0, 4000),
				conversation: String(ctx.conversation || "").slice(0, 140),
				run_id: String(ctx.run_id || "").slice(0, 140),
			};
			var fp = fingerprint(e);
			if (seen[fp] || queue.length >= MAX_QUEUE) return;
			seen[fp] = 1;
			queue.push(e);
			schedule(DEBOUNCE_MS);
		} catch (_) {
			/* a reporter that throws is worse than a missing report */
		}
	}

	function schedule(ms) {
		if (timer) return;
		timer = setTimeout(function () {
			timer = null;
			flush();
		}, ms);
	}

	// Take the next batch off the queue: at most MAX_QUEUE errors and about
	// MAX_BATCH_BYTES serialised (always at least one, so nothing is stuck).
	function takeBatch() {
		var batch = [];
		var bytes = 0;
		while (queue.length && batch.length < MAX_QUEUE) {
			var size = JSON.stringify(queue[0]).length;
			if (batch.length && bytes + size > MAX_BATCH_BYTES) break;
			bytes += size;
			batch.push(queue.shift());
		}
		return batch;
	}

	// force: a page unload sends whatever is queued even while another send is out
	// (keepalive lets it outlive the page).
	function flush(force) {
		if (!queue.length || (reporting && force !== true)) return;
		var batch = [];
		var timeout = null;
		var own = force !== true;
		try {
			batch = takeBatch();
			seen = {};
			if (own) reporting = true;
			sent += batch.length;
			var ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
			if (ctl)
				timeout = setTimeout(function () {
					ctl.abort();
				}, FETCH_TIMEOUT_MS);
			fetch(ENDPOINT, {
				method: "POST",
				credentials: "include",
				headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": csrf() },
				body: JSON.stringify({ errors: batch }),
				keepalive: true,
				signal: ctl ? ctl.signal : undefined,
			})
				.then(function (res) {
					if (!res.ok) throw new Error("report failed " + res.status);
					failures = 0;
				})
				.catch(function () {
					retry(batch);
				})
				.then(function () {
					if (timeout) clearTimeout(timeout);
					if (own) reporting = false;
					// Errors that arrived while this batch was out, or a retry.
					if (queue.length)
						schedule(failures ? DEBOUNCE_MS * (failures + 1) : DEBOUNCE_MS);
				});
		} catch (_) {
			// Setup failed before a request went out: never leave `reporting` set.
			if (timeout) clearTimeout(timeout);
			if (own) reporting = false;
			retry(batch);
			if (queue.length) schedule(DEBOUNCE_MS * (failures + 1));
		}
	}

	// A dropped connection or a server hiccup: keep the batch for a few more
	// tries, ahead of anything queued since, then give up on it.
	function retry(batch) {
		failures += 1;
		if (failures > MAX_RETRIES) {
			failures = 0;
			return;
		}
		for (var i = 0; i < batch.length; i++) seen[fingerprint(batch[i])] = 1;
		queue = batch.concat(queue).slice(0, MAX_QUEUE);
	}

	window.addEventListener("error", function (ev) {
		var err = ev && ev.error;
		if (!isJarvisOrigin(ev, err)) return; // scope: Jarvis Desk surfaces only
		report({
			error_class: (err && err.name) || "Error",
			error_code: "uncaught",
			message: (err && err.message) || (ev && ev.message) || "Uncaught error",
			stack: err && err.stack,
		});
	});
	window.addEventListener("unhandledrejection", function (ev) {
		var r = ev && ev.reason;
		if (!isJarvisOrigin(null, r)) return; // scope: Jarvis Desk surfaces only
		report({
			error_class: (r && r.name) || "UnhandledRejection",
			error_code: "unhandledrejection",
			message: (r && r.message) || String(r),
			stack: r && r.stack,
		});
	});
	window.addEventListener("pagehide", function () {
		flush(true);
	});

	window.jarvisReportError = report;
	window.__jarvisErrorReporter = true;
})();
