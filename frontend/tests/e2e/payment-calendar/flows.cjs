// Small user actions shared by the groups. Each one waits for what it caused.
const { expect } = require("@playwright/test");
const { textOf } = require("./checks.cjs");

const openDetails = async (page) => {
  if (!(await page.locator("#financial-details").evaluate((el) => el.open)))
    await page.locator("#financial-details > summary").click();
};

// Fill the minimum, save, and wait for the new snapshot to replace the headline.
async function saveFloor(page, value) {
  const before = await textOf(page.locator("#lowest-cushion"));
  await page.locator("#floor-input").fill(String(value));
  await page.locator("#floor-save").click();
  await page
    .waitForFunction(
      (old) => {
        const text = document
          .querySelector("#lowest-cushion")
          ?.textContent.trim();
        return (
          !!text &&
          text !== old &&
          !document.querySelector(".headline-value.skeleton")
        );
      },
      before,
      { timeout: 15000 }
    )
    .catch(() => {});
}

const selectDay = (page, date) => page.locator(`[data-date="${date}"]`).click();

const panelRows = (page) => page.locator("#day-panel .ledger-row");

const modal = (page) => page.locator(".pcal-modal");

// The first date row text of the full-details table, once the table has rows.
const firstDetailRow = (page) => page.locator(".detail-table tbody tr").first();

const stat = (page, id) => textOf(page.locator(id));

// A drag of `from` to `to` px on the week strip, as touch events.
const swipe = (page, from, to) =>
  page.locator(".week-strip").evaluate(
    (strip, [start, end]) => {
      const touch = (x) =>
        new Touch({
          identifier: 1,
          target: strip,
          clientX: x,
          clientY: strip.getBoundingClientRect().top + 30,
        });
      strip.dispatchEvent(
        new TouchEvent("touchstart", {
          bubbles: true,
          changedTouches: [touch(start)],
        })
      );
      strip.dispatchEvent(
        new TouchEvent("touchend", {
          bubbles: true,
          changedTouches: [touch(end)],
        })
      );
    },
    [from, to]
  );

// Waits until the page is calm instead of sleeping: no held request on the mock (when there is
// one) and no animation for a few frames in a row. Bounded, so a spinner cannot hold it.
async function quiet(page, mock) {
  if (mock) await expect.poll(() => mock.pending(), { timeout: 8000 }).toBe(0);
  await page.evaluate(async () => {
    let calm = 0;
    for (let frame = 0; frame < 120 && calm < 3; frame++) {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      calm = document.getAnimations().length ? 0 : calm + 1;
    }
    await document.fonts?.ready;
  });
}

module.exports = {
  quiet,
  swipe,
  openDetails,
  saveFloor,
  selectDay,
  panelRows,
  modal,
  firstDetailRow,
  stat,
};
