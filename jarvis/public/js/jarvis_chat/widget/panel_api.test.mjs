import { test } from "node:test";
import assert from "node:assert/strict";
import { TRANSCRIPTION_TIMEOUT_MS, transcribeAudio } from "./panel_api.mjs";

test("Desk dictation uses the full Gemini transcription timeout", async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  let scheduledDelay;
  let clearedTimer;

  globalThis.window = { csrf_token: "test-csrf" };
  globalThis.setTimeout = (_callback, delay) => {
    scheduledDelay = delay;
    return 42;
  };
  globalThis.clearTimeout = (timer) => {
    clearedTimer = timer;
  };
  globalThis.fetch = async (_url, options) => {
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
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
});
