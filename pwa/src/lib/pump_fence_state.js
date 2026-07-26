// JF-018: the PWA's Relay-Pump fence watermarks, one entry per run_id.
//
// MODULE scope on purpose. ChatView is route-mounted, and the PWA router has
// no <keep-alive>: navigating to /business, /files or / unmounts the component
// and would destroy a component-scope fence — exactly the window in which a
// superseded pump's straggler (or the routine duplicate terminal that
// finalize's terminal_publish re-emits) gets readmitted on return. Desktop
// never hits this because its ChatView IS the app shell and never unmounts;
// the widget survives via its own persistent panel state. A module singleton
// is the PWA's equivalent of "the fence outlives the view".
//
// A plain object, not a ref — nothing renders from it, and every delta frame
// would otherwise pay for a reactive proxy write. Entries are three integers
// keyed by run_id, so growth across a session is a non-issue.
import { createFence } from "@jsshared/pump_fence.mjs";

export const eventFence = createFence();
