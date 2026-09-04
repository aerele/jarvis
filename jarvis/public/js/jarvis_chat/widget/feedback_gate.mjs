// Client-side throttle for the mini-widget feedback pills. Mirrors the main
// SPA's lib/feedbackGate.js and shares its localStorage key, so the per-day caps
// are unified across both chat surfaces (a user who rates in the SPA won't also
// be nagged in the widget the same day). Keep the two in sync if either changes.

const KEY = "jvChatFeedback.v1";

export const FEEDBACK_MIN_MS = 60000; // only genuinely long replies (> 1 min)

const SHOW_PROB = 1 / 3;
const COOLDOWN_REPLIES = 5;
const SESSION_CAP = 2;
const IGNORE_CAP = 2;

function today() {
  return new Date().toISOString().slice(0, 10);
}

function fresh() {
  return {
    day: today(),
    n: 0,
    lastShown: null,
    shown: 0,
    rated: false,
    ignores: 0,
    stopped: false,
  };
}

function load() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "null");
    if (!raw || raw.day !== today()) return fresh();
    return {
      day: raw.day,
      n: raw.n | 0,
      lastShown: raw.lastShown == null ? null : raw.lastShown | 0,
      shown: raw.shown | 0,
      rated: !!raw.rated,
      ignores: raw.ignores | 0,
      stopped: !!raw.stopped,
    };
  } catch {
    return fresh();
  }
}

function save(s) {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* storage disabled - degrade to "never show" */
  }
}

export function shouldOfferFeedback(durationMs) {
  const s = load();
  s.n += 1;
  let show = false;
  const cooldownOk =
    s.lastShown === null || s.n - s.lastShown >= COOLDOWN_REPLIES;
  if (
    !s.stopped &&
    !s.rated &&
    s.shown < SESSION_CAP &&
    Number(durationMs) >= FEEDBACK_MIN_MS &&
    cooldownOk &&
    Math.random() < SHOW_PROB
  ) {
    show = true;
    s.shown += 1;
    s.lastShown = s.n;
  }
  save(s);
  return show;
}

export function markRated() {
  const s = load();
  s.rated = true;
  save(s);
}

export function markIgnored() {
  const s = load();
  s.ignores += 1;
  if (s.ignores >= IGNORE_CAP) s.stopped = true;
  save(s);
}
