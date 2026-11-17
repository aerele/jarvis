// A test that opens its own calendar context, runs the body, and checks for page errors.
const { test } = require("@playwright/test");
const { openCalendar } = require("./boot.cjs");
const { check } = require("./checks.cjs");

function scenario(title, options, body) {
  test(title, async ({ browser, baseURL }) => {
    const app = await openCalendar(browser, baseURL, options);
    try {
      await body(app);
      // allowErrors: errors a test causes on purpose (blocked storage) are not page defects.
      const errors = app.errors.filter(
        (text) => !options.allowErrors?.test(text)
      );
      await check(
        `No page or console errors (${title})`,
        () => ({
          pass: errors.length === 0,
          detail: errors.slice(0, 3).join(" | "),
        }),
        { timeout: 0 }
      );
    } finally {
      await app.context.close();
    }
  });
}

module.exports = { scenario };
