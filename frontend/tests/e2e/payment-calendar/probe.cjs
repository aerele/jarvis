// Measurements run in the page. Each takes a Playwright page and returns plain data.

// Sideways overflow where it can hide: the document and each named scroll area / region.
const OVERFLOW_TARGETS = [
  ".pcal-main",
  ".pcal-layout",
  '[role="grid"]',
  ".week-strip",
  ".list-view",
  "#accounts-menu",
  ".pcal-modal",
  ".table-wrap",
];

async function overflow(page, targets = OVERFLOW_TARGETS) {
  return page.evaluate((selectors) => {
    const wide = [];
    for (const selector of selectors)
      for (const el of document.querySelectorAll(selector))
        if (el.scrollWidth > el.clientWidth + 1)
          wide.push(`${selector} ${el.scrollWidth}>${el.clientWidth}`);
    const doc = document.documentElement;
    return { doc: doc.scrollWidth, view: window.innerWidth, wide };
  }, targets);
}

async function noOverflow(page, targets) {
  const found = await overflow(page, targets);
  return {
    pass: found.doc <= found.view && !found.wide.length,
    detail: `(document ${found.doc}/${found.view}, wide: ${
      found.wide.join(" | ") || "none"
    })`,
  };
}

// Bounding box of an element (page coordinates).
const box = (locator) => locator.first().boundingBox();

const style = (locator, property) =>
  locator.first().evaluate((el, key) => getComputedStyle(el)[key], property);

// Elements painted with a light background (every channel > 150) that are not allowed to be.
// `allowed` is a CSS selector list of the surfaces the design inverts in dark mode.
async function lightSurfaces(page, allowed) {
  return page.evaluate((exceptions) => {
    const found = [];
    for (const el of document.querySelectorAll(
      ".pcal-root *, .pcal-overlay, .pcal-overlay *"
    )) {
      const rect = el.getBoundingClientRect();
      const css = getComputedStyle(el);
      if (
        !rect.width ||
        !rect.height ||
        css.visibility === "hidden" ||
        el.closest("[hidden]")
      )
        continue;
      const rgb = css.backgroundColor.match(/^rgb\((\d+), (\d+), (\d+)\)$/);
      if (!rgb || !rgb.slice(1).every((n) => +n > 150)) continue;
      // Status dots (6 px) are marks, not surfaces; unchecked checkboxes are checked on their own.
      if (rect.width <= 8 || el.matches(exceptions) || el.closest(exceptions))
        continue;
      found.push(
        `${el.tagName.toLowerCase()}.${String(el.className).split(" ")[0]} ${
          css.backgroundColor
        }`
      );
    }
    return found.slice(0, 6);
  }, allowed);
}

// WCAG contrast of an element's text against the first opaque background above it.
const contrastOf = (locator) =>
  locator.first().evaluate((el) => {
    const parse = (value) =>
      value
        .match(/[\d.]+/g)
        .slice(0, 3)
        .map(Number);
    const lum = (rgb) =>
      rgb
        .map((n) => n / 255)
        .map((n) => (n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4))
        .reduce(
          (sum, n, index) => sum + n * [0.2126, 0.7152, 0.0722][index],
          0
        );
    let parent = el;
    while (
      parent &&
      ["transparent", "rgba(0, 0, 0, 0)"].includes(
        getComputedStyle(parent).backgroundColor
      )
    )
      parent = parent.parentElement;
    const fg = lum(parse(getComputedStyle(el).color));
    const bg = lum(
      parse(getComputedStyle(parent || document.body).backgroundColor)
    );
    return (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
  });

// What has focus: tag, accessible text, and the id.
const active = (page) =>
  page.evaluate(() => {
    const el = document.activeElement;
    return {
      tag: el.tagName,
      id: el.id,
      date: el.dataset?.date || el.dataset?.weekDate || "",
      text: (el.getAttribute("aria-label") || el.textContent || "")
        .trim()
        .slice(0, 40),
      inDialog: !!el.closest('[role="dialog"]'),
    };
  });

const isActive = (locator) =>
  locator.first().evaluate((el) => el === document.activeElement);

// Sum of the numbers in a list of nodes' text (used to reconcile a visible list to a figure).
const sumOf = (locator) =>
  locator.evaluateAll((nodes) =>
    nodes.reduce(
      (total, node) =>
        total + Number(node.textContent.replace(/[^0-9.-]/g, "")),
      0
    )
  );

module.exports = {
  overflow,
  noOverflow,
  box,
  style,
  lightSurfaces,
  contrastOf,
  active,
  isActive,
  sumOf,
  OVERFLOW_TARGETS,
};
