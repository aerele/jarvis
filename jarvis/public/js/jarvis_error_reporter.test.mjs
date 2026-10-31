import { test, mock } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

// jarvis_error_reporter.bundle.js is a plain script (app_include_js), so it is
// require()d for its side effect: it installs window.jarvisReportError. `window`
// is aliased to globalThis as in a browser; fetch and the timers are stubbed.
globalThis.window = globalThis;
globalThis.location = { pathname: "/app/jarvis-chat" };
const handlers = {};
globalThis.addEventListener = (type, fn) => {
  handlers[type] = fn;
};

mock.timers.enable({ apis: ["setTimeout"] });

const posts = [];
let respond = () => Promise.resolve({ ok: true });
globalThis.fetch = (url, init) => {
  posts.push(JSON.parse(init.body).errors.map((e) => e.message));
  return respond();
};

createRequire(import.meta.url)("./jarvis_error_reporter.bundle.js");
const report = globalThis.jarvisReportError;

// Let the fetch promise chain settle.
const settle = () => new Promise((r) => setImmediate(r));

test("an error raised while a batch is being sent is kept and sent next", async () => {
  posts.length = 0;
  let release;
  respond = () => new Promise((r) => (release = () => r({ ok: true })));

  report({ message: "first" });
  mock.timers.tick(4000);
  assert.deepEqual(posts, [["first"]]);

  report({ message: "while sending" }); // the first POST is still out
  release();
  await settle();
  mock.timers.tick(4000);
  await settle();
  assert.deepEqual(posts, [["first"], ["while sending"]]);
  release();
  await settle();
});

test("a failed send is retried instead of dropped", async () => {
  posts.length = 0;
  let calls = 0;
  respond = () => Promise.resolve({ ok: ++calls > 1 }); // 500 once, then fine

  report({ message: "turn failed" });
  mock.timers.tick(4000);
  await settle();
  mock.timers.tick(8000);
  await settle();
  assert.deepEqual(posts, [["turn failed"], ["turn failed"]]);
});

test("a server that keeps failing is tried four times with growing delays, then dropped", async () => {
  posts.length = 0;
  respond = () => Promise.resolve({ ok: false, status: 500 });

  report({ message: "always failing" });
  mock.timers.tick(4000); // first try
  await settle();
  assert.equal(posts.length, 1);
  for (const [delay, total] of [
    [8000, 2],
    [12000, 3],
    [16000, 4],
  ]) {
    mock.timers.tick(delay - 1);
    await settle();
    assert.equal(posts.length, total - 1, "not before its delay");
    mock.timers.tick(1);
    await settle();
    assert.equal(posts.length, total);
  }
  mock.timers.tick(120000);
  await settle();
  assert.equal(posts.length, 4);
});

test("a retried error is not queued a second time while it waits", async () => {
  posts.length = 0;
  let calls = 0;
  respond = () => Promise.resolve({ ok: ++calls > 1 });

  report({ message: "retry dedupe" });
  mock.timers.tick(4000);
  await settle();
  report({ message: "retry dedupe" }); // the same error again, while the retry waits
  mock.timers.tick(8000);
  await settle();
  assert.deepEqual(posts, [["retry dedupe"], ["retry dedupe"]]);
});

test("a send that throws before the request goes out cannot wedge the reporter", async () => {
  posts.length = 0;
  const realFetch = globalThis.fetch;
  globalThis.fetch = () => {
    throw new Error("fetch is gone");
  };
  report({ message: "throws" });
  mock.timers.tick(4000);
  await settle();
  globalThis.fetch = realFetch;
  respond = () => Promise.resolve({ ok: true });
  mock.timers.tick(20000);
  await settle();
  assert.deepEqual(posts, [["throws"]]);
});

test("one POST holds at most about 60 KB; the rest follows", async () => {
  posts.length = 0;
  respond = () => Promise.resolve({ ok: true });
  for (let i = 0; i < 20; i++)
    report({
      message: String.fromCharCode(97 + i) + " " + "x".repeat(1900),
      stack: "s".repeat(4000),
    });
  mock.timers.tick(4000);
  await settle();
  assert.ok(posts[0].length < 20, "first batch is size-bounded");
  for (let i = 0; i < 6; i++) {
    mock.timers.tick(4000);
    await settle();
  }
  assert.equal(posts.flat().length, 20);
});

test("a page unload sends the queue even while another send is in flight", async () => {
  posts.length = 0;
  let release;
  respond = () => new Promise((r) => (release = () => r({ ok: true })));

  report({ message: "unload first" });
  mock.timers.tick(4000); // first POST is out and unanswered
  report({ message: "unload second" });
  handlers.pagehide();
  assert.deepEqual(posts, [["unload first"], ["unload second"]]);
  release();
  await settle();
});
