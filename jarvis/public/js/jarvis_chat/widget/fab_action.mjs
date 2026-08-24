// What a tap on the floating Jarvis button (FAB) should do, as pure functions so
// the load-bearing bits can be unit-tested away from the Desk DOM. Widget.vue's
// onFabClick wires these to frappe.boot, window.innerWidth, and Vue refs.
//
// Two decisions live here:
//
//   fabAction()       - route the tap: no-access redirect vs full-SPA handoff vs
//                       open/close the in-place side panel.
//   panelTogglePlan() - once the tap is a "toggle", how to drive the lazily
//                       mounted Panel, including the two-tick FIRST-open sequence.
//
// Both exist to keep the side-chat Panel from ever mounting for a user without
// Jarvis access: the Panel calls get_chat_ui_settings() (and other access-gated
// endpoints) on mount, which the server rejects with a PermissionError - a red
// "Message" dialog - for anyone lacking the Jarvis User role. So the widget
// mounts the FAB for everyone (preserving the /jarvis-no-access self-heal
// redirect) but only OPENS - and thus only lazily mounts - the Panel for a user
// who has access.

/**
 * Route a FAB tap.
 *
 *   - "no-access": redirect to /jarvis-no-access (no panel; no server call).
 *   - "full":      hand off to the full chat SPA (a 400px panel is most of a
 *                  narrow screen, so there is no in-place layout for it).
 *   - "toggle":    open/close the in-place side panel.
 *
 * `hasAccess` is Boolean(frappe.boot.jarvis_has_access) - the same
 * has_jarvis_access() verdict the server enforces (jarvis/boot.py), read
 * fail-closed so a missing/undefined flag routes to the recoverable no-access
 * page rather than opening a panel that would just pop the error dialog.
 */
export function fabAction(hasAccess, viewportWidth, minViewportPx) {
  if (!hasAccess) return "no-access";
  if (viewportWidth < minViewportPx) return "full";
  return "toggle";
}

/**
 * Given the Panel's current mount/open state, describe how a "toggle" tap should
 * drive it. Split out of onFabClick so the FIRST-open sequence - the easy thing
 * to get subtly wrong - is unit-testable without a DOM.
 *
 *   - mount:       lazily mount the Panel now if it is not mounted yet.
 *   - open:        the Panel's target open state after this tap.
 *   - deferReveal: on the FIRST open the Panel must mount CLOSED and flip open on
 *                  the NEXT tick, so Panel.vue's non-immediate watch(() =>
 *                  props.open) observes a false->true transition and runs its
 *                  first-open load() (conversation restore), ensureRealtime() and
 *                  focus. Revealing in the same tick as the mount makes the Panel
 *                  mount already-open, the watch never fires, and the panel opens
 *                  BLANK (and the first send mints a NEW conversation). Only the
 *                  first open needs this; a reopen toggles a Panel that is already
 *                  mounted and watching.
 *   - readContext: re-read the Desk record context. Done when opening (first open
 *                  or reopen), never on a close.
 */
export function panelTogglePlan({ mounted, open }) {
  if (!mounted) {
    return { mount: true, open: true, deferReveal: true, readContext: true };
  }
  const opening = !open;
  return {
    mount: false,
    open: opening,
    deferReveal: false,
    readContext: opening,
  };
}
