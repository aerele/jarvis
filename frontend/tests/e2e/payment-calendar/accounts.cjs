// Template checks 56-72 and 99-101: the balances dialog, the account picker, the chips.
const { scenario } = require("./scenario.cjs");
const { check, equal, includes, textOf } = require("./checks.cjs");
const { noOverflow, sumOf, box } = require("./probe.cjs");
const { WIDTHS } = require("./boot.cjs");
const { stat, modal } = require("./flows.cjs");

const options = { size: WIDTHS.desktop };
const option = (page, label) =>
  page.locator(`.account-option input[aria-label="${label}"]`);
const options_ = (page) => page.locator(".account-option");
const chip = (page, label) =>
  page.locator(`button[aria-label="Remove ${label}"]`);
const bookCash = (page) => stat(page, "#book-cash");

async function openPicker(page) {
  await page.locator("#accounts-trigger").click();
  await page.locator("#accounts-menu").waitFor();
}

// Real clicks, one at a time: a box at the limit turns disabled before the next click.
async function checkEveryOption(page) {
  const boxes = page.locator("#accounts-options input[type=checkbox]");
  for (let index = 0; index < (await boxes.count()); index++) {
    const input = boxes.nth(index);
    if ((await input.isEnabled()) && !(await input.isChecked()))
      await input.check();
  }
}

function balancesDialog() {
  scenario(
    "accounts: balances dialog, selected and all accounts",
    options,
    async ({ page, mock }) => {
      await page.locator("#account-balances").click();
      await check(
        "Account breakdown names both selected bank and cash accounts",
        async () =>
          includes(await textOf(modal(page)), [
            "HDFC Current",
            "Petty Cash",
            "10:42 IST",
          ])
      );
      await check(
        "Selected account balances reconcile to book cash",
        async () => {
          const total = equal(
            await textOf(page.locator(".account-balance-total strong")),
            "₹2,46,850.00"
          );
          return total.pass
            ? equal(await sumOf(page.locator(".account-balance-row b")), 246850)
            : total;
        }
      );
      await page.locator("#account-balance-toggle").click();
      await check(
        "Account breakdown can show all available accounts without changing selection",
        async () => {
          const rows = equal(
            await page.locator(".account-balance-row").count(),
            60
          );
          return rows.pass
            ? includes(
                await textOf(page.locator(".account-balance-total")),
                "Total shown · all accounts"
              )
            : rows;
        }
      );
      await check(
        "All accounts is one balances job: request then poll, no snapshot request",
        async () => ({
          pass:
            mock.count("request_account_balances") === 1 &&
            mock.count("get_account_balances_status") >= 1 &&
            mock.count("request_snapshot") === 1,
        })
      );
      await page.keyboard.press("Escape");
    }
  );
}

function pickerAndSearch() {
  scenario(
    "accounts: picker lists 60, searches, cancels",
    options,
    async ({ page }) => {
      await openPicker(page);
      await check(
        "Scalable account picker contains 60 sample accounts in a scrollable list",
        async () => {
          const count = equal(await options_(page).count(), 60);
          return count.pass
            ? {
                pass: await page
                  .locator("#accounts-options")
                  .evaluate((el) => el.scrollHeight > el.clientHeight),
              }
            : count;
        }
      );
      await page.locator("#accounts-search").fill("HDFC");
      await check("Account search supports the bank name", async () => {
        const found = await options_(page).count();
        return { pass: found > 1 && found < 60, detail: `(${found} found)` };
      });
      await page.locator("#accounts-search").fill("9016");
      await check("Account search supports the last digits", async () =>
        equal(await options_(page).count(), 1)
      );
      await option(page, "ICICI Savings").check();
      await page.locator("#accounts-cancel").click();
      await check(
        "Cancelling draft account edits preserves book cash",
        async () => equal(await bookCash(page), "₹2,46,850.00")
      );
    }
  );
}

function chips() {
  scenario(
    "accounts: chips remove accounts and cash follows",
    options,
    async ({ page, mock }) => {
      test_requires(mock, "accounts_chip_removed", "removing an account");
      await chip(page, "Petty Cash").click();
      await check(
        "Removing an account updates cash and forecast consistently",
        async () => {
          const cash = equal(await bookCash(page), "₹2,21,850.00");
          return cash.pass
            ? equal(await stat(page, "#stat-closing"), "₹2,38,552.00")
            : cash;
        }
      );
      await chip(page, "HDFC Current").click();
      await check(
        "No accounts means unavailable cash and forecast",
        async () => {
          const cash = equal(await bookCash(page), "Unavailable");
          return cash.pass
            ? equal(await stat(page, "#stat-closing"), "No forecast available")
            : cash;
        }
      );
      await check(
        "No accounts means the lowest cushion is unavailable",
        async () => {
          const cushion = equal(
            await stat(page, "#lowest-cushion"),
            "Unavailable"
          );
          return cushion.pass
            ? { pass: await page.locator("#trough-jump").isDisabled() }
            : cushion;
        }
      );
      await page.locator("#header-book-cash").click();
      await check(
        "Empty account breakdown does not turn missing book cash into zero",
        async () =>
          equal(
            await textOf(page.locator(".account-balance-total strong")),
            "Unavailable"
          )
      );
      await page.keyboard.press("Escape");
      await check(
        "Invoice amounts remain usable without bank accounts",
        async () => equal(await stat(page, "#stat-payable"), "₹42,048.00")
      );
      await openPicker(page);
      await option(page, "HDFC Current").check();
      await option(page, "Petty Cash").check();
      await page.locator("#accounts-apply").click();
      await check("Applying the original accounts restores cash", async () =>
        equal(await bookCash(page), "₹2,46,850.00")
      );
      await check(
        "Each chip click saved the selection (set_preferences) and rebuilt the snapshot",
        async () => ({
          pass:
            mock.count("set_preferences") >= 3 &&
            mock.count("request_snapshot") >= 4,
        })
      );
    }
  );
}

// The scenario is part of PR 1's golden files; without it the test cannot run.
function test_requires(mock, scenarioName, what) {
  const { test } = require("@playwright/test");
  test.skip(
    !mock.hasScenario(scenarioName),
    `golden scenario ${scenarioName} is not in the golden files yet (${what})`
  );
}

function largeSelection() {
  scenario(
    "accounts: the 50-account limit and compact chips",
    options,
    async ({ page }) => {
      await openPicker(page);
      await checkEveryOption(page);
      await check("Picker enforces a 50-account selection limit", async () => {
        const count = includes(
          await textOf(page.locator("#accounts-count")),
          "50 of 50 selected"
        );
        return count.pass
          ? equal(
              await page.locator("#accounts-options input:disabled").count(),
              10
            )
          : count;
      });
      await check(
        "Large draft changes do not recompute the applied forecast",
        async () => equal(await bookCash(page), "₹2,46,850.00")
      );
      await page.locator("#accounts-apply").click();
      await check(
        "Large selection stays compact with two chips and an overflow count",
        async () => {
          const shown = equal(
            await page.locator(".account-chip.active").count(),
            2
          );
          return shown.pass
            ? equal(
                await textOf(page.locator("#accounts-overflow")),
                "+48 selected"
              )
            : shown;
        }
      );
      await check("Large selection does not widen the desktop page", () =>
        noOverflow(page)
      );
    }
  );
}

function phonePicker() {
  scenario(
    "accounts: phone picker and balances",
    { size: WIDTHS.phone },
    async ({ page }) => {
      await page.locator("#accounts-trigger").tap();
      await check(
        "Phone account picker stays within the viewport width",
        async () => {
          const menu = await box(page.locator("#accounts-menu"));
          return {
            pass: menu.x >= 0 && menu.x + menu.width <= 390,
            detail: `(x ${menu.x}, width ${menu.width})`,
          };
        }
      );
      await page.locator("#accounts-search").fill("Petty");
      await check("Phone account search narrows a large catalog", async () =>
        equal(await options_(page).count(), 1)
      );
      await page.locator("#accounts-cancel").tap();
      await page.locator("#account-balances").tap();
      await check("Phone account balance breakdown is readable", async () => {
        const total = equal(
          await textOf(page.locator(".account-balance-total strong")),
          "₹2,46,850.00"
        );
        if (!total.pass) return total;
        return {
          pass: await page
            .locator(".account-balance-row")
            .first()
            .evaluate((el) => el.getBoundingClientRect().right <= innerWidth),
        };
      });
    }
  );
}

function register() {
  balancesDialog();
  pickerAndSearch();
  chips();
  largeSelection();
  phonePicker();
}

module.exports = { register };
