// Template theme checks (check-theme.mjs): the switch, entry modes, user menu, blocked storage.
// The toggle writes jarvis-theme and calls set_user_theme (decision 2); nothing else may.
const { scenario } = require("./scenario.cjs");
const { check, equal } = require("./checks.cjs");
const { DARK, dark, is } = require("./themeKit.cjs");

function toggling() {
  scenario(
    "theme: the toggle writes the Jarvis theme and nothing else does",
    { size: { width: 1632, height: 1100 }, theme: "system", scheme: "dark" },
    async ({ page, mock }) => {
      await check("Loading the page writes no theme", async () =>
        equal(mock.themeWrites.length, 0)
      );
      await page.locator("#theme-toggle").click();
      await check(
        "Theme toggle switches to light and updates its label",
        async () => {
          const light = !(await dark(page));
          return light
            ? equal(
                await page.locator("#theme-toggle").getAttribute("aria-label"),
                "Switch to dark mode"
              )
            : { pass: false, detail: "(still dark)" };
        }
      );
      await is(
        page,
        "Light calendar retains white cells",
        '[data-cell-date="2026-11-12"]',
        "rgb(255, 255, 255)"
      );
      await check(
        "The toggle writes jarvis-theme and calls set_user_theme",
        async () => {
          const stored = await page.evaluate(() =>
            localStorage.getItem("jarvis-theme")
          );
          return {
            pass: stored === "light" && mock.themeWrites.join() === "light",
            detail: `(stored ${stored}, saved ${mock.themeWrites})`,
          };
        }
      );
      await page.reload();
      await check("Standalone theme choice survives reload", async () => ({
        pass: !(await dark(page)),
      }));
      await page.locator("#theme-toggle").click();
      await check("Theme toggle switches back to dark", async () => {
        const state = await dark(page);
        return state
          ? equal(
              await page.locator("#theme-toggle").getAttribute("aria-pressed"),
              "true"
            )
          : { pass: false };
      });
      await check(
        "Each click wrote the theme once and nothing else wrote it",
        async () => equal(mock.themeWrites.join(), "light,dark")
      );
      await page.emulateMedia({ colorScheme: "light" });
      await check(
        "Explicit theme choice takes priority over system changes",
        async () => ({ pass: await dark(page) })
      );
    }
  );
}

function entryModes() {
  scenario(
    "theme: a fresh system-light session stays light and follows a system change",
    { theme: "system", scheme: "light" },
    async ({ page }) => {
      await check("Fresh system-light session keeps light mode", async () => ({
        pass: !(await dark(page)),
      }));
      await page.emulateMedia({ colorScheme: "dark" });
      await check(
        "Automatic theme follows a system theme change",
        async () => ({ pass: await dark(page) })
      );
    }
  );
  scenario(
    "theme: the Jarvis desk setting beats a light system",
    { theme: "dark", scheme: "light" },
    async ({ page }) => {
      await check(
        "Jarvis desk dark setting takes priority over a light system",
        async () => ({ pass: await dark(page) })
      );
    }
  );
}

function userMenuSync() {
  scenario(
    "theme: the switch follows a change made from the user menu",
    { theme: "light", scheme: "light" },
    async ({ page }) => {
      await page.locator("aside, nav").first().waitFor();
      await page.getByRole("button", { name: "Jarvis menu" }).click();
      await page.getByText("Change theme", { exact: true }).click();
      await check(
        "Host theme changes update the page colours and the toggle",
        async () => {
          const state = await dark(page);
          if (!state) return { pass: false, detail: "(theme did not change)" };
          return equal(
            await page.locator("#theme-toggle").getAttribute("aria-pressed"),
            "true"
          );
        }
      );
      await is(
        page,
        "The calendar cells follow the user-menu change",
        '[data-cell-date="2026-11-12"]',
        DARK.raised
      );
    }
  );
}

function blockedStorage() {
  // Private mode: reads work, writes throw. (Blocking every read breaks the whole SPA, not this page.)
  const blocked = () =>
    (Storage.prototype.setItem = () => {
      throw new DOMException("Blocked", "SecurityError");
    });
  scenario(
    "theme: blocked storage writes do not break theme selection",
    {
      theme: "system",
      scheme: "dark",
      blockStorage: blocked,
      allowErrors: /SecurityError: Blocked/,
    },
    async ({ page, mock }) => {
      await page.locator("#theme-toggle").click();
      await check(
        "Blocked storage does not break theme selection",
        async () => ({ pass: !(await dark(page)) })
      );
      await check(
        "The choice is still saved to the user (set_user_theme)",
        async () => equal(mock.themeWrites.join(), "light")
      );
    }
  );
}

function register() {
  toggling();
  entryModes();
  userMenuSync();
  blockedStorage();
}

module.exports = { register };
