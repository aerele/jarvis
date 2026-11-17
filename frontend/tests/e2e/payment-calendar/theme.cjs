// Template theme checks (check-theme.mjs): dark surfaces on desktop and phone.
const { scenario } = require("./scenario.cjs");
const { check, equal, includes } = require("./checks.cjs");
const { noOverflow, contrastOf } = require("./probe.cjs");
const { WIDTHS } = require("./boot.cjs");
const { openDetails, saveFloor, stat } = require("./flows.cjs");
const { DARK, dark, dim, color, is } = require("./themeKit.cjs");

async function palette(page) {
  await check(
    "System dark mode selects the dark palette on entry",
    async () => ({ pass: await dark(page) })
  );
  await is(
    page,
    "Dark canvas uses the Jarvis surface colour",
    ".pcal-root",
    DARK.surface
  );
  await is(
    page,
    "Dark cards use the Jarvis raised surface",
    ".headline-card",
    DARK.raised
  );
  await is(
    page,
    "Dark calendar cells stay neutral",
    '[data-cell-date="2026-11-12"]',
    DARK.raised
  );
  await is(
    page,
    "Primary text uses the Jarvis dark text colour",
    "h1",
    DARK.text,
    "color"
  );
  await is(
    page,
    "Links use the Jarvis dark link colour",
    "#account-balances",
    DARK.link,
    "color"
  );
  await is(
    page,
    "Green cash figures use the Jarvis dark green",
    "#lowest-cushion",
    DARK.green,
    "color"
  );
  await is(
    page,
    "Immediate dues use the Jarvis dark amber",
    "#immediate-dues",
    DARK.amber,
    "color"
  );
  await is(
    page,
    "Negative net pills use the Jarvis dark red",
    ".day-net.negative",
    DARK.red,
    "color"
  );
  await is(
    page,
    "Native controls use a dark colour scheme",
    "html",
    "dark",
    "colorScheme"
  );
  await check("Desktop overview has no light panels", () => dim(page));
  await check("Desktop dark layout has no horizontal overflow", () =>
    noOverflow(page)
  );
  for (const selector of [
    "h1",
    "#lowest-cushion",
    "#immediate-dues",
    "#account-balances",
    ".headline-note",
    ".day-net.negative",
  ])
    await check(
      `Dark text contrast is at least 4.5:1: ${selector}`,
      async () => {
        const ratio = await contrastOf(page.locator(selector));
        return { pass: ratio >= 4.5, detail: `(${ratio.toFixed(2)}:1)` };
      }
    );
}

async function pickerAndBalances(page) {
  await page.locator("#accounts-trigger").click();
  await check(
    "Unchecked account checkboxes are not bright white in dark mode",
    async () => {
      const bg = await color(page, ".account-option input:not(:checked)");
      const light = bg
        .match(/\d+/g)
        .slice(0, 3)
        .every((n) => +n > 150);
      return { pass: !light, detail: `(${bg})` };
    }
  );
  await is(
    page,
    "Account picker uses a dark panel",
    "#accounts-menu",
    DARK.raised
  );
  await is(
    page,
    "Account search uses a dark input",
    "#accounts-search",
    DARK.input
  );
  await check("Large account list has no light rows", () => dim(page));
  await page.locator("#accounts-search").fill("9016");
  await page
    .locator('.account-option input[aria-label="ICICI Savings"]')
    .check();
  await page.locator("#accounts-cancel").click();
  await check(
    "Account Cancel keeps the applied balances in dark mode",
    async () => includes(await stat(page, "#header-book-cash"), "2,46,850.00")
  );
  await page.locator("#account-balances").click();
  await is(
    page,
    "Account balance modal uses a dark surface",
    ".pcal-modal",
    DARK.raised
  );
  await check("Account balance rows remain dark", () => dim(page));
  await check("Account balance amount has readable contrast", async () => ({
    pass: (await contrastOf(page.locator(".account-balance-row b"))) >= 4.5,
  }));
  await page.keyboard.press("Escape");
}

async function invoiceDetails(page) {
  await page.locator("#panel-view-all").click();
  await is(
    page,
    "Full invoice view has a dark surface",
    ".pcal-modal",
    DARK.raised
  );
  await is(
    page,
    "Invoice table header uses a dark surface",
    ".detail-table th",
    DARK.input
  );
  await check("Full invoice view has no light rows", () => dim(page));
  await page.locator('[data-sort="remaining"]').click();
  await check("Dark invoice view keeps balance sorting", async () =>
    includes(await stat(page, ".detail-table tbody tr"), "PINV-2026-0151")
  );
  await page.keyboard.press("Escape");
}

async function cashDetailsAndRisk(page) {
  await openDetails(page);
  await check("Expanded cash details have no light panels", () => dim(page));
  await is(
    page,
    "Dynamic closing amount uses the dark green after rendering",
    "#stat-closing",
    DARK.green,
    "color"
  );
  // The golden floors stop at 200000, so the below-minimum day is 19 Nov (closing 167174).
  await saveFloor(page, 200000);
  await openDetails(page);
  await page.locator("#risk-jump").click();
  await is(
    page,
    "Dynamic below-floor closing uses dark amber",
    "#stat-closing",
    DARK.amber,
    "color"
  );
  await is(
    page,
    "Deficit KPI uses dark red",
    "#lowest-cushion",
    DARK.red,
    "color"
  );
  await is(
    page,
    "Floor warning uses the dark amber fill",
    "#risk-alert",
    DARK.amberBg
  );
}

function darkDesktop() {
  scenario(
    "theme: dark desktop palette and surfaces",
    { size: { width: 1632, height: 1100 }, theme: "system", scheme: "dark" },
    async ({ page }) => {
      await palette(page);
      await pickerAndBalances(page);
      await invoiceDetails(page);
      await cashDetailsAndRisk(page);
      await page.locator("#company-trigger").click();
      await is(
        page,
        "Company picker uses the dark surface",
        "#company-menu",
        DARK.raised
      );
    }
  );
}

async function phoneDark(page) {
  await check("Phone starts in dark mode without page overflow", async () =>
    (await dark(page))
      ? noOverflow(page)
      : { pass: false, detail: "(not dark)" }
  );
  await check("Phone agenda has no light surfaces", () => dim(page));
  await page.locator("#trough-jump").tap();
  await check("Phone trough jump keeps selected state and focus", async () => ({
    pass: await page
      .locator('[data-week-date="2026-11-23"]')
      .evaluate(
        (el) =>
          el === document.activeElement &&
          el.getAttribute("aria-pressed") === "true"
      ),
  }));
  await is(
    page,
    "Phone trough outline uses the Jarvis dark link colour",
    ".trough-target",
    DARK.link,
    "outlineColor"
  );
  await page.locator("#accounts-trigger").tap();
  await check("Dark phone account picker stays in the viewport", async () => {
    const rect = await page.locator("#accounts-menu").boundingBox();
    return {
      pass: rect.x >= 0 && rect.x + rect.width <= 390,
      detail: JSON.stringify(rect),
    };
  });
  await page.locator("#accounts-cancel").tap();
  await page.locator("#account-balances").tap();
  await check("Phone account modal keeps dark surfaces", () => dim(page));
  await page.keyboard.press("Escape");
}

function darkPhone() {
  scenario(
    "theme: dark phone",
    { size: WIDTHS.phone, theme: "system", scheme: "dark" },
    async ({ page }) => {
      await phoneDark(page);
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.locator('[data-obligation="immediate"]').tap();
      await check("Dark Immediate Dues focuses its visible ledger", async () =>
        page.evaluate(() => {
          const el = document.getElementById("ledger-heading"),
            rect = el.getBoundingClientRect();
          return (
            el === document.activeElement &&
            rect.top >= 0 &&
            rect.bottom <= innerHeight
          );
        })
      );
      await page.locator("#panel-view-all").tap();
      await check("Phone invoice details keep dark surfaces", () => dim(page));
      await page.keyboard.press("Escape");
      await page.setViewportSize({ width: 320, height: 740 });
      await check("Theme toggle fits the 320 px layout", () =>
        noOverflow(page)
      );
      await check("Read-only scope is preserved", async () =>
        equal(
          await page
            .locator("[data-pick],[data-erp],#batch-bar,#modal-batch")
            .count(),
          0
        )
      );
    }
  );
}

function register() {
  darkDesktop();
  darkPhone();
}

module.exports = { register };
