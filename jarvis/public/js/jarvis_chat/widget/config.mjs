// Where the FAB sends an authorized user when the panel cannot be used
// (narrow viewport, or no access). No `?new=1` any more: that told the SPA to
// start a fresh conversation, which is the opposite of the side chat's
// contract — the panel continues where the user left off, and so should the
// full-page fallback.
export const FULL_CHAT_URL = "/jarvis/";

// Deep link to one conversation in the full SPA — the panel's "open full chat"
// action, so the panel reads as a view onto a thread rather than a separate
// inbox.
export const conversationUrl = (id) =>
  id ? `/jarvis/c/${encodeURIComponent(id)}` : FULL_CHAT_URL;

// The SPA's onboarding wizard route (frontend/src/router/index.js "/onboarding",
// mounted under the "/jarvis" base). The panel's setup nudge sends an admin
// here rather than duplicating the wizard in 400px.
export const ONBOARDING_URL = "/jarvis/onboarding";

// Below this viewport width a 400px docked panel is most of the screen, so the
// FAB falls back to navigating to the SPA instead of opening in place.
export const PANEL_MIN_VIEWPORT_PX = 640;
