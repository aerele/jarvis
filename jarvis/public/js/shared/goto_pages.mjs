// The pages a ```jarvis-goto block may name, and the SPA route each one opens.
//
// The agent picks one of these keys; it never names a route. Navigation uses
// only the route name from this table, so a block cannot send the user to an
// arbitrary path. Placed under jarvis/public/js/shared/ so every surface reads
// one list. The real consumers today:
//   - frontend/src/lib/chatGoto.js (parse allowlist, page map, decision), which
//     the desktop SPA's ChatView and api.js use, and which the PWA reuses
//     through pwa/src/lib/blocks.js (the chip);
//   - the Desk widget parses the goto with chatGoto.js parseGoto: a Dashboards
//     goto becomes a "Continue in full chat" chip; other gotos are removed.
// .mjs so it is unambiguously ESM regardless of the nearest package.json "type".
//
// Route names are those in frontend/src/router/index.js; goto_pages.test.mjs
// fails when one is renamed there.

// The page keys, as constants, so no consumer repeats the literal.
export const DASHBOARDS_PAGE = "dashboards";
export const PAYMENT_CALENDAR_PAGE = "payment-calendar";

export const GOTO_PAGES = Object.freeze({
  [DASHBOARDS_PAGE]: "DashboardsPage",
  [PAYMENT_CALENDAR_PAGE]: "PaymentCalendar",
});
