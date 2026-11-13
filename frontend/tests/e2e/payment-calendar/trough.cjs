// Template trough-cleanup checks (check-trough-cleanup.mjs): the "View 23 Nov" jump marks and
// focuses its date, and the marker clears on blur, arrows, date choice and ledger inspection.
const { scenario } = require("./scenario.cjs");
const { check, equal, includes, textOf } = require("./checks.cjs");
const { style, isActive } = require("./probe.cjs");
const { WIDTHS } = require("./boot.cjs");

const SIZES = { Desktop: { width: 1632, height: 1100 }, Phone: WIDTHS.phone };

const outlineVisible = async (locator) => {
  const kind = await style(locator, "outlineStyle");
  const width = parseFloat(await style(locator, "outlineWidth"));
  return {
    pass: kind === "solid" && width >= 2,
    detail: `(${kind} ${width}px)`,
  };
};

async function jumpMarksTheDate(page, label, target) {
  await page.locator("#trough-jump").click();
  await check(
    `${label}: jump selects and focuses the marked date`,
    async () => ({
      pass: await target().evaluate(
        (el) =>
          el === document.activeElement &&
          el.getAttribute("aria-pressed") === "true" &&
          el.classList.contains("trough-target")
      ),
    })
  );
  await check(`${label}: intentional jump has a visible outline`, () =>
    outlineVisible(target())
  );
}

async function leavingTheDate(page, label, target) {
  await page.keyboard.press("Tab");
  await check(`${label}: leaving the date clears the jump class`, async () =>
    equal(await page.locator(".trough-target").count(), 0)
  );
  await check(
    `${label}: blur preserves the selected date and ledger`,
    async () => {
      const pressed = equal(
        await target().getAttribute("aria-pressed"),
        "true"
      );
      return pressed.pass
        ? includes(await textOf(page.locator("#ledger-heading")), "23 Nov")
        : pressed;
    }
  );
  await page.keyboard.press("Shift+Tab");
  await check(
    `${label}: returning by keyboard keeps ordinary focus`,
    async () => {
      const plain = await target().evaluate(
        (el) =>
          el === document.activeElement &&
          !el.classList.contains("trough-target")
      );
      return plain
        ? outlineVisible(target())
        : { pass: false, detail: "(not focused, or still marked)" };
    }
  );
}

async function anotherJumpAndManualChoice(page, label, target, phone) {
  await page.locator("#trough-jump").click();
  await check(
    `${label}: another jump restores exactly one focus target`,
    async () => {
      const one = equal(await page.locator(".trough-target").count(), 1);
      return one.pass ? { pass: await isActive(target()) } : one;
    }
  );
  if (!phone) {
    await page.keyboard.press("ArrowRight");
    await check(
      `${label}: arrow navigation clears the jump marker`,
      async () => {
        const cleared = equal(await page.locator(".trough-target").count(), 0);
        return cleared.pass
          ? { pass: await isActive(page.locator('[data-date="2026-11-24"]')) }
          : cleared;
      }
    );
    await page.keyboard.press("Enter");
  } else await page.locator('[data-week-date="2026-11-24"]').click();
  await check(
    `${label}: manual date selection updates the ledger`,
    async () => {
      const cleared = equal(await page.locator(".trough-target").count(), 0);
      return cleared.pass
        ? includes(await textOf(page.locator("#ledger-heading")), "24 Nov")
        : cleared;
    }
  );
}

async function ledgerInspection(page, label, target) {
  await page.locator("#trough-jump").click();
  await page.locator('#day-panel [data-panel-side="Receivable"]').click();
  await check(
    `${label}: ledger inspection clears the marker without changing the date`,
    async () => {
      const cleared = equal(await page.locator(".trough-target").count(), 0);
      if (!cleared.pass) return cleared;
      const date = equal(await target().getAttribute("aria-pressed"), "true");
      return date.pass
        ? equal(
            await page
              .locator('#day-panel [data-panel-side="Receivable"]')
              .getAttribute("aria-pressed"),
            "true"
          )
        : date;
    }
  );
}

function register() {
  for (const [name, size] of Object.entries(SIZES))
    for (const theme of ["light", "dark"]) {
      const phone = name === "Phone";
      const label = `${name} ${theme}`;
      scenario(
        `trough: ${label}`,
        { size, theme, scheme: theme },
        async ({ page }) => {
          const target = () =>
            page.locator(
              phone
                ? '[data-week-date="2026-11-23"]'
                : '[data-date="2026-11-23"]'
            );
          await jumpMarksTheDate(page, label, target);
          await leavingTheDate(page, label, target);
          await anotherJumpAndManualChoice(page, label, target, phone);
          await ledgerInspection(page, label, target);
        }
      );
    }
}

module.exports = { register };
