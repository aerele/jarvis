// Pure reducer over the `jarvis:event` realtime frames the chat worker
// publishes. Kept out of Panel.vue so the streaming rules — the easiest thing
// in this feature to get subtly wrong — are unit-testable without a browser.
//
// Reference implementation: pwa/src/views/ChatView.vue (onEvent).

import { admitEvent } from "../../shared/pump_fence.mjs";

// `fence` is the Relay-Pump epoch/seq watermark, carried IN the reducer state so
// this stays a pure function of (state, frame) — Panel.vue holds one state object
// and never has to know the fence exists.
export function emptyStream() {
  return {
    live: null,
    busy: false,
    error: "",
    pending: [],
    reload: false,
    // Coarse turn phase, mirroring the full SPA's status line: "waking" while a
    // cold container starts, null otherwise. Panel.vue turns this into the
    // visible caption ("Waking up your assistant…" / "Working on it…") so the
    // mini panel says the same thing the web chat does instead of bare dots.
    status: null,
    fence: {},
  };
}

// A conversation's message list is not just user/assistant prose: `tool` rows
// outnumber both (they carry tool_name/tool_status, and their content is either
// null or a terse "get_doc -> completed"). The full SPA renders those as tool
// cards; this panel is deliberately text-only, so rendering them would put raw
// machine chatter in the transcript.
//
// Empty-content assistant rows are dropped for the same reason: they are the
// shell of a turn whose payload lives in canvas items the panel does not show,
// and they would otherwise paint as blank bubbles.
// Whether a parked write's card offers "Approve & run" (P1, skill
// approve-and-run - preview.card.approve_run, jarvis/api.py). The widget is
// text-only and has NO rich-card/plan renderer (design §3.5), so it reads only
// this one boolean off the preview it already receives on the wire - never the
// plan/steps themselves - purely to decide whether to deflect to "Review in
// full chat" instead of quietly offering its own plain per-card Confirm.
export function pendingApproveRun(preview) {
  return !!(preview && preview.card && preview.card.approve_run);
}

export function visibleMessages(messages) {
  if (!Array.isArray(messages)) return [];
  return messages.filter(
    (m) =>
      m &&
      (m.role === "user" || m.role === "assistant") &&
      typeof m.content === "string" &&
      m.content.trim() !== ""
  );
}

// Returns a NEW state; never mutates the input. The caller assigns the result
// to a Vue ref, so structural sharing is not worth the aliasing risk.
export function applyEvent(state, payload) {
  return applyEventEx(state, payload).state;
}

// Like applyEvent, but also says whether the fence ADMITTED the frame. The
// widget's HTTP-polling fallback must only stand down for an admitted frame:
// a stream of nothing but fenced-out stragglers proves a superseded pump is
// still talking, not that the winning pump's frames are arriving.
export function applyEventEx(state, payload) {
  const p = payload || {};
  const s = {
    live: state.live ? { ...state.live } : null,
    busy: state.busy,
    error: state.error,
    pending: state.pending.slice(),
    reload: state.reload,
    status: state.status || null,
    // Shallow copy is enough: admitEvent REPLACES a run's entry, never mutates it.
    fence: { ...(state.fence || {}) },
  };

  // JF-018: drop a superseded pump's straggler before it can touch the projection.
  // Deltas carry the FULL text so far, so an out-of-order one silently REWINDS the
  // reply, and a stale run:end/run:error re-closes (or re-errors) a turn that has
  // already moved on. Nothing user-visible is lost: the terminal path reloads the
  // durable conversation over HTTP, which is what the panel actually renders.
  if (!admitEvent(s.fence, p)) return { state: s, admitted: false };

  return { state: applyAdmitted(s, p), admitted: true };
}

// The per-kind projection, applied only AFTER the fence admitted the frame.
// `s` is already a safe copy the caller owns.
function applyAdmitted(s, p) {
  switch (p.kind) {
    // Pre-flight phase (the container is cold and being woken). Arrives BEFORE
    // run:start, which is why it gets its own caption: this is the slowest wait
    // in a turn and bare dots make it look hung.
    case "run:status":
      if (p.status === "waking") s.status = "waking";
      return s;

    case "run:start":
      s.busy = false;
      // Woken: the model is working now, so the caption drops back to the
      // generic "Working on it…".
      s.status = null;
      s.live = {
        runId: p.run_id || "",
        messageId: p.message_id || "",
        text: "",
      };
      return s;

    case "assistant:delta":
      // The frame carries the FULL text so far, not an increment. Assign it —
      // appending doubles the reply.
      if (!s.live) s.live = { runId: p.run_id || "", messageId: "", text: "" };
      s.live.messageId = p.message_id || s.live.messageId;
      s.live.text = p.text || "";
      return s;

    case "run:end":
      s.busy = false;
      s.live = null;
      s.status = null;
      // The reply is durable now; re-fetch rather than trusting the streamed
      // copy, which lacks the final formatting.
      s.reload = true;
      return s;

    case "run:error":
      s.busy = false;
      s.live = null;
      s.status = null;
      s.error = p.error || "That turn failed.";
      s.reload = true;
      return s;

    case "action:pending":
      // The agent is blocked waiting on a write confirmation. Without this the
      // panel would sit on "Jarvis is replying..." forever.
      if (!p.token) return s;
      if (s.pending.some((x) => x.token === p.token)) return s;
      s.pending.push({
        token: p.token,
        tool: p.tool || "",
        summary: p.summary || "",
        // expires_at is the primary sort key for numbered typed approval
        // ("confirm 2"). Dropping it makes orderedPending fall back to token
        // order, which disagrees with the server and can run the wrong card.
        expires_at: p.expires_at ?? null,
        // Text-only signal (see pendingApproveRun) - never the preview/plan
        // itself, so the widget stays a plain confirm/deflect surface.
        approve_run: pendingApproveRun(p.preview),
      });
      return s;

    case "action:resolved":
      s.pending = s.pending.filter((x) => x.token !== p.token);
      return s;

    case "conversation:renamed":
      s.reload = true;
      return s;

    default:
      return s;
  }
}
