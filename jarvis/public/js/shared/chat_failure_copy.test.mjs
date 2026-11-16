import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CANCEL_FALLBACK,
  OFFLINE,
  SESSION_EXPIRED,
  cancelCopy,
  failureCopy,
  failureKind,
  refusalCopy,
  serverReason,
  uploadFailureCopy,
} from "./chat_failure_copy.mjs";

test("401 and 403 mean the session expired", () => {
  assert.equal(failureCopy({ status: 401 }, "x"), SESSION_EXPIRED);
  assert.equal(failureCopy({ status: 403 }, "x"), SESSION_EXPIRED);
});

test("a dead connection says so, for jQuery (status 0) and fetch (TypeError)", () => {
  assert.equal(failureKind({ status: 0, readyState: 0 }), "offline");
  assert.equal(failureCopy(new TypeError("Failed to fetch"), "x"), OFFLINE);
  assert.equal(failureCopy(new TypeError("Load failed"), "x"), OFFLINE);
});

test("anything else keeps the caller's sentence, and the helper never throws", () => {
  assert.equal(
    failureCopy({ status: 500, messages: ["boom"] }, "Could not send."),
    "Could not send."
  );
  assert.equal(failureCopy(undefined, "Could not send."), "Could not send.");
  assert.equal(failureCopy(null, "Could not send."), "Could not send.");
  assert.equal(failureCopy("text", "Could not send."), "Could not send.");
});

test("a server message on status 0 is not called offline", () => {
  assert.equal(failureKind({ status: 0, messages: ["x"] }), "other");
});

test("refusal copy covers the codes the desktop app handles, and nothing else", () => {
  assert.match(
    refusalCopy("usage_limit", "Aida", { limit_period: "Daily" }),
    /today's usage limit/
  );
  assert.match(refusalCopy("usage_limit", "Aida", {}), /Ask your Aida admin/);
  assert.match(
    refusalCopy("llm_not_configured", "Jarvis"),
    /No AI model is connected/
  );
  assert.match(refusalCopy("subscription_suspended", "Jarvis"), /Renew it/);
  assert.match(
    refusalCopy("workspace_resetting", "Aida"),
    /Aida is being reset/
  );
  assert.equal(refusalCopy("who_knows", "Jarvis"), "");
  assert.equal(refusalCopy("constructor", "Jarvis"), "");
});

test("a cancelled queued turn: sentences pass, codes and JSON get the plain line", () => {
  assert.equal(
    cancelCopy("You cancelled this message before it was answered."),
    "You cancelled this message before it was answered."
  );
  assert.equal(cancelCopy("age_out"), CANCEL_FALLBACK);
  assert.equal(cancelCopy('{"code": "x y"}'), CANCEL_FALLBACK);
  assert.equal(cancelCopy(undefined), CANCEL_FALLBACK);
});

test("an upload failure says what to do", () => {
  assert.match(
    uploadFailureCopy({ status: 413 }, "a.pdf"),
    /"a.pdf" is too large/
  );
  assert.equal(uploadFailureCopy({ status: 403 }), SESSION_EXPIRED);
  assert.equal(uploadFailureCopy(new TypeError("Failed to fetch")), OFFLINE);
  assert.equal(
    uploadFailureCopy({
      status: 417,
      serverReason: "File type of x.exe is not allowed",
    }),
    "File type of x.exe is not allowed"
  );
  assert.match(
    uploadFailureCopy({ status: 500 }),
    /could not be attached\. Check your connection/
  );
});

test("serverReason reads Frappe's server message", () => {
  const body = {
    _server_messages: JSON.stringify([
      JSON.stringify({ message: "<b>Too big</b>" }),
    ]),
  };
  assert.equal(serverReason(body), "Too big");
});

test("a 403 the server explained shows its own sentence, not 'session expired'", () => {
  const refused = {
    status: 403,
    messages: ["You don&#39;t have permission to open this file."],
  };
  assert.equal(failureKind(refused), "refused");
  assert.equal(
    failureCopy(refused, "fallback"),
    "You don't have permission to open this file."
  );
  assert.equal(failureCopy({ status: 403 }, "fallback"), SESSION_EXPIRED);
  assert.equal(
    failureCopy({ status: 401, messages: ["x y"] }, "fallback"),
    SESSION_EXPIRED
  );
  assert.equal(
    uploadFailureCopy(
      { status: 403, serverReason: "File type is not allowed." },
      "a.exe"
    ),
    "File type is not allowed."
  );
});
