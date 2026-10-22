import { test } from "node:test";
import assert from "node:assert/strict";
import {
  TRANSCRIPTION_TIMEOUT_MS,
  csrf,
  serverReason,
  transcribeAudio,
  uploadFile,
} from "./panel_api.mjs";

test("Desk dictation uses the full Gemini transcription timeout", async () => {
  const originalFetch = globalThis.fetch;
  const originalFrappe = globalThis.frappe;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  let scheduledDelay;
  let clearedTimer;

  // Desk reality: the token is on frappe.csrf_token, window.csrf_token is unset.
  globalThis.frappe = { csrf_token: "test-csrf" };
  globalThis.setTimeout = (_callback, delay) => {
    scheduledDelay = delay;
    return 42;
  };
  globalThis.clearTimeout = (timer) => {
    clearedTimer = timer;
  };
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.headers["X-Frappe-CSRF-Token"], "test-csrf");
    assert.equal(options.signal.aborted, false);
    return {
      ok: true,
      status: 200,
      json: async () => ({ message: { ok: true, text: "hello" } }),
    };
  };

  try {
    const result = await transcribeAudio(
      new Blob(["recording"], { type: "audio/webm" }),
      2
    );
    assert.equal(TRANSCRIPTION_TIMEOUT_MS, 150000);
    assert.equal(scheduledDelay, TRANSCRIPTION_TIMEOUT_MS);
    assert.equal(clearedTimer, 42);
    assert.equal(result.text, "hello");
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
    if (originalFrappe === undefined) delete globalThis.frappe;
    else globalThis.frappe = originalFrappe;
  }
});

test("csrf() prefers frappe.csrf_token, then window, then empty", () => {
  const f = globalThis.frappe;
  const w = globalThis.csrf_token;
  try {
    globalThis.frappe = { csrf_token: "from-frappe" };
    globalThis.csrf_token = "from-window";
    assert.equal(csrf(), "from-frappe");
    globalThis.frappe = {};
    assert.equal(csrf(), "from-window");
    delete globalThis.csrf_token;
    assert.equal(csrf(), "");
    delete globalThis.frappe;
    assert.equal(csrf(), "");
  } finally {
    if (f === undefined) delete globalThis.frappe;
    else globalThis.frappe = f;
    if (w === undefined) delete globalThis.csrf_token;
    else globalThis.csrf_token = w;
  }
});

test("serverReason reads _server_messages, then message, then exception", () => {
  const sm = JSON.stringify([JSON.stringify({ message: "<b>Too big</b>" })]);
  assert.equal(serverReason({ _server_messages: sm }), "Too big");
  assert.equal(serverReason({ message: "plain" }), "plain");
  assert.equal(
    serverReason({ exception: "frappe.exceptions.ValidationError: nope" }),
    "nope"
  );
  assert.equal(serverReason({}), "");
  assert.equal(serverReason(null), "");
});

test("uploadFile sends the Desk token and surfaces the server reason", async () => {
  const originalFetch = globalThis.fetch;
  const originalFrappe = globalThis.frappe;
  let sent;
  globalThis.frappe = { csrf_token: "desk-token" };
  globalThis.fetch = async (_url, options) => {
    sent = options.headers["X-Frappe-CSRF-Token"];
    return {
      ok: false,
      status: 417,
      json: async () => ({
        _server_messages: JSON.stringify([
          JSON.stringify({ message: "File type not allowed" }),
        ]),
      }),
    };
  };
  try {
    await assert.rejects(
      uploadFile(new File(["x"], "a.txt")),
      (err) => err.serverReason === "File type not allowed"
    );
    assert.equal(sent, "desk-token");
  } finally {
    globalThis.fetch = originalFetch;
    if (originalFrappe === undefined) delete globalThis.frappe;
    else globalThis.frappe = originalFrappe;
  }
});
