// Thin wrappers over the Desk's own frappe.call. The panel runs INSIDE the
// Desk, so the session cookie and CSRF token are already in place — unlike the
// SPA and the PWA, this surface needs no frappe-ui and no socket of its own.
//
// frappe.call resolves to the full envelope; every caller here wants
// `.message`, so unwrap it once at this boundary.

const CHAT = "jarvis.chat.api.";
const ACTIONS = "jarvis.chat.actions_api.";
const ACCOUNT = "jarvis.account.";
const ONBOARDING = "jarvis.onboarding.";
export const TRANSCRIPTION_TIMEOUT_MS = 150000;

function call(method, args) {
  return frappe.call({ method, args: args || {} }).then((r) => r.message);
}

export const listConversations = () => call(CHAT + "list_conversations");

export const getConversation = (conversation) =>
  call(CHAT + "get_conversation", { conversation });

// An empty `conversation` is allowed: the backend creates (or focuses) the
// user's empty conversation and returns its id as `conversation_id`, which
// saves a round-trip before the very first send.
//
// `context` is the object from desk_context.contextFromRoute. Send it only when
// there is one, so a non-record page behaves as a plain chat.
export const sendMessage = (
  conversation,
  message,
  context,
  attachments,
  approvalTokens
) =>
  call(CHAT + "send_message", {
    conversation: conversation || "",
    message,
    ...(context ? { context: JSON.stringify(context) } : {}),
    ...(attachments && attachments.length
      ? { attachments: JSON.stringify(attachments) }
      : {}),
    // Ordered tokens of the on-screen confirmation cards, so a typed "confirm 2"
    // resolves to the card shown at that number instead of a re-fetched position
    // that a mid-flight expiry could renumber onto a different write.
    ...(approvalTokens && approvalTokens.length
      ? { approval_tokens: JSON.stringify(approvalTokens) }
      : {}),
  });

// Mic gating: stt_enabled is false when voice/STT is not configured, and the
// button must not appear at all in that case.
export const getChatUiSettings = () => call(CHAT + "get_chat_ui_settings");

// Multipart endpoints cannot go through frappe.call, so these two use fetch
// with the Desk's CSRF token, the same way the SPA's uploadFile does.
export async function uploadFile(file) {
  const fd = new FormData();
  fd.append("file", file, file.name);
  fd.append("is_private", "1");
  const r = await fetch("/api/method/upload_file", {
    method: "POST",
    headers: { "X-Frappe-CSRF-Token": window.csrf_token || "" },
    body: fd,
    credentials: "include",
  });
  if (!r.ok) throw new Error(`upload failed (${r.status})`);
  const data = await r.json();
  const f = data.message || data;
  return { file_url: f.file_url, file_name: f.file_name || file.name };
}

function audioName(blob) {
  const t = (blob && blob.type) || "";
  if (t.includes("ogg")) return "audio.ogg";
  if (t.includes("mp4")) return "audio.m4a";
  if (t.includes("wav")) return "audio.wav";
  return "audio.webm";
}

// Recorded blob -> verbatim transcript. Returns {ok, text, ...}.
export async function transcribeAudio(blob, durationS) {
  const fd = new FormData();
  fd.append("audio", blob, audioName(blob));
  fd.append("duration_s", String(Math.round(durationS || 0)));
  const ctrl = new AbortController();
  // Match the SPA and PWA budget. The browser upload plus the provider's
  // 90-second read window cannot fit inside the widget's former 25-second cap.
  const timer = setTimeout(() => ctrl.abort(), TRANSCRIPTION_TIMEOUT_MS);
  try {
    const r = await fetch("/api/method/jarvis.chat.voice.transcribe_audio", {
      method: "POST",
      headers: { "X-Frappe-CSRF-Token": window.csrf_token || "" },
      body: fd,
      credentials: "include",
      signal: ctrl.signal,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(`transcription failed (${r.status})`);
    return data.message || data;
  } finally {
    clearTimeout(timer);
  }
}

export const stopRun = (conversation, runId) =>
  call(CHAT + "stop_run", { conversation, run_id: runId || "" });

// Resolves a write-confirmation gate raised by an `action:pending` frame.
export const confirmTool = (token, conversation) =>
  call(ACTIONS + "confirm_tool", { token, conversation: conversation || "" });

// Resync the still-open write confirmations for a conversation. The panel gets
// live cards from the `action:pending` realtime frame, but a card raised while
// the panel was CLOSED (or a dropped realtime tail) would otherwise never show —
// so load() calls this to restore them. Returns {ok, data:{pending:[...]}}.
export const listPendingConfirmations = (conversation) =>
  call(ACTIONS + "list_pending_confirmations", {
    conversation: conversation || "",
  });

// Chat-readiness verdict ({ready, reason, detail, billing_notice}) - see
// panel_readiness.mjs for how the panel classifies it into gate/degraded/ready.
export const isReadyForChat = () => call(ACCOUNT + "is_ready_for_chat");

// Re-drive the saved AI config through admin - the lever for a stuck apply
// (reason "llm_apply_stuck", jarvis#825). Probes admin first, only re-pushes if
// not already serving, throttled 180s server-side. Admin-gated (require_jarvis_admin);
// the panel only shows the Retry CTA to a Jarvis Admin / System Manager.
export const resyncLlm = () => call(ONBOARDING + "resync_llm");
