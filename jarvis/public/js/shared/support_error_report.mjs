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
// is not fatal: the ticket opens with the diagnostic metadata alone. Returns false when no
// draft exists (a cancelled turn) or the tab could not open.
export async function openErrorReport({
  info,
  conversation,
  message,
  at,
  fetchDetails,
  open,
}) {
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
  if (!draft) return false;
  const win = open(reportErrorUrl(draft), "_blank", "noopener");
  return win !== false;
}
