// How often the holiday scene plays: the client copy of jarvis/holiday_touch/cadence.py.
// Both are tested from one case table (cadence_cases.json), so they cannot drift.
// Pure: time and storage are injected, nothing here reads a clock. Text only, so it
// stays in the main bundle.

const SPLIT_HOUR = 13;
const MIN_GAP_MS = 3 * 60 * 60 * 1000;
// The browser key for showings the server never confirmed (see recordLocal).
export const LOCAL_KEY = "jv-holiday-shown";

// Local hour and date of an instant in a zone. Throws RangeError for a bad zone, the
// way the Python side raises for an unknown one: the caller decides what that means.
function localParts(ms, tz) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
  }).formatToParts(new Date(ms));
  const part = (type) => parts.find((p) => p.type === type).value;
  return {
    date: `${part("year")}-${part("month")}-${part("day")}`,
    hour: Number(part("hour")),
  };
}

const toMs = (value) =>
  value instanceof Date ? value.getTime() : new Date(value).getTime();

/**
 * May the scene play now? `times` are earlier showings (ISO strings, UTC instants);
 * only the ones on today's local date count, so a stale list or a zone change cannot
 * block a new day. Unreadable entries are ignored.
 */
export function mayPlay(now, times, tz) {
  const nowMs = toMs(now);
  const local = localParts(nowMs, tz);
  const today = (Array.isArray(times) ? times : [])
    .map(toMs)
    .filter(
      (ms) => Number.isFinite(ms) && localParts(ms, tz).date === local.date
    )
    .sort((a, b) => a - b);
  const am = today.find((ms) => localParts(ms, tz).hour < SPLIT_HOUR);
  const pm = today.find((ms) => localParts(ms, tz).hour >= SPLIT_HOUR);
  if (local.hour < SPLIT_HOUR) return am === undefined && pm === undefined;
  if (pm !== undefined) return false;
  if (am === undefined) return true;
  return nowMs - am >= MIN_GAP_MS;
}

/**
 * `{date, times}` with `now` appended; showings from any other local date are dropped
 * with unreadable entries. Same shape the server stores.
 */
export function addShowing(state, now, tz) {
  const nowMs = toMs(now);
  const today = localParts(nowMs, tz).date;
  const old = state && Array.isArray(state.times) ? state.times : [];
  const kept = old
    .map(toMs)
    .filter((ms) => Number.isFinite(ms) && localParts(ms, tz).date === today)
    .map((ms) => new Date(ms).toISOString());
  kept.push(new Date(nowMs).toISOString());
  return { date: today, times: kept };
}

/** Showings remembered in this browser (never throws; storage may be blocked). */
export function readLocal(storage) {
  try {
    const state = JSON.parse(storage.getItem(LOCAL_KEY));
    return state && Array.isArray(state.times) ? state : null;
  } catch {
    return null;
  }
}

/** Remember a showing the server did not confirm. Storage failures are ignored. */
export function recordLocal(storage, now, tz) {
  try {
    const next = addShowing(readLocal(storage), now, tz);
    storage.setItem(LOCAL_KEY, JSON.stringify(next));
  } catch {
    // Private window or blocked storage: the scene still played, it just is not remembered.
  }
}

/** May the scene play according to what this browser remembers alone? */
export function mayPlayLocal(storage, now, tz) {
  try {
    return mayPlay(now, (readLocal(storage) || {}).times || [], tz);
  } catch {
    return false;
  }
}
