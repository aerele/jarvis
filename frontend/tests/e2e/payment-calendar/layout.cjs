// A9: six widths (plus 768) in light and dark. Nothing may scroll sideways; the regions are
// measured in their default view, in list view, with the picker open and with a modal open.
// AC2 (phone overlays) and AC3 (picker edges) are measured here too.
const { scenario } = require("./scenario.cjs");
const { expect } = require("@playwright/test");
const { check, equal } = require("./checks.cjs");
const { noOverflow, OVERFLOW_TARGETS, box, style } = require("./probe.cjs");
const { WIDTHS } = require("./boot.cjs");
const { quiet, modal } = require("./flows.cjs");

const PHONE_MAX = 540;
const tap = (page, locator, size) =>
  size.touch ? locator.tap() : locator.click();
// The phone month grid is its own scroll area (like the template), so it is measured elsewhere.
const targets = (size, view) =>
  OVERFLOW_TARGETS.filter(
    (item) =>
      !(size.width <= PHONE_MAX && view === "month" && item === '[role="grid"]')
  );

async function measureViews(page, name, size) {
  await check(`${name}: default view has no overflow`, () =>
    noOverflow(page, targets(size, "default"))
  );
  await tap(page, page.locator("#list-button"), size);
  await check(`${name}: list view has no overflow`, () =>
    noOverflow(page, targets(size, "list"))
  );
  await tap(page, page.locator("#accounts-trigger"), size);
  await check(
    `${name}: account picker has no overflow and stays in the viewport`,
    async () => {
      const found = await noOverflow(page);
      const rect = await box(page.locator("#accounts-menu"));
      const inside = rect.x >= 0 && rect.x + rect.width <= size.width;
      return {
        pass: found.pass && inside,
        detail: `${found.detail} picker x ${rect.x} w ${rect.width}`,
      };
    }
  );
  await tap(page, page.locator("#accounts-cancel"), size);
  await tap(page, page.locator("#panel-view-all"), size);
  await check(`${name}: full-details modal has no overflow`, async () => {
    const found = await noOverflow(page);
    const rect = await box(modal(page));
    return {
      pass: found.pass && rect.x >= 0 && rect.x + rect.width <= size.width,
      detail: `${found.detail} modal x ${rect.x} w ${rect.width}`,
    };
  });
}

function widthMatrix() {
  for (const [label, size] of Object.entries(WIDTHS))
    for (const theme of ["light", "dark"]) {
      const name = `${label} ${size.width}x${size.height} ${theme}`;
      scenario(
        `layout: ${name}`,
        { size, theme, scheme: theme },
        async ({ page }) => measureViews(page, name, size)
      );
    }
}

// AC2: at 390x844 the full-details rows are cards and the overlays use the phone padding.
function phoneOverlays() {
  scenario(
    "layout: phone overlays use phone padding and cards (AC2)",
    { size: WIDTHS.phone },
    async ({ page }) => {
      await page.locator("#panel-view-all").tap();
      await check(
        "Phone full details render cards, not table rows",
        async () => {
          const row = await style(
            page.locator(".detail-table tbody tr").first(),
            "display"
          );
          const head = await style(
            page.locator(".detail-table thead"),
            "display"
          );
          return {
            pass: row !== "table-row" && head === "none",
            detail: `(row ${row}, thead ${head})`,
          };
        }
      );
      await check("Phone details overlay uses the 12 px padding", async () =>
        equal(
          await style(page.locator(".pcal-backdrop"), "paddingLeft"),
          "12px"
        )
      );
      await page.locator("#close-details").tap();
      await page.locator("#account-balances").tap();
      await check("Phone balances overlay uses the 12 px padding", async () =>
        equal(
          await style(page.locator(".pcal-backdrop"), "paddingLeft"),
          "12px"
        )
      );
    }
  );
  scenario(
    "layout: desktop overlays use the 24 px padding (AC2 control)",
    { size: WIDTHS.desktop },
    async ({ page }) => {
      await page.locator("#account-balances").click();
      await check("Desktop balances overlay uses the 24 px padding", async () =>
        equal(
          await style(page.locator(".pcal-backdrop"), "paddingLeft"),
          "24px"
        )
      );
    }
  );
}

// A Tab-focused input inside a teleported overlay shows only the template ring (2 px outline, no shadow).
function overlayFocusRing() {
  scenario(
    "layout: a focused control in an overlay shows only the template ring (AC2)",
    { size: WIDTHS.phone },
    async ({ page }) => {
      await page.locator("#panel-view-all").tap();
      await page.locator("#record-sort").focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await check(
        "Sort select in the overlay has the 2 px template outline",
        async () => {
          const select = page.locator("#record-sort");
          const [kind, width, color, shadow] = await Promise.all(
            ["outlineStyle", "outlineWidth", "outlineColor", "boxShadow"].map(
              (key) => style(select, key)
            )
          );
          // The template ring: 2 px solid #276c57, no shadow ring on top of it.
          const pass =
            kind === "solid" &&
            width === "2px" &&
            color === "rgb(39, 108, 87)" &&
            shadow === "none";
          return {
            pass,
            detail: `(outline ${kind} ${width} ${color}, shadow ${shadow})`,
          };
        }
      );
    }
  );
}

// AC3: picker edges match the accounts filter at a container of 540 px or less; 9 px inset above.
async function pickerEdges(page, expectedInset) {
  await page.locator("#accounts-trigger").click();
  const filter = await box(page.locator(".accounts-filter"));
  const picker = await box(page.locator("#accounts-menu"));
  const left = picker.x - filter.x;
  const right = filter.x + filter.width - (picker.x + picker.width);
  return {
    pass:
      Math.abs(left - expectedInset) <= 1 &&
      Math.abs(right - expectedInset) <= 1,
    detail: `(left inset ${left}, right inset ${right}, wanted ${expectedInset})`,
  };
}

// A viewport whose in-page container is exactly `target` px wide.
async function fitContainer(page, target) {
  const read = () =>
    page.evaluate(() => ({
      view: innerWidth,
      layout: document.querySelector(".pcal-layout").clientWidth,
    }));
  const now = await read();
  await page.setViewportSize({
    width: now.view - now.layout + target,
    height: 900,
  });
  // Wait until the container width stops changing (the resize and its transitions are done).
  // It need not equal `target`: the page's own padding and max width decide the final size.
  let last = -1;
  await expect
    .poll(
      async () => {
        const width = (await read()).layout;
        const steady = width === last;
        last = width;
        return steady;
      },
      { timeout: 3000, intervals: [100] }
    )
    .toBe(true);
  await quiet(page);
}

function pickerAlignment() {
  scenario(
    "layout: picker edges at a 358 px container (AC3)",
    { size: WIDTHS.phone },
    async ({ page }) => {
      await check(
        "At a 358 px container the picker edges match the filter edges",
        () => pickerEdges(page, 0)
      );
    }
  );
  scenario(
    "layout: picker keeps the 9 px inset at a 600 px container (AC3)",
    { size: WIDTHS.desktop },
    async ({ page }) => {
      await fitContainer(page, 600);
      await check("At a 600 px container the picker keeps the 9 px inset", () =>
        pickerEdges(page, 9)
      );
    }
  );
}

function register() {
  widthMatrix();
  phoneOverlays();
  overlayFocusRing();
  pickerAlignment();
}

module.exports = { register };
