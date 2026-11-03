// The welcome state: a greeting and a few starting points, so an empty panel
// suggests what to do instead of showing a blank box.
//
// Ported from the side-chat design board, minus its visual identity — the
// board's purple accent and decorative animation are off-language per
// design.md 2.2 and 1.3, so only the copy and the layout idea carry over.
//
// Pure: the greeting is a function of the hour, and the prompts are a function
// of what the user is looking at, which makes both unit-testable.
import {
  TILE_UNITS,
  WORN_GAP_PX,
  WORN_OVERHANG,
} from "../../shared/holiday/worn-room.mjs";

export function greetingFor(hour) {
  const h = Number.isFinite(hour) ? hour : 12;
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

// `name` is a display name; take the first word so "Good evening, Kavin" reads
// like a person talking rather than a database row.
export function greetingLine(hour, name) {
  const g = greetingFor(hour);
  const first = String(name || "")
    .trim()
    .split(/\s+/)[0];
  return first ? `${g}, ${first}` : g;
}

// Starting points, scoped to whatever the panel can see. On a record the
// generic "which sales orders are overdue" prompt is a worse suggestion than
// one about the record already on screen.
export function suggestionsFor(ctx) {
  const label = ctx && typeof ctx === "object" ? ctx : null;

  if (label && label.doctype && label.name) {
    return [
      {
        title: "Explain this record",
        prompt: "Summarise this record and flag anything unusual.",
      },
      {
        title: "Find what is linked",
        prompt: "What documents are linked to this one?",
      },
      {
        title: "Suggest next steps",
        prompt: "What should I do next with this record?",
      },
    ];
  }

  if (label && (label.doctype || label.report_name)) {
    return [
      {
        title: "Analyse data",
        prompt: "Which of these need my attention first?",
      },
      { title: "Summarise", prompt: "Summarise what I am looking at." },
      {
        title: "Find outliers",
        prompt: "Is anything here unusual or out of place?",
      },
    ];
  }

  return [
    {
      title: "Analyse data",
      prompt: "Which sales orders are overdue this month?",
    },
    { title: "Take an action", prompt: "Draft a document for me to review." },
    { title: "Search records", prompt: "Search for a customer or contact." },
  ];
}

// ---- holiday touches ----
// The hero tile, and the padding above it in the welcome (Panel.vue .jvp-hero, .jvp-welcome;
// holiday_geometry.test.mjs holds these to the CSS).
export const HERO_PX = 52;
export const HERO_RADIUS_PX = 14;
export const WELCOME_PAD_TOP_PX = 24;

/**
 * Extra room above the hero for the item it wears, in px: the item's reach above the tile
 * (the tile unit table, scaled to the hero) plus the gap, less the padding already there.
 * Known before the art loads, so the hero does not jump when the item appears. A design
 * whose item sits on or beside the tile (no table entry) needs none.
 */
export function heroTopRoom(design) {
  if (!Object.hasOwn(WORN_OVERHANG, design)) return 0;
  const reach = (WORN_OVERHANG[design] * HERO_PX) / TILE_UNITS;
  return Math.max(0, Math.ceil(reach + WORN_GAP_PX - WELCOME_PAD_TOP_PX));
}

/**
 * The words on the welcome: on a day or birthday the heading replaces the greeting; on a
 * lead-up the normal greeting stays and the advance wish goes under it. A hold (upgrade
 * maintenance) shows the plain greeting: the mark is asleep and has nothing to celebrate.
 */
export function welcomeLines(greeting, view, { hold = false } = {}) {
  if (hold || !view) return { greeting, leadupLine: null };
  return {
    greeting: view.heading || greeting,
    leadupLine: view.leadupLine || null,
  };
}

/**
 * Is the welcome block open right now? A "welcome open" is the moment it becomes visible while
 * the panel is open: a first open with no conversation, or a new chat. It counts only once this
 * open's load has settled (the empty frame before it is not one), never before the readiness
 * verdict is known (an unset-up workspace has the setup nudge, not a welcome), never on the
 * setup gate, under the upgrade hold, or with anything in the thread.
 */
export function isWelcomeOpen({
  open,
  settled,
  enabled,
  maintenance,
  readiness,
  loading,
  hasMessages,
  loadError,
  streaming,
  thinking,
}) {
  const known = readiness === "ready" || readiness === "degraded";
  const calm = !loading && !loadError && !streaming && !thinking;
  return (
    !!(open && settled && enabled) &&
    !maintenance &&
    known &&
    calm &&
    !hasMessages
  );
}
