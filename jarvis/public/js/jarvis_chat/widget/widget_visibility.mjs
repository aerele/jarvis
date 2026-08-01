// Visibility decision for the global floating Jarvis widget (the FAB).
//
// Pure so it can be unit-tested away from the Desk DOM (jarvis_widget.bundle.js
// wires it to frappe.get_route()). The FAB is hidden only on full-page routes
// where a floating launcher is redundant or unwanted:
//
//   - Jarvis's own full-page surfaces (the chat page, the onboarding wizard).
//   - Frappe's SETUP WIZARD — while a site's initial setup isn't finished Frappe
//     forces every route to "setup-wizard" (router.js) and blocks the desk, so a
//     Jarvis launcher floating over the setup screen is just noise.
//
// Deliberately ROUTE-based, NOT gated on whether the ERP is "set up" (a Company
// existing): a site can be past the setup wizard with no Company yet, and that is
// exactly where a user opens this bubble to set Jarvis up — so the launcher must
// stay available there. The moment setup completes Frappe re-routes away from
// "setup-wizard" and the FAB returns; the panel itself then handles the
// onboarded / degraded / gate states (panel_readiness.classifyReadiness).

export const HIDE_ON_ROUTES = [
  "jarvis-chat",
  "jarvis-onboarding",
  "setup-wizard",
];

// `route` is frappe.get_route() (an array; route[0] is the page).
export function shouldHideWidget(route) {
  const page = (Array.isArray(route) && route[0]) || "";
  return HIDE_ON_ROUTES.indexOf(page) !== -1;
}
