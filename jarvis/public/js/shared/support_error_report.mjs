// Report error for the mobile app and the Desk widget: decide when the button shows and open
// the New Ticket page with the draft. The desktop app does the same in its own ChatView.
import { supportErrorDraft } from "./support_error_draft.mjs";

// Only when Aerele support is the one who can fix it (info.support from turnErrorInfo) AND
// this user can raise a ticket.
export function showReportError(info, supportOn) {
  return (
    !!supportOn && !!info && info.support === true && info.code !== "cancelled"
  );
}

export function reportErrorUrl(draft) {
  return `/jarvis/support/new?subject=${encodeURIComponent(
    draft.subject
  )}&body=${encodeURIComponent(draft.body)}`;
}

// `fetchDetails(conversation, message)` resolves to the endpoint's {ok, data}. A failure there
// is not fatal: the ticket opens with the diagnostic metadata alone.
// `openTab(url, target)` is window.open and `navigate(url)` is location.assign. The tab is opened
// FIRST, inside the click: a popup blocker allows a new tab only there, before any await. It is
// opened without "noopener" so we get a handle (window.open returns null with it), and the
// handle's opener is cleared before it loads the page. A blocked tab falls back to this tab.
// Returns false when no draft exists (a cancelled turn) or nothing could open.
export async function openErrorReport({
  info,
  conversation,
  message,
  at,
  fetchDetails,
  openTab,
  navigate,
}) {
  const tab = openTab ? openTab("", "_blank") : null;
  let details = null;
  try {
    const res = await fetchDetails(conversation, message);
    details = res && res.data ? res.data : null;
  } catch {
    details = null;
  }
  const draft = supportErrorDraft({
    code: info && info.code,
    upstream: info && info.upstream,
    conversation,
    message,
    at,
    details,
  });
  if (!draft) {
    if (tab) tab.close();
    return false;
  }
  const url = reportErrorUrl(draft);
  if (tab) {
    try {
      tab.opener = null;
    } catch {
      /* a cross-origin handle may refuse the write; the page is our own origin */
    }
    tab.location.href = url;
    return true;
  }
  if (navigate) {
    navigate(url);
    return true;
  }
  return false;
}
