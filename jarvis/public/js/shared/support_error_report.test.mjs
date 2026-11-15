import { test } from "node:test";
import assert from "node:assert/strict";
import { turnErrorInfo } from "../turn_errors.mjs";
import {
  openErrorReport,
  reportErrorUrl,
  showReportError,
} from "./support_error_report.mjs";

const supportOnly = turnErrorInfo("something unexpected broke", "internal");
const fixable = turnErrorInfo("Rate limit reached", "rate-limit");

test("Report error shows for a support-only failure when support is on", () => {
  assert.equal(supportOnly.support, true);
  assert.equal(showReportError(supportOnly, true), true);
});

test("it stays hidden when the user or admin can fix it, or support is off", () => {
  assert.equal(fixable.support, false);
  assert.equal(showReportError(fixable, true), false);
  assert.equal(showReportError(supportOnly, false), false);
  assert.equal(showReportError(null, true), false);
  assert.equal(
    showReportError({ support: true, code: "cancelled" }, true),
    false
  );
});

test("it opens the New Ticket page with the encoded draft, details included", async () => {
  const opened = [];
  const ok = await openErrorReport({
    info: supportOnly,
    conversation: "conv-1",
    message: "msg-1",
    at: "2026-11-15 10:00:00",
    fetchDetails: async () => ({
      ok: true,
      data: { error: "boom happened", model: "m1", run: "run-1" },
    }),
    open: (...args) => opened.push(args),
  });
  assert.equal(ok, true);
  const [url, target, features] = opened[0];
  assert.equal(target, "_blank");
  assert.equal(features, "noopener");
  assert.match(url, /^\/jarvis\/support\/new\?subject=Chat%20error/);
  const query = new URL(url, "http://x").searchParams;
  assert.match(query.get("body"), /Run: run-1/);
  assert.match(query.get("body"), /Conversation: conv-1/);
});

test("a failed details call still opens the ticket", async () => {
  const opened = [];
  const ok = await openErrorReport({
    info: supportOnly,
    conversation: "conv-1",
    message: "msg-1",
    fetchDetails: async () => {
      throw new Error("403");
    },
    open: (...args) => opened.push(args),
  });
  assert.equal(ok, true);
  assert.equal(opened.length, 1);
});

test("a cancelled turn has no draft, so nothing opens", async () => {
  const opened = [];
  const ok = await openErrorReport({
    info: { code: "cancelled" },
    conversation: "c",
    message: "m",
    fetchDetails: async () => null,
    open: (...args) => opened.push(args),
  });
  assert.equal(ok, false);
  assert.equal(opened.length, 0);
  assert.equal(
    reportErrorUrl({ subject: "a b", body: "x\ny" }),
    "/jarvis/support/new?subject=a%20b&body=x%0Ay"
  );
});
