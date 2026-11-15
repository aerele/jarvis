// Plain words for a chat action that failed before any model ran (a send, a confirm, a load,
// an upload, a rename). Shared by the mobile app and the Desk widget, which cannot import
// frontend/src. Mirrors frontend/src/lib/errors.js (isNetworkFailure, SESSION_EXPIRED_MESSAGE)
// and sendRejectionCopy.js; keep the sentences in step by hand.

export const SESSION_EXPIRED = "Your session has expired. Sign in again.";
export const OFFLINE =
  "Jarvis could not reach the server. Check your internet connection and try again.";

const BROWSER_NETWORK_TEXT =
  /^(failed to fetch|networkerror\b|load failed|network request failed)/i;

// "session" | "offline" | "other". Total: any value, including undefined, is safe.
export function failureKind(e) {
  if (!e || typeof e !== "object") return "other";
  if (e.status === 401 || e.status === 403) return "session";
  const spoke = Array.isArray(e.messages) && e.messages.length > 0;
  // jQuery (the Desk's frappe.call) reports a dead connection as status 0 / readyState 0;
  // fetch (frappe-ui) throws the browser's own TypeError text.
  if (e.status === 0 && !spoke) return "offline";
  if (
    !e.status &&
    !spoke &&
    BROWSER_NETWORK_TEXT.test(String(e.message || "").trim())
  ) {
    return "offline";
  }
  return "other";
}

// `fallback` is the sentence for every other failure. It should already carry the next step.
export function failureCopy(e, fallback) {
  const kind = failureKind(e);
  if (kind === "session") return SESSION_EXPIRED;
  if (kind === "offline") return OFFLINE;
  return fallback;
}

// A send the server refused ({ ok: false, reason }). Returns a sentence, or "" for a reason
// this table does not know (the caller then uses its own fallback). Same copy as the desktop
// app's sendRejectionCopy.js.
const WINDOW_COPY = {
  Daily: "You've reached today's usage limit. It resets at midnight.",
  Weekly: "You've reached this week's usage limit. It resets on Sunday.",
  Monthly: "You've reached this month's usage limit. It resets on the 1st.",
};

export function refusalCopy(reason, brand, envelope) {
  const name = (brand || "").trim() || "Jarvis";
  const known = {
    maintenance: `${name} is temporarily unavailable for maintenance. Try again shortly.`,
    usage_limit:
      WINDOW_COPY[envelope && envelope.limit_period] ||
      `You've reached your usage limit. Ask your ${name} admin to raise it.`,
    llm_not_configured:
      "No AI model is connected. Connect one in Settings, AI models.",
    workspace_resetting: `${name} is being reset. Chat will be back in a few minutes.`,
    subscription_suspended:
      "Your subscription has lapsed. Renew it to keep chatting.",
  };
  return typeof reason === "string" && Object.hasOwn(known, reason)
    ? known[reason]
    : "";
}

// A queued turn that was cancelled ({ kind: "turn:cancelled", reason }). The server sends a
// sentence it wrote (jarvis.chat.admission USER_CANCEL_REASON, AGE_OUT_REASON), which reads
// fine. A machine code or JSON-looking text never does: those get one plain sentence.
export const CANCEL_FALLBACK =
  "Your waiting request was cancelled. Send it again.";

export function cancelCopy(reason) {
  const text = typeof reason === "string" ? reason.trim() : "";
  return /\s/.test(text) && !/[{}]|Traceback/.test(text)
    ? text
    : CANCEL_FALLBACK;
}

// Mirrors MAX_MESSAGE_CHARS in frontend/src/lib/errors.js (smaller, for the panel).
const MAX_REASON_CHARS = 300;

// Best human-readable reason in a Frappe error body: _server_messages (a JSON
// list of JSON strings), then message, then exception. HTML tags are stripped.
export function serverReason(body) {
  if (!body || typeof body !== "object") return "";
  let text = "";
  try {
    const first = JSON.parse(body._server_messages || "[]")[0];
    text = first ? JSON.parse(first).message || "" : "";
  } catch (e) {
    text = "";
  }
  if (!text && typeof body.message === "string") text = body.message;
  if (!text && typeof body.exception === "string") {
    // Only the text after the first colon; a bare class name is no use to users.
    text = body.exception.split(":").slice(1).join(":");
  }
  text = text
    .replace(/<\/?[a-zA-Z][^>]*>/g, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#(\d+);/g, (_m, n) => String.fromCharCode(Number(n)))
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > MAX_REASON_CHARS
    ? text.slice(0, MAX_REASON_CHARS - 1) + "…"
    : text;
}

// A file that would not upload. `e.status` and `e.serverReason` come from the uploader
// (widget panel_api.mjs, mobile api.js); the server's own sentence, such as "File type is not
// allowed", is already plain and says the fix.
export function uploadFailureCopy(e, fileName = "") {
  const kind = failureKind(e);
  if (kind === "session") return SESSION_EXPIRED;
  if (kind === "offline") return OFFLINE;
  const what = fileName ? `"${fileName}"` : "That file";
  if (e && e.status === 413)
    return `${what} is too large. Choose a smaller file and try again.`;
  if (e && typeof e.serverReason === "string" && e.serverReason)
    return e.serverReason;
  return `${what} could not be attached. Check your connection and try again.`;
}
