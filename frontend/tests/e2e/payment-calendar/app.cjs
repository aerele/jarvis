// A13 (Back to chat, fromChat focus, ?date=, route guard), AE1 (site date, not browser date).
const { scenario } = require("./scenario.cjs");
const { check, equal, includes } = require("./checks.cjs");
const { active } = require("./probe.cjs");
const { WIDTHS } = require("./boot.cjs");
const { stat } = require("./flows.cjs");

function deepLink() {
  scenario(
    "app: ?date= selects that date",
    { query: "&date=2026-11-18" },
    async ({ page, mock }) => {
      await check("?date=2026-11-18 selects 18 Nov", async () =>
        includes(await stat(page, "#selected-date-heading"), "18 Nov")
      );
      await check("?date= asks for the November snapshot", async () => ({
        pass: mock.log.some(
          (entry) =>
            entry.method === "request_snapshot" &&
            entry.args.month === "2026-11"
        ),
      }));
    }
  );
}

function routeGuard() {
  scenario(
    "app: the route guard sends a viewer without the flag to Saved dashboards",
    { flags: { can_view_payment_calendar: false }, ready: false },
    async ({ page, mock }) => {
      await page.waitForURL(/\/jarvis\/dashboards(\?|#|$)/, { timeout: 15000 });
      await check("Guarded visit lands on the Saved tab", async () =>
        equal(new URL(page.url()).hash, "#saved")
      );
      await check("The guarded page made no calendar request", async () =>
        equal(mock.calls().length, 0)
      );
    }
  );
}

function backToChat() {
  scenario(
    "app: hand-off from chat shows Back to chat and focuses the title",
    { ready: false },
    async ({ page }) => {
      await page.evaluate(() =>
        window.history.replaceState(
          { ...window.history.state, chatConv: "CONV-1", fromChat: true },
          ""
        )
      );
      await page.reload();
      await page.locator("#lowest-cushion").waitFor({ timeout: 30000 });
      await check("Back to chat is shown", async () => ({
        pass: await page
          .getByRole("button", { name: "Back to chat" })
          .isVisible(),
      }));
      await check(
        "The title takes focus on arrival from chat (fromChat)",
        async () => equal((await active(page)).id, "pcal-title")
      );
      await page.getByRole("button", { name: "Back to chat" }).click();
      await check("Back to chat opens that conversation", async () =>
        includes(new URL(page.url()).pathname, "/jarvis/c/CONV-1")
      );
    }
  );
}

// AE1: the browser is in Los Angeles (10 Nov locally); the site date is 11 Nov.
function siteDate() {
  scenario(
    "app: Today comes from the site date, not the browser (AE1)",
    { size: WIDTHS.desktop, timezoneId: "America/Los_Angeles" },
    async ({ page }) => {
      await check("Premise: the browser date is 10 Nov", async () => {
        const local = await page.evaluate(() =>
          new Date().toLocaleDateString("en-CA")
        );
        return equal(local, "2026-11-10");
      });
      await check("The Today marker is on 11 Nov", async () =>
        equal(
          await page.locator(".day.today").getAttribute("data-cell-date"),
          "2026-11-11"
        )
      );
      await check(
        "The selected date and 'Due on' label say 11 Nov",
        async () => {
          const heading = includes(
            await stat(page, "#selected-date-heading"),
            "11 Nov"
          );
          return heading.pass
            ? equal(await stat(page, "#payable-label"), "Due on 11 Nov")
            : heading;
        }
      );
      await page.locator('[data-date="2026-11-20"]').click();
      await page.locator("#today-button").click();
      await check("Today goes back to 11 Nov", async () =>
        includes(await stat(page, "#selected-date-heading"), "11 Nov")
      );
    }
  );
}

// Real copy replaces the template's "Sample ..." toasts and fake Refresh.
function toastsAndRefresh() {
  scenario(
    "app: toasts use real copy and Refresh reads a new snapshot",
    { size: WIDTHS.desktop },
    async ({ page, mock }) => {
      await page.locator("#floor-input").fill("140000");
      await page.locator("#floor-save").click();
      await check(
        "Saving the minimum shows 'Minimum cash balance saved.'",
        async () =>
          equal(await stat(page, ".pcal-toast"), "Minimum cash balance saved.")
      );
      const mark = mock.mark();
      await page.locator("#refresh-button").click();
      await check(
        "Refresh requests one new snapshot without touching the floor or preferences",
        async () => ({
          pass:
            mock.count("request_snapshot", mark) === 1 &&
            mock.count("configure_floor", mark) === 0 &&
            mock.count("set_preferences", mark) === 0,
          detail: `(${mock.calls(mark)})`,
        })
      );
      await check("Refresh keeps the page showing values", async () =>
        equal(await stat(page, "#lowest-cushion"), "₹7,614.00")
      );
    }
  );
}

function register() {
  toastsAndRefresh();
  deepLink();
  routeGuard();
  backToChat();
  siteDate();
}

module.exports = { register };
