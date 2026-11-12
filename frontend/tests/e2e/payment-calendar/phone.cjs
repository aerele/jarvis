// Template checks 92-98 and 102-109: the phone layout at 390x844 with touch.
// (99-101, the phone picker and balances, are in accounts.cjs.)
const { scenario } = require("./scenario.cjs");
const { check, equal, includes } = require("./checks.cjs");
const {
  noOverflow,
  box,
  isActive,
  style,
  OVERFLOW_TARGETS,
} = require("./probe.cjs");
const { WIDTHS } = require("./boot.cjs");
const { stat, modal, swipe } = require("./flows.cjs");

const options = { size: WIDTHS.phone };
const weekDay = (page, date) => page.locator(`[data-week-date="${date}"]`);

function overview() {
  scenario(
    "phone: weekly agenda, trough jump, picker-free flows",
    options,
    async ({ page }) => {
      await check("Phone starts with a seven-day weekly agenda", async () => {
        const pressed = equal(
          await page.locator("#week-button").getAttribute("aria-pressed"),
          "true"
        );
        return pressed.pass
          ? equal(await page.locator(".week-day").count(), 7)
          : pressed;
      });
      await check("Phone page has no horizontal overflow", () =>
        noOverflow(page)
      );
      await check("Phone keeps the same three primary KPIs", async () =>
        equal(await page.locator(".headline-card:visible").count(), 3)
      );
      await page.locator("#trough-jump").tap();
      await check(
        "Phone trough action selects the day and shows its supplier panel",
        async () => {
          const pressed = equal(
            await weekDay(page, "2026-11-23").getAttribute("aria-pressed"),
            "true"
          );
          return pressed.pass
            ? includes(await stat(page, "#ledger-heading"), "23 Nov")
            : pressed;
        }
      );
      await check("Phone trough day receives actual focus", async () => ({
        pass: await isActive(weekDay(page, "2026-11-23")),
      }));
      await check(
        "Phone trough day has an explicit focus outline after touch",
        async () => {
          const outlineStyle = await style(
            weekDay(page, "2026-11-23"),
            "outlineStyle"
          );
          const width = parseFloat(
            await style(weekDay(page, "2026-11-23"), "outlineWidth")
          );
          return {
            pass: outlineStyle === "solid" && width >= 2,
            detail: `(${outlineStyle} ${width}px)`,
          };
        }
      );
      await weekDay(page, "2026-11-24").tap();
      await check(
        "Choosing another day clears the old trough target",
        async () => equal(await page.locator(".trough-target").count(), 0)
      );
    }
  );
}

function agenda() {
  scenario(
    "phone: reduced motion, week navigation, swipe",
    options,
    async ({ page }) => {
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.locator('[data-obligation="immediate"]').tap();
      await check(
        "Immediate dues is visible and focused with reduced motion",
        async () =>
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
      await page.emulateMedia({ reducedMotion: "no-preference" });
      await weekDay(page, "2026-11-12").tap();
      await check(
        "Phone day selection updates inline records without a modal",
        async () => {
          const heading = includes(
            await stat(page, "#ledger-heading"),
            "12 Nov"
          );
          return heading.pass
            ? equal(await page.locator(".pcal-backdrop").count(), 0)
            : heading;
        }
      );
      await page.getByRole("button", { name: "Next week", exact: true }).tap();
      await check("Weekly navigation advances the selected date", async () =>
        includes(await stat(page, "#selected-date-heading"), "19 Nov")
      );
      await swipe(page, 300, 120);
      await check("Simulated phone swipe advances one week", async () =>
        includes(await stat(page, "#selected-date-heading"), "26 Nov")
      );
    }
  );
}

function detailsAndViews() {
  scenario(
    "phone: full details cards, list and month views",
    options,
    async ({ page }) => {
      await page.locator("#today-button").tap();
      await page.locator("#panel-view-all").tap();
      await check("Phone full details stay inside the viewport", async () => {
        const rect = await box(modal(page));
        return {
          pass:
            rect.x >= 0 &&
            rect.x + rect.width <= 390 &&
            rect.y >= 0 &&
            rect.y + rect.height <= 844,
          detail: JSON.stringify(rect),
        };
      });
      await check(
        "Phone record cards keep remaining amounts visible",
        async () => ({
          pass: await page
            .locator(".remaining-column")
            .first()
            .evaluate((el) => el.getBoundingClientRect().right <= innerWidth),
        })
      );
      await page.locator("#close-details").tap();
      await page.locator("#list-button").tap();
      await check(
        "Phone list retains all dates and sticky week context",
        async () => {
          const dates = equal(
            await page.locator("[data-list-date]").count(),
            30
          );
          return dates.pass
            ? equal(
                await style(page.locator(".list-week-label"), "position"),
                "sticky"
              )
            : dates;
        }
      );
      await page.locator("#month-button").tap();
      // Like the template, the month grid scrolls inside its own box at phone width (overflow auto).
      await check("Optional phone month view does not widen the page", () =>
        noOverflow(
          page,
          OVERFLOW_TARGETS.filter((item) => item !== '[role="grid"]')
        )
      );
      await check("The phone month grid is its own scroll area", async () =>
        equal(await style(page.locator('[role="grid"]'), "overflowX"), "auto")
      );
    }
  );
}

function register() {
  overview();
  agenda();
  detailsAndViews();
}

module.exports = { register };
