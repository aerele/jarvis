// What a tap on the floating Jarvis button (FAB) should do. Pure so it can be
// unit-tested away from the Desk DOM (Widget.vue's onFabClick wires it to
// frappe.boot + window.innerWidth).
//
// This is the gate that keeps the side-chat Panel from ever mounting for a user
// without Jarvis access. The Panel calls get_chat_ui_settings() (and other
// access-gated endpoints) on mount, which the server rejects with a
// PermissionError — a red "Message" dialog — for anyone lacking the Jarvis User
// role. So the widget mounts the FAB for everyone (preserving the no-access
// self-heal redirect below), but only OPENS — and thus only lazily mounts — the
// Panel for a user who has access. A no-access tap redirects to the page that
// explains how to get access instead.
//
//   - "no-access": redirect to /jarvis-no-access (no panel; no server call).
//   - "full":      hand off to the full chat SPA (a 400px panel is most of a
//                  narrow screen, so there is no in-place panel layout for it).
//   - "toggle":    open/close the in-place side panel.
//
// `hasAccess` is Boolean(frappe.boot.jarvis_has_access) — the same
// has_jarvis_access() verdict the server enforces (jarvis/boot.py), read
// fail-closed so a missing/undefined flag routes to the recoverable no-access
// page rather than opening a panel that would just pop the error dialog.
export function fabAction(hasAccess, viewportWidth, minViewportPx) {
  if (!hasAccess) return "no-access";
  if (viewportWidth < minViewportPx) return "full";
  return "toggle";
}
