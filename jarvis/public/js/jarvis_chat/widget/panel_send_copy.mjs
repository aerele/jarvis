// Copy for a send the server refused outright, extracted from Panel.vue so the
// decision (refusal reason -> user-facing sentence) is a pure, node-testable
// function rather than an inline ternary buried in the send() handler.
//
// Plain .mjs by necessity, not choice: this widget is served straight into Desk
// (see jarvis_widget.bundle.js) and cannot import from frontend/src or use the
// SPA's "@/" alias - the same constraint that forces panel_readiness.mjs to
// duplicate readiness.js. Keep the SPA's own equivalent copy in sync BY HAND.
//
// The full update nudge (pill/banner) stays off the bubble by design; this is
// only the legible reason shown when a send is rejected.

import { refusalCopy } from "../../shared/chat_failure_copy.mjs";

// `brandName` is a PARAMETER (default "Jarvis"), not an import, so this module
// stays pure and testable - the caller passes the white-label agent name in.
//   - "release_update_required": the branded "a new <brand> version is required"
//     line, so a customer who renamed the assistant never sees "Jarvis" leak.
//   - usage limit, subscription, no AI model, workspace reset: the same copy as the desktop
//     app (shared/chat_failure_copy.mjs refusalCopy).
//   - anything else (a generic/unknown refusal): a neutral "try again" line.
// `envelope` is the whole refusal ({ reason, limit_period, ... }); only the usage-limit
// window is read from it.
export function sendRefusalMessage(reason, brandName = "Jarvis", envelope) {
  const brand = (brandName || "").trim() || "Jarvis";
  if (reason === "release_update_required") {
    return `A new ${brand} version is required. Please ask your administrator to update.`;
  }
  if (reason === "maintenance") {
    return `${brand} is upgrading and will be back shortly.`;
  }
  return (
    refusalCopy(reason, brand, envelope) ||
    "That couldn't be sent. Please try again."
  );
}

// True for a reason whose copy is ours, so it outranks a server-sent sentence.
export function isKnownRefusal(reason) {
  return (
    reason === "release_update_required" ||
    reason === "maintenance" ||
    !!refusalCopy(reason, "Jarvis")
  );
}
