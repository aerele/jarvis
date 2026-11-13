// Shared by the theme groups: the DARK_VARS colours and small colour probes.
const { check, equal } = require("./checks.cjs");
const { style, lightSurfaces } = require("./probe.cjs");

// DARK_VARS of frontend/src/theme.js as rgb().
const DARK = {
  surface: "rgb(22, 22, 26)",
  raised: "rgb(29, 29, 34)",
  input: "rgb(38, 38, 45)",
  text: "rgb(237, 237, 242)",
  link: "rgb(110, 139, 255)",
  green: "rgb(52, 211, 153)",
  amber: "rgb(251, 191, 36)",
  red: "rgb(248, 113, 113)",
  amberBg: "rgb(42, 35, 21)",
};
// Surfaces the design inverts in dark mode (the template's .btn.primary, today badge, save, toast).
const INVERTED =
  ".pcal-primary-btn, .btn.primary, .today .date-number, .floor-save, .pcal-toast, input[type=checkbox]";

const theme = (page) => page.locator("html").getAttribute("data-theme");
const color = (page, selector, property = "backgroundColor") =>
  style(page.locator(selector), property);
const dark = async (page) => (await theme(page)) === "dark";
const dim = async (page) => {
  const found = await lightSurfaces(page, INVERTED);
  return { pass: !found.length, detail: `(light: ${found.join(" | ")})` };
};

async function is(page, name, selector, expected, property) {
  await check(name, async () =>
    equal(await color(page, selector, property), expected)
  );
}

module.exports = { DARK, dark, dim, color, is };
