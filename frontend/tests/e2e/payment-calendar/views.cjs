// Template checks 84, 87-91: hover does nothing, the company menu, the list view, the method dialogs.
// Checks 85 and 86 (hover preview) are dropped: owner decision 7 removed the preview.
const { scenario } = require("./scenario.cjs");
const { check, equal, includes } = require("./checks.cjs");
const { WIDTHS } = require("./boot.cjs");
const { openDetails, quiet, stat, modal } = require("./flows.cjs");

const options = { size: WIDTHS.desktop };

function hoverDefault() {
  scenario(
    "views: hovering a day does nothing",
    options,
    async ({ page, mock }) => {
      const mark = mock.mark();
      await page.locator('[data-cell-date="2026-11-12"]').hover();
      await quiet(page, mock);
      await check(
        "Default hover does not interrupt the date panel",
        async () => ({
          pass:
            (await page.locator("#preview").count()) === 0 ||
            (await page.locator("#preview").isHidden()),
        })
      );
      await check("Default hover requests nothing", async () =>
        equal(mock.calls(mark).length, 0)
      );
    }
  );
}

function companyMenu() {
  scenario(
    "views: company search and keyboard choice",
    options,
    async ({ page }) => {
      await page.locator("#company-trigger").click();
      await page.locator("#company-search").fill("Manufacturing");
      await check("Company search still filters options", async () =>
        equal(await page.locator('#company-options [role="option"]').count(), 1)
      );
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Enter");
      await check(
        "Company picker still supports keyboard selection",
        async () => ({ pass: await page.locator("#company-menu").isHidden() })
      );
    }
  );
}

function listAndMethods() {
  scenario(
    "views: list view and the method dialogs",
    options,
    async ({ page }) => {
      await page.locator("#list-button").click();
      await check("Desktop list offers all dates and week labels", async () => {
        const dates = equal(await page.locator("[data-list-date]").count(), 30);
        if (!dates.pass) return dates;
        const labels = await page.locator(".list-week-label").count();
        return { pass: labels >= 4, detail: `(${labels} week labels)` };
      });
      await page.locator("#snapshot-note").click();
      await check("Calculation explanation uses current inputs", async () =>
        includes(await stat(page, ".pcal-modal"), [
          "₹2,63,552.00",
          "₹2,13,552.00",
        ])
      );
      await page.keyboard.press("Escape");
      await openDetails(page);
      await page.locator("#balances-button").click();
      await check("Balance reconciliation remains available", async () =>
        includes(await stat(page, ".pcal-modal"), [
          "Total outstanding",
          "₹22,500.00",
        ])
      );
      await page.keyboard.press("Escape");
      await check("The method dialogs closed", async () =>
        equal(await modal(page).count(), 0)
      );
    }
  );
}

function register() {
  hoverDefault();
  companyMenu();
  listAndMethods();
}

module.exports = { register };
