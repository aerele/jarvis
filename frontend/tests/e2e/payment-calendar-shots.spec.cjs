// Paired screenshots of the live page and the design template. Skipped unless SHOTS is set;
// PCAL_TEMPLATE must name the template file:
//   SHOTS=/some/folder PCAL_TEMPLATE=/path/to/calendar-proposal.html npx playwright test -c playwright.payment-calendar.config.cjs payment-calendar-shots
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("@playwright/test");
const { pair, SETS } = require("./payment-calendar/shots.cjs");

const dir = process.env.SHOTS;
test.describe.configure({ mode: "serial" });
const meta = {};

for (const { size, shots } of SETS)
  for (const theme of ["light", "dark"])
    for (const shot of shots)
      test(`shots: ${shot.name} ${theme}`, async ({ browser, baseURL }) => {
        test.skip(!dir, "set SHOTS to a folder to capture screenshots");
        fs.mkdirSync(dir, { recursive: true });
        try {
          await pair({ browser, baseURL }, { size, theme, shot, dir, meta });
        } catch (error) {
          if (!shot.optional) throw error;
          test.skip(
            true,
            `${shot.name} not captured: ${String(error.message).split("\n")[0]}`
          );
        } finally {
          fs.writeFileSync(
            path.join(dir, "shots-meta.json"),
            JSON.stringify(meta, null, 2)
          );
        }
      });
