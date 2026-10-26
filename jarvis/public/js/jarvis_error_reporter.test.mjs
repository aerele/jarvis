import { test, mock } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

// jarvis_error_reporter.bundle.js is a plain script (app_include_js), so it is
// require()d for its side effect: it installs window.jarvisReportError. `window`
// is aliased to globalThis as in a browser; fetch and the timers are stubbed.
globalThis.window = globalThis;
globalThis.location = { pathname: "/app/jarvis-chat" };
globalThis.addEventListener = () => {};

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
