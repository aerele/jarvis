// The pages a ```jarvis-goto block may name, and the SPA route each one opens.
//
// The agent picks one of these keys; it never names a route. Navigation uses
// only the route name from this table, so a block cannot send the user to an
// arbitrary path. Placed under jarvis/public/js/shared/ so the desktop SPA, the
// PWA and the Desk widget WILL read one list (the Desk widget is an esbuild
// bundle that cannot reach frontend/src or the "@/" alias). They consume it from
// task A3 (PR 2); in A0 only goto_pages.test.mjs reads it. .mjs so it is
// unambiguously ESM regardless of the nearest package.json "type".
//
// Route names are those in frontend/src/router/index.js; goto_pages.test.mjs
// fails when one is renamed there.
export const GOTO_PAGES = Object.freeze({
  dashboards: "DashboardsPage",
  "payment-calendar": "PaymentCalendar",
});
