// Template checks 73-83 (month navigation) and the D4 adaptation: the app loads a whole month,
// so "partial December" becomes "complete December" plus an incomplete-month variant.
const { scenario } = require("./scenario.cjs");
const { check, equal, includes } = require("./checks.cjs");
const { sumOf } = require("./probe.cjs");
const { WIDTHS } = require("./boot.cjs");
const { openDetails, selectDay, stat } = require("./flows.cjs");

const options = { size: WIDTHS.desktop };
const cell = (page, date) => page.locator(`[data-cell-date="${date}"]`);
const goToDecember = async (page) => {
  await page.locator("#next-month").click();
  await page.waitForFunction(
    () =>
      document.querySelector("#month-heading")?.textContent.trim() ===
        "December 2026" && !document.querySelector(".headline-value.skeleton")
  );
};

function december() {
  scenario(
    "months: December comes from its own snapshot",
    options,
    async ({ page, mock }) => {
      await goToDecember(page);
      await check(
        "Month navigation changes both month and selected date",
        async () => {
          const month = equal(
            await stat(page, "#month-heading"),
            "December 2026"
          );
          return month.pass
            ? includes(await stat(page, "#selected-date-heading"), "1 Dec")
            : month;
        }
      );
      await check(
        "December upcoming list cannot contain November records",
        async () => {
          const month = equal(
            await stat(page, "#upcoming-month"),
            "December 2026"
          );
          return month.pass
            ? equal(await page.locator('[data-upcoming^="2026-11"]').count(), 0)
            : month;
        }
      );
      await check(
        "A complete December carries no partial-month label",
        async () => ({
          pass: !(await stat(page, ".next-panel")).includes("Partial month"),
        })
      );
      await check("A complete month shows the server's month net", async () =>
        equal(await stat(page, "#month-flow"), "−₹5,000.00")
      );
      await check(
        "Loaded December dates have a coherent cash forecast",
        async () => equal(await stat(page, "#stat-closing"), "₹2,68,052.00")
      );
      await selectDay(page, "2026-12-15");
      await check(
        "Any December date has a server forecast (no unloaded dates, no past-date message)",
        async () => {
          const closing = equal(
            await stat(page, "#stat-closing"),
            "₹2,57,052.00"
          );
          return closing.pass
            ? {
                pass: !(await stat(page, "#closing-note")).includes(
                  "before today"
                ),
              }
            : closing;
        }
      );
      await check(
        "Snapshot obligation stays tied to its timestamp in December",
        async () =>
          includes(await stat(page, ".snapshot-card"), [
            "11 Nov, 10:42 IST",
            "₹69,548.00",
          ])
      );
      await check(
        "Moving to December requested a December snapshot once",
        async () => ({
          pass:
            mock.log.filter(
              (entry) =>
                entry.method === "request_snapshot" &&
                entry.args.month === "2026-12"
            ).length === 1,
        })
      );
      await openDetails(page);
      await page.locator('[data-obligation="now"]').click();
      await page.locator("#panel-view-all").click();
      await check(
        "The now bucket works on a December snapshot (AB8)",
        async () =>
          equal(await sumOf(page.locator(".remaining-column strong")), 69548)
      );
      await page.keyboard.press("Escape");
    }
  );
}

function troughFromDecember() {
  scenario(
    "months: the trough jump returns from another month",
    options,
    async ({ page }) => {
      await goToDecember(page);
      await page.locator("#trough-jump").click();
      await check(
        "Jump to trough returns from another month to the exact date",
        async () => {
          const month = equal(
            await stat(page, "#month-heading"),
            "November 2026"
          );
          return month.pass
            ? includes(
                await cell(page, "2026-11-23").getAttribute("class"),
                "selected"
              )
            : month;
        }
      );
      await check(
        "Jump from another month shows the matching supplier ledger",
        async () => {
          const heading = includes(
            await stat(page, "#ledger-heading"),
            "23 Nov"
          );
          return heading.pass
            ? equal(
                await page
                  .locator('#day-panel [data-panel-side="Payable"]')
                  .getAttribute("aria-pressed"),
                "true"
              )
            : heading;
        }
      );
      await page.locator("#today-button").click();
      await selectDay(page, "2026-11-30");
      await check(
        "Known empty remainder says no payments scheduled",
        async () =>
          includes(await stat(page, ".next-list"), "No payments scheduled")
      );
    }
  );
}

function incompleteMonth() {
  const setup = (mock) =>
    mock.editStatus((status) => {
      if (status.scope.month !== "2026-12") return;
      status.projection.month_totals = {
        ...status.projection.month_totals,
        complete: false,
        net: null,
      };
    });
  scenario(
    "months: a month the server marks incomplete",
    { ...options, setup },
    async ({ page }) => {
      await goToDecember(page);
      await check(
        "A partial month cannot claim a complete month net",
        async () => equal(await stat(page, "#month-flow"), "Unavailable")
      );
      await check(
        "Unavailable remaining schedule is unavailable, not empty",
        async () => {
          const text = await stat(page, ".next-list");
          return {
            pass:
              text.includes("Payment schedule unavailable") &&
              !text.includes("No payments scheduled"),
            detail: `(${text})`,
          };
        }
      );
    }
  );
}

function register() {
  december();
  troughFromDecember();
  incompleteMonth();
}

module.exports = { register };
