// The request log: which endpoints each action calls, AC1, and late replies (A5).
const { scenario } = require("./scenario.cjs");
const { expect } = require("@playwright/test");
const { check, equal, includes } = require("./checks.cjs");
const { WIDTHS } = require("./boot.cjs");
const { quiet, saveFloor, selectDay, stat } = require("./flows.cjs");

const options = { size: WIDTHS.desktop };
const NOT_FINANCE_WRITES = ["configure_floor", "set_preferences"];
const names = (mock, mark) => mock.calls(mark);

function firstLoad() {
  scenario(
    "requests: first load reads, never writes",
    options,
    async ({ mock }) => {
      await check(
        "First load calls get_preferences, get_capabilities, one request_snapshot",
        async () => ({
          pass:
            mock.count("get_preferences") === 1 &&
            mock.count("get_capabilities") >= 2 &&
            mock.count("request_snapshot") === 1,
          detail: `(${names(mock, 0)})`,
        })
      );
      await check(
        "First load never writes the floor or the preferences",
        async () => ({
          pass: NOT_FINANCE_WRITES.every((method) => mock.count(method) === 0),
        })
      );
      await check(
        "First load reads the day and the upcoming list once each",
        async () => ({
          pass:
            mock.count("get_day_detail") === 1 &&
            mock.count("get_upcoming") === 1,
          detail: `(${names(mock, 0)})`,
        })
      );
    }
  );
}

function actions() {
  scenario(
    "requests: each action calls the endpoints it should",
    options,
    async ({ page, mock }) => {
      let mark = mock.mark();
      await page.locator("#theme-toggle").click();
      await quiet(page, mock);
      await check("The theme switch makes no calendar request", async () =>
        equal(names(mock, mark).length, 0)
      );
      mark = mock.mark();
      await selectDay(page, "2026-11-13");
      await check(
        "One date click makes exactly one detail read and one upcoming read (AC1)",
        async () => ({
          pass:
            mock.count("get_day_detail", mark) === 1 &&
            mock.count("get_upcoming", mark) === 1 &&
            mock.count("request_snapshot", mark) === 0,
          detail: `(${names(mock, mark)})`,
        })
      );
      mark = mock.mark();
      await page.locator("#month-flow-details").click();
      await check(
        "View totals reads the window bucket, not a snapshot",
        async () => ({
          pass:
            mock.log
              .slice(mark)
              .some(
                (entry) =>
                  entry.method === "get_bucket_detail" &&
                  entry.args.bucket === "window"
              ) && mock.count("request_snapshot", mark) === 0,
          detail: `(${names(mock, mark)})`,
        })
      );
      await page.keyboard.press("Escape");
      mark = mock.mark();
      await saveFloor(page, 140000);
      await check(
        "Saving the minimum calls configure_floor then request_snapshot, never set_preferences",
        async () => {
          const list = names(mock, mark);
          return {
            pass:
              list.indexOf("configure_floor") >= 0 &&
              list.indexOf("configure_floor") <
                list.indexOf("request_snapshot") &&
              !list.includes("set_preferences"),
            detail: `(${list})`,
          };
        }
      );
      mark = mock.mark();
      await page.locator("#next-month").click();
      await check(
        "Next month requests that month's snapshot and writes nothing",
        async () => ({
          pass:
            mock.log
              .slice(mark)
              .some(
                (entry) =>
                  entry.method === "request_snapshot" &&
                  entry.args.month === "2026-12"
              ) &&
            NOT_FINANCE_WRITES.every(
              (method) => mock.count(method, mark) === 0
            ),
          detail: `(${names(mock, mark)})`,
        })
      );
    }
  );
}

function sorting() {
  scenario(
    "requests: a sort click is a server read with sort and order",
    options,
    async ({ page, mock }) => {
      await page.locator("#panel-view-all").click();
      // The dialog's own read leaves after the debounce: let it finish before the click.
      await expect
        .poll(() => mock.count("get_day_detail"), { timeout: 5000 })
        .toBeGreaterThan(1);
      await quiet(page, mock);
      const mark = mock.mark();
      await page.locator('[data-sort="party"]').click();
      await check(
        "Sorting sends sort and order to get_day_detail and requests no snapshot",
        async () => {
          const read = mock.log
            .slice(mark)
            .find((entry) => entry.method === "get_day_detail");
          return {
            pass:
              !!read &&
              read.args.sort === "party" &&
              ["asc", "desc"].includes(read.args.order) &&
              mock.count("request_snapshot", mark) === 0,
            detail: `(${JSON.stringify(read?.args)})`,
          };
        }
      );
    }
  );
}

// A5: a slow reply for an older date never replaces the newer date's rows.
function lateReplies() {
  scenario(
    "requests: a late reply for an older date never renders (A5)",
    options,
    async ({ page, mock }) => {
      const mark = mock.mark();
      mock.delayNext("get_day_detail", 2500);
      await selectDay(page, "2026-11-12");
      // The read leaves after the debounce: wait until the slow one is on its way, then
      // pick the newer date and wait for the late reply to arrive.
      await expect
        .poll(() => mock.count("get_day_detail", mark), { timeout: 5000 })
        .toBe(1);
      await selectDay(page, "2026-11-14");
      await quiet(page, mock);
      await check(
        "The panel shows the newer date, not the late reply",
        async () => {
          const heading = includes(
            await stat(page, "#ledger-heading"),
            "14 Nov"
          );
          if (!heading.pass) return heading;
          const due = await page
            .locator("#day-panel .ledger-row small")
            .allTextContents();
          return {
            pass: due.every((text) => !text.includes("12 Nov")),
            detail: `(${due})`,
          };
        }
      );
    }
  );
}

function register() {
  firstLoad();
  actions();
  sorting();
  lateReplies();
}

module.exports = { register };
