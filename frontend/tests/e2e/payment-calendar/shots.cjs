// Paired screenshots: live-<name>-<theme>.png from the app and tpl-<name>-<theme>.png
// from the template at the same CONTENT width (template viewport = app viewport - sidebar).
const path = require("node:path");
const { openCalendar, WIDTHS } = require("./boot.cjs");
const { quiet, saveFloor, swipe } = require("./flows.cjs");

// The design template is not in the repository: the caller must say where it is.
function templatePath() {
  const file = process.env.PCAL_TEMPLATE;
  if (!file) {
    throw new Error(
      "Set PCAL_TEMPLATE to the path of calendar-proposal.html to take paired screenshots."
    );
  }
  return file;
}

// Each shot: what to do on the page, then how to capture. `kind` is "live" or "tpl".
const DESKTOP = [
  { name: "desktop-overview", full: true },
  {
    name: "desktop-deficit",
    full: true,
    run: (page, kind) => floor(page, kind, 150000),
  },
  {
    name: "desktop-picker",
    run: (page) => page.locator("#accounts-trigger").click(),
  },
  {
    name: "desktop-balances",
    run: (page) => page.locator("#account-balances").click(),
  },
  {
    name: "desktop-full-details",
    run: (page) => page.locator("#panel-view-all").click(),
  },
  {
    name: "desktop-cash-details",
    full: true,
    run: (page) => page.locator("#financial-details > summary").click(),
  },
];
const PHONE = [
  { name: "phone-overview", full: true },
  { name: "phone-week-swipe", run: (page) => swipe(page, 300, 120) },
  {
    name: "phone-details",
    run: (page) => page.locator("#panel-view-all").tap(),
  },
];

async function floor(page, kind, value) {
  if (kind === "live") return saveFloor(page, value);
  await page.locator("#floor-input").fill(String(value));
  await page.locator("#floor-save").click();
}

// The live page scrolls inside .pcal-main: grow the viewport so the whole content is captured.
async function fitLive(page, width) {
  const need = await page.evaluate(() => {
    const main = document.querySelector(".pcal-main");
    return main.getBoundingClientRect().top + main.scrollHeight;
  });
  await page.setViewportSize({ width, height: Math.ceil(need) });
  await quiet(page);
}

async function capture(page, kind, shot, file, width) {
  if (kind === "live" && shot.full) await fitLive(page, width);
  await page.screenshot({
    path: file,
    fullPage: kind === "tpl" && !!shot.full,
  });
}

async function sidebarWidth(page) {
  return page.evaluate(() =>
    Math.round(
      document.querySelector(".pcal-root").getBoundingClientRect().left
    )
  );
}

// One live page and one template page per theme and device; every shot reloads them.
async function pair({ browser, baseURL }, { size, theme, shot, dir, meta }) {
  const base = `${shot.name}-${theme}`;
  templatePath(); // fail before any page opens if the template is not named
  const live = await openCalendar(browser, baseURL, {
    size,
    theme,
    scheme: theme,
  });
  try {
    const sidebar = await sidebarWidth(live.page);
    const layout = await live.page.evaluate(
      () => document.querySelector(".pcal-layout").clientWidth
    );
    await shot.run?.(live.page, "live");
    await quiet(live.page, live.mock);
    await capture(
      live.page,
      "live",
      shot,
      path.join(dir, `live-${base}.png`),
      size.width
    );
    meta[base] = {
      liveViewport: `${size.width}x${size.height}`,
      sidebar,
      liveLayoutWidth: layout,
      templateViewport: `${size.width - sidebar}x${size.height}`,
    };
    await templateShot(browser, {
      size,
      theme,
      shot,
      dir,
      sidebar,
      base,
      meta,
    });
  } finally {
    await live.context.close();
  }
}

async function templateShot(
  browser,
  { size, theme, shot, dir, sidebar, base, meta }
) {
  const context = await browser.newContext({
    viewport: { width: size.width - sidebar, height: size.height },
    isMobile: !!size.touch,
    hasTouch: !!size.touch,
    colorScheme: theme,
  });
  try {
    const page = await context.newPage();
    await page.goto(`file://${templatePath()}`);
    await shot.run?.(page, "tpl");
    await quiet(page);
    await capture(
      page,
      "tpl",
      shot,
      path.join(dir, `tpl-${base}.png`),
      size.width - sidebar
    );
    meta[base].templatePage = await page.evaluate(
      () => document.documentElement.scrollWidth
    );
  } finally {
    await context.close();
  }
}

const SETS = [
  { size: WIDTHS.desktop, shots: DESKTOP },
  { size: WIDTHS.phone, shots: PHONE },
];

module.exports = { pair, SETS };
