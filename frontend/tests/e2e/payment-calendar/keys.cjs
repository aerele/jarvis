// A11: keyboard. Grid arrows and Enter, the Escape order (C8), Tab wrap and focus return in
// every modal, and the swipe threshold. Real key presses throughout.
const { scenario } = require("./scenario.cjs");
const { check, equal, includes } = require("./checks.cjs");
const { active } = require("./probe.cjs");
const { WIDTHS } = require("./boot.cjs");
const { openDetails, stat, modal, quiet, swipe } = require("./flows.cjs");

const options = { size: WIDTHS.desktop };

function gridKeys() {
  scenario(
    "keys: arrows move focus, Enter selects",
    options,
    async ({ page }) => {
      await page.locator('[data-date="2026-11-11"]').click();
      await page.keyboard.press("ArrowRight");
      await check(
        "ArrowRight moves focus to the next date without selecting it",
        async () => {
          const now = await active(page);
          const heading = await stat(page, "#selected-date-heading");
          return {
            pass: now.date === "2026-11-12" && heading.includes("11 Nov"),
            detail: JSON.stringify(now),
          };
        }
      );
      await page.keyboard.press("ArrowDown");
      await check("ArrowDown moves one week down", async () =>
        equal((await active(page)).date, "2026-11-19")
      );
      await page.keyboard.press("Enter");
      await check("Enter selects the focused date", async () =>
        includes(await stat(page, "#selected-date-heading"), "19 Nov")
      );
      await page.keyboard.press("ArrowUp");
      await page.keyboard.press("ArrowLeft");
      await check("ArrowUp and ArrowLeft move back", async () =>
        equal((await active(page)).date, "2026-11-11")
      );
    }
  );
}

// Open the details modal, then the calculation example above it (an aux layer): Escape closes the top one.
function escapeOrder() {
  scenario(
    "keys: Escape closes the top layer first (C8)",
    options,
    async ({ page }) => {
      await page.locator("#panel-view-all").click();
      await page.locator("#snapshot-note").evaluate((el) => el.click());
      await check(
        "Two modals are open (details and the aux example)",
        async () => equal(await modal(page).count(), 2)
      );
      await page.keyboard.press("Escape");
      await check("First Escape closes the aux modal only", async () => {
        const open = await modal(page).count();
        const detailsOpen = await page.locator("#close-details").count();
        return {
          pass: open === 1 && detailsOpen === 1,
          detail: `(${open} open, details dialog open: ${detailsOpen})`,
        };
      });
      await page.keyboard.press("Escape");
      await check("Second Escape closes the details modal", async () =>
        equal(await modal(page).count(), 0)
      );
      await page.locator("#company-trigger").click();
      await page.keyboard.press("Escape");
      await check(
        "Escape closes the company menu and returns focus to its button",
        async () => {
          const closed = await page.locator("#company-menu").count();
          return {
            pass: closed === 0 && (await active(page)).id === "company-trigger",
            detail: JSON.stringify(await active(page)),
          };
        }
      );
    }
  );
}

const OPENERS = [
  {
    name: "balances",
    open: (page) => page.locator("#account-balances").click(),
    id: "account-balances",
  },
  {
    name: "invoice details",
    open: (page) => page.locator("#panel-view-all").click(),
    id: "panel-view-all",
  },
  {
    name: "calculation example",
    open: (page) => page.locator("#snapshot-note").click(),
    id: "snapshot-note",
  },
  {
    name: "balance details",
    open: (page) => page.locator("#balances-button").click(),
    id: "balances-button",
    details: true,
  },
];

const insideModal = (page) =>
  page.evaluate(() => !!document.activeElement.closest(".pcal-modal"));

async function wrapsAndReturns(page, opener) {
  await check(
    `${opener.name}: focus moves into the dialog on open`,
    async () => ({ pass: await insideModal(page) })
  );
  let escaped = false;
  for (let step = 0; step < 30 && !escaped; step++) {
    await page.keyboard.press("Tab");
    escaped = !(await insideModal(page));
  }
  await check(
    `${opener.name}: Tab wraps and never leaves the dialog`,
    async () => ({ pass: !escaped })
  );
  await page.keyboard.press("Shift+Tab");
  await check(
    `${opener.name}: Shift+Tab stays inside the dialog`,
    async () => ({ pass: await insideModal(page) })
  );
  await page.keyboard.press("Escape");
  await check(
    `${opener.name}: Escape returns focus to the opener`,
    async () => {
      const now = await active(page);
      return { pass: now.id === opener.id, detail: JSON.stringify(now) };
    }
  );
}

function modalFocus() {
  for (const opener of OPENERS)
    scenario(
      `keys: ${opener.name} traps Tab and returns focus`,
      options,
      async ({ page }) => {
        if (opener.details) await openDetails(page);
        await opener.open(page);
        await wrapsAndReturns(page, opener);
      }
    );
}

function swipeThreshold() {
  scenario(
    "keys: a swipe under 50 px does nothing, 50 px or more moves a week",
    { size: WIDTHS.phone },
    async ({ page }) => {
      await swipe(page, 300, 270);
      await quiet(page);
      await check("A 30 px swipe leaves the selected date alone", async () =>
        includes(await stat(page, "#selected-date-heading"), "11 Nov")
      );
      await swipe(page, 300, 200);
      await check("A 100 px swipe moves one week", async () =>
        includes(await stat(page, "#selected-date-heading"), "18 Nov")
      );
    }
  );
}

function register() {
  gridKeys();
  escapeOrder();
  modalFocus();
  swipeThreshold();
}

module.exports = { register };
