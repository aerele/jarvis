// Template checks 1-25: what the page shows on first load (Wednesday 11 Nov 2026, 10:42 IST).
const { scenario } = require("./scenario.cjs");
const { check, equal, includes, textOf } = require("./checks.cjs");
const { noOverflow, box, style } = require("./probe.cjs");
const { WIDTHS } = require("./boot.cjs");

const cell = (page, date) => page.locator(`[data-cell-date="${date}"]`);

async function calendarFacts({ page, errors }) {
  await check(
    "Desktop starts without script errors",
    () => errors.length === 0
  );
  await check("November has five weeks and all 30 dates", async () => ({
    pass:
      (await page.locator(".week").count()) === 5 &&
      (await page.locator('.day[data-cell-date^="2026-11"]').count()) === 30,
  }));
  await check("Cell net is separate from projected closing cash", async () =>
    includes(
      await textOf(cell(page, "2026-11-11").locator(".day-net")),
      "+₹44.2k"
    )
  );
  await check("Unpaid cells use a neutral white background", async () =>
    equal(
      await style(cell(page, "2026-11-12"), "backgroundColor"),
      "rgb(255, 255, 255)"
    )
  );
  await check("Zero activity uses a quiet dash", async () =>
    equal(await textOf(cell(page, "2026-11-25").locator(".quiet-day")), "—")
  );
  await check("Desktop has no horizontal overflow", () => noOverflow(page));
}

async function selectedDateFacts({ page }) {
  const text = (selector) => textOf(page.locator(selector));
  await check("Selected date names the weekday and date", async () =>
    includes(await text("#selected-date-heading"), ["Wednesday", "11 Nov"])
  );
  await check("Payment obligation is Due on 11 Nov", async () =>
    equal(await text("#payable-label"), "Due on 11 Nov")
  );
  await check(
    "Snapshot obligation has its time and all three components",
    async () =>
      includes(await text(".snapshot-card"), [
        "Total payable now",
        "₹69,548.00",
        "Due today",
        "₹42,048.00",
        "₹22,500.00",
        "₹5,000.00",
        "10:42 IST",
      ])
  );
  await check("Forecast states its collection assumption", async () =>
    equal(await text("#closing-note"), "Assumes ₹86,250.00 is collected today.")
  );
  await check("Forecast explains margin above minimum", async () =>
    equal(await text("#floor-margin"), "₹2,13,552.00 above minimum")
  );
  await check("Detailed financial figures are closed by default", async () => ({
    pass: !(await page.locator("#financial-details").evaluate((el) => el.open)),
  }));
}

async function headlineFacts({ page }) {
  const text = (selector) => textOf(page.locator(selector));
  await check("There are exactly three visible headline cards", async () => ({
    pass:
      (await page.locator(".headline-card").count()) === 3 &&
      (await page.locator(".headline-card:visible").count()) === 3,
  }));
  await check("Lowest cushion uses the true 30-day trough", async () => {
    const cushion = equal(await text("#lowest-cushion"), "₹97,614.00");
    return cushion.pass
      ? includes(await text("#lowest-note"), "23 Nov")
      : cushion;
  });
  await check("The 30-day forecast range is explicit", async () =>
    includes(await text("#lowest-card"), ["11 Nov–10 Dec", "30 days"])
  );
  await check("Default cushion is safe and green", async () => {
    const state = await page.locator("#lowest-card").getAttribute("data-state");
    return state === "green"
      ? equal(await text("#lowest-status"), "Safe")
      : equal(state, "green");
  });
  await check(
    "Month net is complete November inflows minus scheduled outflows",
    async () => {
      const net = equal(await text("#month-flow"), "+₹3,29,702.00");
      return net.pass
        ? equal(await text("#month-flow-scope"), "November 2026")
        : net;
    }
  );
  await check("Immediate dues exclude the 42048 scheduled today", async () =>
    equal(await text("#immediate-dues"), "₹27,500.00")
  );
}

async function starFacts({ page }) {
  await check(
    "An exactly 50 percent invoice share is not a hotspot",
    async () =>
      equal(await cell(page, "2026-11-17").locator(".hotspot").count(), 0)
  );
  await check(
    "A dominant supplier invoice has a concentration marker",
    async () => {
      const star = cell(page, "2026-11-19").locator(".hotspot");
      if ((await star.count()) !== 1) return equal(await star.count(), 1);
      return includes(await star.getAttribute("title"), [
        "₹1,02,236.00",
        "100%",
      ]);
    }
  );
  await check(
    "Mixed due rows without a dominant invoice have no marker",
    async () =>
      equal(await cell(page, "2026-11-11").locator(".hotspot").count(), 0)
  );
  await check(
    "Star has at least three extra pixels before the status marker",
    async () => {
      const margin = parseFloat(
        await style(
          cell(page, "2026-11-19").locator(".hotspot"),
          "marginInlineEnd"
        )
      );
      return { pass: margin >= 3, detail: `(margin ${margin}px)` };
    }
  );
}

async function layoutFacts({ page }) {
  await check("Account picker uses the Choose accounts label", async () =>
    equal(await textOf(page.locator("#accounts-trigger")), "Choose accounts")
  );
  await check(
    "Next payments fills the area directly below the calendar",
    async () => {
      const calendar = await box(page.locator(".pcal-cal"));
      const next = await box(page.locator(".next-panel"));
      const gap = next.y - calendar.y - calendar.height;
      return { pass: gap >= 0 && gap <= 16, detail: `(gap ${gap}px)` };
    }
  );
  await check(
    "Invoice panel has no payment selection or ERP controls",
    async () =>
      equal(
        await page
          .locator("#day-panel [data-pick],#day-panel [data-erp]")
          .count(),
        0
      )
  );
}

function register() {
  scenario(
    "overview: first load at 1632x1000 matches the template figures",
    { size: WIDTHS.desktop },
    async (app) => {
      await calendarFacts(app);
      await selectedDateFacts(app);
      await headlineFacts(app);
      await starFacts(app);
      await layoutFacts(app);
    }
  );
}

module.exports = { register };
