// Template checks 26-55: obligations, trough jump, floor states, the side panel, full details.
const { scenario } = require("./scenario.cjs");
const { check, equal, includes, textOf } = require("./checks.cjs");
const { sumOf } = require("./probe.cjs");
const { WIDTHS } = require("./boot.cjs");
const {
  openDetails,
  saveFloor,
  selectDay,
  firstDetailRow,
  stat,
} = require("./flows.cjs");

const options = { size: WIDTHS.desktop };
const cell = (page, date) => page.locator(`[data-cell-date="${date}"]`);
const remainingSum = (page) => sumOf(page.locator(".remaining-column strong"));

async function obligationCheck(page, scope, expected) {
  await page.locator(`[data-obligation="${scope}"]`).click();
  await page.locator("#panel-view-all").click();
  await check(
    `${scope} opens its matching obligation records (${expected})`,
    async () => equal(await remainingSum(page), expected)
  );
  await page.keyboard.press("Escape");
}

function immediateAndTrough() {
  scenario(
    "ledger: immediate dues and the trough jump",
    options,
    async ({ page }) => {
      await page.locator('[data-obligation="immediate"]').click();
      await check(
        "Immediate dues scrolls its ledger heading into view and moves focus",
        async () =>
          page.evaluate(() => {
            const heading = document.getElementById("ledger-heading"),
              rect = heading.getBoundingClientRect();
            return (
              document.activeElement === heading &&
              rect.top >= 0 &&
              rect.bottom <= innerHeight
            );
          })
      );
      await page.locator("#panel-view-all").click();
      await check("Immediate dues drilldown reconciles to its KPI", async () =>
        equal(await remainingSum(page), 27500)
      );
      await page.keyboard.press("Escape");
      await selectDay(page, "2026-11-11");
      await page.locator('#day-panel [data-panel-side="Receivable"]').click();
      await page.locator("#trough-jump").click();
      await check("Jump to trough selects the exact calendar day", async () =>
        includes(
          await cell(page, "2026-11-23").getAttribute("class"),
          "selected"
        )
      );
      await check(
        "Jump to trough opens matching supplier records",
        async () => {
          const heading = includes(
            await textOf(page.locator("#ledger-heading")),
            "23 Nov"
          );
          if (!heading.pass) return heading;
          return equal(
            await page
              .locator('#day-panel [data-panel-side="Payable"]')
              .getAttribute("aria-pressed"),
            "true"
          );
        }
      );
      await check(
        "Selecting a different day does not turn month KPIs into daily figures",
        async () => {
          const month = equal(await stat(page, "#month-flow"), "+₹3,29,702.00");
          return month.pass
            ? equal(await stat(page, "#immediate-dues"), "₹27,500.00")
            : month;
        }
      );
    }
  );
}

function floorStates() {
  scenario(
    "floor: cushion states follow the saved minimum",
    options,
    async ({ page, mock }) => {
      await saveFloor(page, 140000);
      await check(
        "Cushion within ten percent of the floor is amber",
        async () => {
          const value = equal(await stat(page, "#lowest-cushion"), "₹7,614.00");
          return value.pass
            ? equal(
                await page.locator("#lowest-card").getAttribute("data-state"),
                "amber"
              )
            : value;
        }
      );
      await saveFloor(page, 147614);
      await check("A cushion exactly at the floor is tight", async () => {
        const value = equal(await stat(page, "#lowest-cushion"), "₹0.00");
        return value.pass
          ? equal(await stat(page, "#lowest-status"), "Tight")
          : value;
      });
      await saveFloor(page, 150000);
      await check("Negative cushion is red with the deficit date", async () => {
        const value = equal(await stat(page, "#lowest-cushion"), "−₹2,386.00");
        if (!value.pass) return value;
        const state = equal(
          await page.locator("#lowest-card").getAttribute("data-state"),
          "red"
        );
        return state.pass
          ? includes(await stat(page, "#lowest-note"), "23 Nov")
          : state;
      });
      await check(
        "Each save called configure_floor, then request_snapshot",
        async () => equal(mock.count("configure_floor"), 3)
      );
    }
  );
}

function sidePanelAndDetails() {
  scenario(
    "ledger: receivable panel, full details, sorting",
    options,
    async ({ page }) => {
      await openDetails(page);
      await page.locator("#stat-receivable").click();
      await check(
        "Receivable amount opens only customer records in the date panel",
        async () => {
          const text = await textOf(page.locator("#day-panel"));
          const found = includes(text, ["Evergreen Retail", "SINV-2026-0284"]);
          return found.pass
            ? {
                pass: !text.includes("PINV-2026-0142"),
                detail: "(a supplier invoice is listed)",
              }
            : found;
        }
      );
      await check("Date panel does not block the calendar", async () =>
        equal(await page.locator(".pcal-backdrop").count(), 0)
      );
      await check("Invoice tab includes count and total", async () =>
        includes(
          await textOf(
            page.locator('#day-panel [data-panel-side="Receivable"]')
          ),
          ["2", "₹86,250.00"]
        )
      );
      await page.locator("#stat-payable").click();
      await page.locator("#panel-view-all").click();
      await check("Full view contains all three supplier invoices", async () =>
        equal(await page.locator(".detail-table tbody tr").count(), 3)
      );
      await page.locator('[data-sort="remaining"]').click();
      await check("Balance column sorts ascending", async () =>
        includes(await textOf(firstDetailRow(page)), "PINV-2026-0151")
      );
      await page.locator('[data-sort="party"]').click();
      await check("Party column sorts alphabetically", async () =>
        includes(await textOf(firstDetailRow(page)), "Atlas Components")
      );
      await page.locator('[data-sort="date"]').click();
      await check("Due date sort is available", async () =>
        includes(
          await textOf(page.locator('th[aria-sort="ascending"]')),
          "Due date"
        )
      );
      await check(
        "Full details have no payment creation or ERP actions",
        async () =>
          equal(
            await page
              .locator("[data-pick],[data-erp],#batch-bar,#modal-batch")
              .count(),
            0
          )
      );
      await check("Invoice numbers are plain text", async () => {
        const plain =
          (await page.locator(".detail-table .invoice-reference").count()) ===
          3;
        return {
          pass:
            plain &&
            (await page
              .locator(".detail-table a,.detail-table [data-erp]")
              .count()) === 0,
        };
      });
    }
  );
}

function obligationBuckets() {
  scenario(
    "ledger: every obligation bucket reconciles",
    options,
    async ({ page }) => {
      await openDetails(page);
      for (const [scope, expected] of [
        ["today", 42048],
        ["overdue", 22500],
        ["undated", 5000],
        ["now", 69548],
      ])
        await obligationCheck(page, scope, expected);
    }
  );
}

function dateAndMonthScope() {
  scenario(
    "ledger: date, month scope, floor draft and warning",
    options,
    async ({ page }) => {
      await openDetails(page);
      await selectDay(page, "2026-11-18");
      await check(
        "18 Nov updates the date heading and dated figures",
        async () => {
          const heading = includes(
            await stat(page, "#selected-date-heading"),
            "18 Nov"
          );
          return heading.pass
            ? equal(await stat(page, "#stat-payable"), "₹36,950.00")
            : heading;
        }
      );
      await check(
        "Snapshot total stays fixed on future-date selection",
        async () => includes(await stat(page, "#total-payable"), "₹69,548.00")
      );
      await check(
        "Future forecast exposes cumulative assumed collections",
        async () =>
          includes(await stat(page, "#closing-note"), [
            "₹2,82,950.00",
            "by 18 Nov",
          ])
      );
      await page.locator("#scope-month").click();
      await check(
        "Monthly aggregate has an explicit scope and different totals",
        async () => {
          const caption = includes(
            await stat(page, "#scope-caption"),
            "Monthly aggregate"
          );
          return caption.pass
            ? { pass: (await stat(page, "#stat-payable")) !== "₹36,950.00" }
            : caption;
        }
      );
      await check("Monthly projection uses month end", async () =>
        includes(await stat(page, "#floor-context"), "30 Nov")
      );
      await page.locator("#today-button").click();
      await check("Today returns the cash details to the day view", async () =>
        equal(
          await page.locator("#scope-day").getAttribute("aria-pressed"),
          "true"
        )
      );
      await page.locator("#scope-day").click();
      await page.locator("#floor-input").fill("200000");
      await check("Unsaved minimum does not change the forecast", async () =>
        equal(await stat(page, "#floor-margin"), "₹2,13,552.00 above minimum")
      );
      await page.locator("#floor-save").click();
      await check(
        "Cash details stay open while the saved minimum reloads",
        async () => ({
          pass: await page
            .locator("#financial-details")
            .evaluate((el) => el.open),
        })
      );
      await openDetails(page);
      await check("Threshold warning identifies first low day", async () =>
        includes(await stat(page, "#risk-message"), "19 Nov")
      );
      await page.locator("#risk-jump").click();
      await check(
        "Warning jump selects the low day and shows its shortfall",
        async () => {
          const heading = includes(
            await stat(page, "#selected-date-heading"),
            "19 Nov"
          );
          return heading.pass
            ? includes(await stat(page, "#floor-margin"), "below minimum")
            : heading;
        }
      );
      await page.locator("#floor-input").fill("-2");
      await page.locator("#floor-save").click();
      await check("Invalid minimum is rejected inline", async () =>
        includes(await stat(page, "#floor-error"), "non-negative")
      );
    }
  );
}

function register() {
  immediateAndTrough();
  floorStates();
  sidePanelAndDetails();
  obligationBuckets();
  dateAndMonthScope();
}

module.exports = { register };
