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
	var DEBOUNCE_MS = 4000;

	var sent = 0;
	var queue = [];
	var seen = {};
	var timer = null;
	var reporting = false;

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
			if (reporting || sent >= MAX_PER_SESSION) return;
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
			if (!timer) {
				timer = setTimeout(function () {
					timer = null;
					flush();
				}, DEBOUNCE_MS);
			}
		} catch (_) {
			/* a reporter that throws is worse than a missing report */
		}
	}

	function flush() {
		if (!queue.length || reporting) return;
		var batch = queue.splice(0, MAX_QUEUE);
		seen = {};
		reporting = true;
		fetch(ENDPOINT, {
			method: "POST",
			credentials: "include",
			headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": csrf() },
			body: JSON.stringify({ errors: batch }),
			keepalive: true,
		})
			.then(function (res) {
				if (res.ok) sent += batch.length;
			})
			.catch(function () {
				/* best-effort telemetry - drop on failure */
			})
			.then(function () {
				reporting = false;
			});
	}

	window.addEventListener("error", function (ev) {
		var err = ev && ev.error;
		report({
			error_class: (err && err.name) || "Error",
			error_code: "uncaught",
			message: (err && err.message) || (ev && ev.message) || "Uncaught error",
			stack: err && err.stack,
		});
	});
	window.addEventListener("unhandledrejection", function (ev) {
		var r = ev && ev.reason;
		report({
			error_class: (r && r.name) || "UnhandledRejection",
			error_code: "unhandledrejection",
			message: (r && r.message) || String(r),
			stack: r && r.stack,
		});
	});
	window.addEventListener("pagehide", flush);

	window.jarvisReportError = report;
	window.__jarvisErrorReporter = true;
})();
