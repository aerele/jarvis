// Unit tests for the shared error reporter (SPA + PWA). node:test with mocked
// browser globals - matches the repo's other src/**/*.test.js. Covers the
// in-browser logic the builds can't: batching, POST shape, dedupe, benign
// filter, caps, offline buffering, the re-entrancy guard, and the global
// handlers.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import {
	report,
	flush,
	flushBuffered,
	configure,
	install,
	vueErrorHandler,
	_resetForTests,
} from "./errorReporter.js";

let lsStore;

function installEnv() {
	lsStore = {};
	globalThis.localStorage = {
		getItem: (k) => (k in lsStore ? lsStore[k] : null),
		setItem: (k, v) => {
			lsStore[k] = String(v);
		},
		removeItem: (k) => {
			delete lsStore[k];
		},
	};
	const handlers = {};
	globalThis.window = {
		csrf_token: "csrf-abc",
		addEventListener: (ev, fn) => {
			(handlers[ev] = handlers[ev] || []).push(fn);
		},
		__handlers: handlers,
	};
	globalThis.document = { addEventListener: () => {}, visibilityState: "visible" };
	globalThis.location = { pathname: "/c/route" };
}

/** Fetch mock recording each call's parsed body. behavior: "ok" | "fail" | "reject". */
function installFetch(behavior = "ok") {
	const calls = [];
	globalThis.fetch = (url, opts) => {
		calls.push({ url, opts, body: JSON.parse(opts.body) });
		if (behavior === "reject") return Promise.reject(new Error("network"));
		return Promise.resolve({ ok: behavior === "ok", status: behavior === "ok" ? 200 : 500 });
	};
	return calls;
}

beforeEach(() => {
	installEnv();
	_resetForTests();
});
afterEach(() => _resetForTests());

test("flush posts one batch with the right envelope + headers", async () => {
	const calls = installFetch("ok");
	report({ surface: "spa_chat", error_code: "e1", message: "boom one" });
	report({ surface: "spa_chat", error_code: "e2", message: "boom two" });
	await flush();
	assert.equal(calls.length, 1);
	assert.match(calls[0].url, /jarvis\.api_errors\.report_client_errors$/);
	assert.equal(calls[0].opts.method, "POST");
	assert.equal(calls[0].opts.headers["X-Frappe-CSRF-Token"], "csrf-abc");
	assert.equal(calls[0].opts.keepalive, true);
	assert.equal(calls[0].body.errors.length, 2);
	assert.equal(calls[0].body.errors[0].route, "/c/route");
});

test("digit-only differences dedupe into one group; different words do not", async () => {
	let calls = installFetch("ok");
	report({ surface: "s", error_code: "e", message: "failed 3 times" });
	report({ surface: "s", error_code: "e", message: "failed 9 times" });
	await flush();
	assert.equal(calls[0].body.errors.length, 1, "digit-only diff = one group");

	_resetForTests();
	calls = installFetch("ok");
	report({ surface: "s", error_code: "e", message: "disk full" });
	report({ surface: "s", error_code: "e", message: "network down" });
	await flush();
	assert.equal(calls[0].body.errors.length, 2, "different words = two groups");
});

test("benign browser noise is never sent", async () => {
	const calls = installFetch("ok");
	report({ message: "ResizeObserver loop limit exceeded" });
	report({ message: "Loading chunk 42 failed" });
	await flush();
	assert.equal(calls.length, 0, "nothing queued -> no POST");
});

test("the queue is capped at 50 per flush", async () => {
	const calls = installFetch("ok");
	for (let i = 0; i < 60; i++) {
		report({ surface: "s", error_code: "e", message: "z".repeat(i + 1) }); // 60 distinct, no digits
	}
	await flush();
	assert.equal(calls[0].body.errors.length, 50);
});

test("the per-session cap stops reporting past 100", async () => {
	const calls = installFetch("ok");
	for (let b = 0; b < 3; b++) {
		for (let i = 0; i < 50; i++) {
			report({ surface: "s", error_code: "e", message: `x${"z".repeat(i + 1)}` });
		}
		await flush(); // _seen resets per flush; _sent accumulates
	}
	const total = calls.reduce((n, c) => n + c.body.errors.length, 0);
	assert.equal(total, 100, `sent ${total}, expected the 100 cap`);
});

test("offline: a failed POST buffers to localStorage; flushBuffered resends and clears", async () => {
	configure({ offline: true });
	installFetch("reject");
	report({ surface: "s", error_code: "e", message: "offline boom" });
	await flush();
	const buf = JSON.parse(lsStore["jarvis.errorBuffer"] || "[]");
	assert.equal(buf.length, 1, "buffered on failure");

	const calls = installFetch("ok");
	await flushBuffered();
	assert.equal(calls.length, 1);
	assert.equal(calls[0].body.errors.length, 1);
	assert.equal(lsStore["jarvis.errorBuffer"], undefined, "buffer cleared after resend");
});

test("offline flushBuffered puts the batch back if the resend also fails", async () => {
	configure({ offline: true });
	installFetch("reject");
	report({ surface: "s", error_code: "e", message: "still offline" });
	await flush();
	installFetch("reject");
	await flushBuffered();
	const buf = JSON.parse(lsStore["jarvis.errorBuffer"] || "[]");
	assert.equal(buf.length, 1, "kept for the next reconnect");
});

test("a report raised WHILE a flush is in flight is dropped, not re-entrant", async () => {
	let resolveFetch;
	const bodies = [];
	globalThis.fetch = (url, opts) => {
		bodies.push(JSON.parse(opts.body));
		return new Promise((res) => {
			resolveFetch = () => res({ ok: true, status: 200 });
		});
	};
	report({ surface: "s", error_code: "e", message: "first" });
	const inflight = flush(); // _reporting = true, awaiting fetch
	await flush(); // guarded: no second fetch
	report({ surface: "s", error_code: "e", message: "second" }); // dropped by the guard
	assert.equal(bodies.length, 1, "only one fetch while in flight");
	resolveFetch();
	await inflight;
	await flush(); // queue empty (second was dropped)
	assert.equal(bodies.length, 1);
});

test("install() wires window handlers that capture uncaught errors + rejections", async () => {
	const calls = installFetch("ok");
	install({ surface: "spa" });
	const h = globalThis.window.__handlers;
	assert.ok(h.error?.length && h.unhandledrejection?.length, "handlers registered");
	h.error[0]({ error: { name: "TypeError", message: "x is undefined", stack: "at foo" } });
	h.unhandledrejection[0]({ reason: { name: "BoomError", message: "rejected", stack: "" } });
	await flush();
	const classes = calls[0].body.errors.map((e) => e.error_class);
	assert.ok(classes.includes("TypeError"));
	assert.ok(classes.includes("BoomError"));
});

test("install() is idempotent (handlers not double-registered)", () => {
	install({ surface: "spa" });
	install({ surface: "spa" });
	assert.equal(globalThis.window.__handlers.error.length, 1);
});

test("vueErrorHandler reports render errors as code 'vue'", async () => {
	const calls = installFetch("ok");
	vueErrorHandler(new TypeError("render boom"));
	await flush();
	assert.equal(calls[0].body.errors[0].error_code, "vue");
	assert.match(calls[0].body.errors[0].message, /render boom/);
});

test("report() truncates an oversized message", async () => {
	const calls = installFetch("ok");
	report({ surface: "s", error_code: "e", message: "q".repeat(5000) });
	await flush();
	assert.ok(calls[0].body.errors[0].message.length <= 2000);
});
