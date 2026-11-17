// Template checks 110-112: the template rebuilt its HTML with other sample numbers. The app is
// fed by the server, so each variant edits the status the mock serves (a server-side value).
// Check 113 (same-invoice terms before the concentration test) is a server rule: PR 1 tests it.
const { scenario } = require("./scenario.cjs");
const { check, equal } = require("./checks.cjs");
const { stat } = require("./flows.cjs");

const monthNet = (net) => (mock) =>
  mock.editStatus((status) => {
    status.projection.month_totals = { ...status.projection.month_totals, net };
  });

function monthNetStates() {
  scenario(
    "variants: a negative month net is a signed deficit",
    { setup: monthNet("-600898.00") },
    async ({ page }) => {
      await check(
        "Negative month net uses a signed deficit and coral state",
        async () => {
          const value = equal(await stat(page, "#month-flow"), "−₹6,00,898.00");
          return value.pass
            ? equal(
                await page
                  .locator("#month-flow-card")
                  .getAttribute("data-state"),
                "negative"
              )
            : value;
        }
      );
    }
  );
  scenario(
    "variants: a zero month net is balanced and neutral",
    { setup: monthNet("0.00") },
    async ({ page }) => {
      await check("Zero month net is balanced and neutral", async () => {
        const value = equal(await stat(page, "#month-flow"), "₹0.00");
        return value.pass
          ? equal(
              await page.locator("#month-flow-card").getAttribute("data-state"),
              "neutral"
            )
          : value;
      });
    }
  );
}

function incompleteWindow() {
  const setup = (mock) =>
    mock.editStatus((status) => {
      status.horizon = {
        ...status.horizon,
        lowest: null,
        forecast_available: false,
      };
    });
  scenario(
    "variants: an incomplete 30-day window cannot claim a safe cushion",
    { setup },
    async ({ page }) => {
      await check(
        "An incomplete 30-day window cannot claim a safe cushion",
        async () => {
          const value = equal(
            await stat(page, "#lowest-cushion"),
            "Unavailable"
          );
          return value.pass
            ? { pass: await page.locator("#trough-jump").isDisabled() }
            : value;
        }
      );
    }
  );
}

function register() {
  monthNetStates();
  incompleteWindow();
}

module.exports = { register };
