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

test("a report raised WHILE a flush is in flight is queued and sent next, not dropped", async () => {
	let resolveFirst;
	let callN = 0;
	const bodies = [];
	globalThis.fetch = (url, opts) => {
		bodies.push(JSON.parse(opts.body));
		callN += 1;
		// Only the first POST is held open (to create the in-flight window); the
		// follow-up flush that drains the queued error resolves immediately.
		if (callN === 1) {
			return new Promise((res) => {
				resolveFirst = () => res({ ok: true, status: 200 });
			});
		}
		return Promise.resolve({ ok: true, status: 200 });
	};
	report({ surface: "s", error_code: "e", message: "first" });
	const inflight = flush(); // _reporting = true, awaiting the held-open fetch
	await flush(); // no *second concurrent* fetch (flush guards overlap)
	report({ surface: "s", error_code: "e", message: "second" }); // queued, NOT dropped
	assert.equal(bodies.length, 1, "only one concurrent fetch while in flight");
	resolveFirst();
	await inflight;
	await flush(); // drains the queued "second"
	assert.equal(bodies.length, 2, "the error raised mid-flight was captured, not lost");
	assert.equal(bodies[1].errors[0].message, "second");
});

test("flushBuffered drains the whole buffer in chunks, not just the first 50", async () => {
	configure({ offline: true });
	const buf = Array.from({ length: 80 }, (_, i) => ({ surface: "s", message: "m" + i }));
	lsStore["jarvis.errorBuffer"] = JSON.stringify(buf);
	const calls = installFetch("ok");
	await flushBuffered();
	const totalSent = calls.reduce((n, c) => n + c.body.errors.length, 0);
	assert.equal(totalSent, 80, "all 80 buffered entries resent across chunks");
	assert.equal(lsStore["jarvis.errorBuffer"], undefined, "buffer cleared once fully drained");
});

test("flushBuffered re-buffers only the unsent remainder when a chunk fails", async () => {
	configure({ offline: true });
	const buf = Array.from({ length: 80 }, (_, i) => ({ surface: "s", message: "m" + i }));
	lsStore["jarvis.errorBuffer"] = JSON.stringify(buf);
	let n = 0;
	globalThis.fetch = () => {
		n += 1; // first chunk (50) ok, second chunk (30) fails
		return Promise.resolve({ ok: n === 1, status: n === 1 ? 200 : 500 });
	};
	await flushBuffered();
	const left = JSON.parse(lsStore["jarvis.errorBuffer"] || "[]");
	assert.equal(left.length, 30, "the 50 sent are gone, the 30 unsent are kept");
	assert.equal(left[0].message, "m50", "remainder starts right after the sent chunk");
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
