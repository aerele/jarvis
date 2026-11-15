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

function fakeTab() {
  return {
    opener: {},
    closed: false,
    location: { href: "" },
    close() {
      this.closed = true;
    },
  };
}

test("the tab opens inside the click, then gets the encoded draft with details", async () => {
  const tab = fakeTab();
  const calls = [];
  let fetched = false;
  const ok = await openErrorReport({
    info: supportOnly,
    conversation: "conv-1",
    message: "msg-1",
    at: "2026-11-15 10:00:00",
    fetchDetails: async () => {
      fetched = true;
      return {
        ok: true,
        data: { error: "boom happened", model: "m1", run: "run-1" },
      };
    },
    openTab: (...args) => {
      calls.push({ args, fetchedYet: fetched });
      return tab;
    },
    navigate: () => assert.fail("must not navigate when the tab opened"),
  });
  assert.equal(ok, true);
  // Opened before the details call, so a popup blocker still sees the click.
  assert.deepEqual(calls, [{ args: ["", "_blank"], fetchedYet: false }]);
  assert.equal(tab.opener, null);
  const url = tab.location.href;
  assert.match(url, /^\/jarvis\/support\/new\?subject=Chat%20error/);
  const query = new URL(url, "http://x").searchParams;
  assert.match(query.get("body"), /Run: run-1/);
  assert.match(query.get("body"), /Conversation: conv-1/);
});

test("a blocked tab opens the ticket page in this tab instead", async () => {
  const went = [];
  const ok = await openErrorReport({
    info: supportOnly,
    conversation: "conv-1",
    message: "msg-1",
    fetchDetails: async () => {
      throw new Error("403");
    },
    openTab: () => null,
    navigate: (url) => went.push(url),
  });
  assert.equal(ok, true);
  assert.equal(went.length, 1);
  assert.match(went[0], /^\/jarvis\/support\/new\?/);
});

test("a cancelled turn has no draft: the blank tab closes and nothing navigates", async () => {
  const tab = fakeTab();
  const ok = await openErrorReport({
    info: { code: "cancelled" },
    conversation: "c",
    message: "m",
    fetchDetails: async () => null,
    openTab: () => tab,
    navigate: () => assert.fail("must not navigate"),
  });
  assert.equal(ok, false);
  assert.equal(tab.closed, true);
  assert.equal(
    reportErrorUrl({ subject: "a b", body: "x\ny" }),
    "/jarvis/support/new?subject=a%20b&body=x%0Ay"
  );
});
