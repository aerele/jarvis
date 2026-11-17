// Opens the Payment Calendar in a fresh context: boot globals, fixed clock, stateful mock.
const { CalendarMock } = require("./mock.cjs");

// 10:42 IST on 11 Nov 2026 (= 05:12 UTC), the template's "now".
const CLOCK_START = new Date("2026-11-11T10:42:00+05:30");
const ROUTE = "/jarvis/dashboards/payment-calendar";

const DESK_THEME = { light: "Light", dark: "Dark", system: "Automatic" };

const WIDTHS = {
  desktop: { width: 1632, height: 1000 },
  laptop: { width: 1440, height: 900 },
  narrow: { width: 1100, height: 800 },
  tablet: { width: 768, height: 900 },
  phone: { width: 390, height: 844, touch: true },
  small: { width: 375, height: 667, touch: true },
  tiny: { width: 320, height: 568, touch: true },
};

function contextOptions(
  size,
  { timezoneId = "Asia/Kolkata", scheme = "light" } = {}
) {
  return {
    viewport: { width: size.width, height: size.height },
    isMobile: !!size.touch,
    hasTouch: !!size.touch,
    timezoneId,
    colorScheme: scheme,
    deviceScaleFactor: 1,
  };
}

// theme: "light" | "dark" | "system". Both the desk boot value (what theme.js reads) and the
// local anti-flash cache (what index.html reads) are set, as the server and a returning user would.
const bootGlobals = ({ theme, desk, flags }) => {
  Object.assign(window, {
    csrf_token: "test",
    time_zone: "Asia/Kolkata",
    agent_name: "Jarvis",
    is_system_manager: false,
    is_jarvis_admin: false,
    support_available: false,
    has_support_access: false,
    release_notice: "",
    can_view_payment_calendar: true,
    jarvis_desk_theme: desk,
    ...flags,
  });
  try {
    if (!sessionStorage.getItem("pcal-booted")) {
      localStorage.setItem("jarvis-theme", theme);
      sessionStorage.setItem("pcal-booted", "1");
    }
  } catch (e) {
    /* storage blocked */
  }
};

// A font served from outside the Vite allow list (a worktree whose node_modules is linked) is
// a local setup artefact, not a page error. Any other failed resource is reported.
function environmentNoise(message) {
  const url = message.location().url || "";
  return (
    /\.woff2?($|\?)/.test(url) && /Failed to load resource/.test(message.text())
  );
}

// Returns { page, context, mock, errors }. `ready: false` skips waiting for the first snapshot.
async function openCalendar(browser, baseURL, options = {}) {
  const {
    size = WIDTHS.desktop,
    theme = "light",
    flags = {},
    query = "",
    ready = true,
    state,
    blockStorage,
  } = options;
  const context = await browser.newContext({
    ...contextOptions(size, options),
    baseURL,
  });
  await context.addCookies([
    { name: "user_id", value: "finance%40example.test", url: baseURL },
  ]);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error" && !environmentNoise(message))
      errors.push(`console: ${message.text()}`);
  });
  // The dev proxy has no bench behind it: answer the static assets the shell asks for.
  await page.route(/\/assets\/(jarvis|frappe)\//, (route) =>
    route.fulfill({ status: 204, body: "" })
  );
  await page.addInitScript(bootGlobals, {
    theme,
    desk: DESK_THEME[theme],
    flags,
  });
  if (blockStorage) await page.addInitScript(blockStorage);
  await page.clock.install({ time: CLOCK_START });
  const mock = new CalendarMock({ clockStart: CLOCK_START, ...(state || {}) });
  mock.onUserTheme = (saved) =>
    page.addInitScript(
      (desk) => (window.jarvis_desk_theme = desk),
      DESK_THEME[saved]
    );
  options.setup?.(mock);
  await mock.install(page);
  await page.goto(`${ROUTE}?nosocket${query}`);
  if (ready) await waitReady(page);
  return { page, context, mock, errors };
}

const waitReady = (page) =>
  page
    .locator("#lowest-cushion")
    .waitFor({ state: "visible", timeout: 30000 })
    .then(() =>
      page.waitForFunction(
        () =>
          document.querySelector("#lowest-cushion")?.textContent.trim() !==
            "" && !document.querySelector(".headline-value.skeleton"),
        null,
        { timeout: 30000 }
      )
    );

module.exports = {
  openCalendar,
  waitReady,
  WIDTHS,
  CLOCK_START,
  ROUTE,
  contextOptions,
};
