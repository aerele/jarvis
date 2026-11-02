// Reads the server's holiday answer (jarvis/holiday_touch, and its answer_cases.json)
// and says whether it is still today's. Pure functions: no DOM, never throws. Text
// only, so it stays in the main bundle.
import { OCCASIONS } from "./copy.mjs";

export const KINDS = ["none", "leadup", "day", "birthday"];
export const DESIGNS = [
  "birthday",
  "christmas",
  "diwali",
  "pongal",
  "eid",
  "newyear",
  "onam",
  "holi",
  "independence",
];

const NONE = { v: 1, kind: "none" };

/**
 * Turn whatever the page or endpoint gave us into a known-shape answer.
 * Additive versioning: any integer v >= 1 is read, unknown fields are dropped,
 * and an unknown kind, design or occasion means none. Returns a new object.
 */
export function readAnswer(raw) {
  try {
    return parse(raw);
  } catch {
    return { ...NONE };
  }
}

function parse(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { ...NONE };
  if (!Number.isInteger(raw.v) || raw.v < 1) return { ...NONE };
  if (!KINDS.includes(raw.kind)) return { ...NONE };
  const stamp =
    isIsoDate(raw.date) && isText(raw.tz)
      ? { date: raw.date, tz: raw.tz }
      : null;
  if (raw.kind === "none") return { v: raw.v, ...stamp, kind: "none" };
  // Staleness needs the date and zone, so a holiday without them cannot be trusted.
  if (!stamp || !DESIGNS.includes(raw.design)) return { ...NONE };
  const occasion = raw.occasion === undefined ? raw.design : raw.occasion;
  if (!OCCASIONS.includes(occasion)) return { ...NONE };
  return {
    v: raw.v,
    ...stamp,
    kind: raw.kind,
    design: raw.design,
    occasion,
    year: Number.isInteger(raw.year) ? raw.year : null,
    worn: raw.worn === true,
    scene: raw.scene === true,
    may_play: raw.may_play !== false,
  };
}

const isText = (value) => typeof value === "string" && value !== "";

function isIsoDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value))
    return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return (
    !Number.isNaN(parsed.getTime()) &&
    parsed.toISOString().slice(0, 10) === value
  );
}

/** Today's date (YYYY-MM-DD) in a zone. Throws RangeError for an invalid zone. */
function todayIn(tz, now) {
  // Read the parts, not a formatted string: the order and separators of a formatted
  // date are the locale's business, the parts are not.
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const part = (type) => parts.find((p) => p.type === type).value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/**
 * True when the answer is for another day than today in its own zone. A bad zone
 * counts as stale: when we cannot tell, we ask the server again.
 */
export function isStale(answer, now = new Date()) {
  if (!answer || !answer.date) return false; // an undated none has nothing to go stale
  try {
    return todayIn(answer.tz, now) !== answer.date;
  } catch {
    return true;
  }
}
