// Visibility decision for the global floating Jarvis widget (the FAB).
//
// Pure so it can be unit-tested away from the Desk DOM (jarvis_widget.bundle.js
// wires it to frappe.get_route() and frappe.boot). The FAB is hidden on
// full-page routes where a floating launcher is redundant or unwanted:
//
//   - Jarvis's own full-page surfaces (the chat page, the onboarding wizard).
//   - Frappe's SETUP WIZARD — while a site's initial setup isn't finished Frappe
//     forces every route to "setup-wizard" (router.js) and blocks the desk, so a
//     Jarvis launcher floating over the setup screen is just noise.
//
// It is also hidden whenever `siteSetupComplete` is explicitly false: a site
// can be past ERPNext's setup wizard route with no Company yet (e.g. the
// wizard was skipped), and popping a "working" chat bubble there just walks a
// user into a broken session. `siteSetupComplete` is
// frappe.boot.jarvis_site_setup_complete (jarvis/boot.py — true once a
// Company exists). Strict === false, matching the onboarding banner's
// check, so an older boot payload without the key still shows the FAB.

export const HIDE_ON_ROUTES = [
  "jarvis-chat",
  "jarvis-onboarding",
  "setup-wizard",
];

// `route` is frappe.get_route() (an array; route[0] is the page).
// `siteSetupComplete` is frappe.boot.jarvis_site_setup_complete.
export function shouldHideWidget(route, siteSetupComplete) {
  const page = (Array.isArray(route) && route[0]) || "";
  if (HIDE_ON_ROUTES.indexOf(page) !== -1) return true;
  return siteSetupComplete === false;
}

// Whether the widget may be MOUNTED at all for this user — a stronger gate than
// shouldHideWidget, which only toggles per-route visibility of an ALREADY-mounted
// widget. A user without Jarvis access must never mount it: the Panel calls
// get_chat_ui_settings() on mount (Panel.vue onMounted), which the server rejects
// with a PermissionError for anyone lacking the Jarvis User role — surfacing as a
// red "Message" dialog on EVERY Desk page load. display:none does not help; the
// component still mounts and still makes the call, so the gate has to run before
// mount. `hasAccess` is frappe.boot.jarvis_has_access (jarvis/boot.py — the same
// has_jarvis_access() verdict the server enforces). Strict === false so an older
// boot payload without the key still mounts (matches shouldHideWidget's fail-open).
export function shouldMountWidget(hasAccess) {
  return hasAccess !== false;
}
