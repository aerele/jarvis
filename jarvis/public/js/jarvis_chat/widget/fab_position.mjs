// Pure geometry/drag/idle-timer core for the floating Jarvis button (FAB).
// No DOM, no Vue, no storage access - Widget.vue owns the DOM/localStorage and
// only hands this module plain numbers/strings, so it can be unit tested with
// node:test and reused as-is once wired up.
//
// A saved position is { side: "left"|"right", yRatio: 0..1 } rather than raw
// pixels so it survives viewport resizes; pixel coordinates are derived from
// a `viewport` snapshot on demand:
//   viewport = { vw, vh, topInset, bottomInset, edgeInset, fabSize }

export const FAB_SIZE = 54; // px, matches .jvw-fab width/height
export const EDGE_INSET = 22; // px gap kept from the docked screen edge
export const TAP_THRESHOLD_PX = 6; // pointer travel below this is a tap, not a drag
export const SNAP_MS = 250; // edge-snap animation duration, ms
export const IDLE_FADE_MS = 8000; // inactivity delay before the FAB fades, ms
export const IDLE_OPACITY = 0.4;
export const STORAGE_KEY = "jarvis-fab-pos";

/** A center X at or past the viewport midpoint counts as the right side
 * (matches which edge a released drag snaps to). */
export function chooseSide(centerX, vw) {
  return centerX >= vw / 2 ? "right" : "left";
}

/** Clamps a top-left Y (px) into the dockable band [topInset, vh - bottomInset
 * - fabSize]. When the viewport is too short for that band to be positive,
 * it degenerates to the single point at topInset. */
export function clampY(y, viewport) {
  const { topInset, bottomInset, vh, fabSize } = viewport;
  const min = topInset;
  const max = Math.max(topInset, vh - bottomInset - fabSize);
  return Math.min(Math.max(y, min), max);
}

/** Docked X (px) for a side. Anything other than "left" resolves as "right",
 * the same default resolvePosition() falls back to for invalid input. */
export function xForSide(side, viewport) {
  const { vw, edgeInset, fabSize } = viewport;
  return side === "left" ? edgeInset : vw - edgeInset - fabSize;
}

/** Y (px) -> ratio within the dockable band. Non-finite input is treated as
 * the band's top; a degenerate band always reports ratio 0. */
export function yToRatio(y, viewport) {
  const { topInset, bottomInset, vh, fabSize } = viewport;
  const min = topInset;
  const max = Math.max(topInset, vh - bottomInset - fabSize);
  const range = max - min;
  const safeY = Number.isFinite(y) ? y : min;
  return range === 0 ? 0 : (clampY(safeY, viewport) - min) / range;
}

/** Inverse of yToRatio(). Non-finite or out-of-[0,1] ratios clamp first. */
export function ratioToY(ratio, viewport) {
  const { topInset, bottomInset, vh, fabSize } = viewport;
  const min = topInset;
  const max = Math.max(topInset, vh - bottomInset - fabSize);
  const safeRatio = Number.isFinite(ratio)
    ? Math.min(1, Math.max(0, ratio))
    : 0;
  return min + safeRatio * (max - min);
}

/** Parses the localStorage payload. Returns null for anything that isn't
 * `{ side: "left"|"right", yRatio: <finite number> }` (bad JSON, wrong
 * shape, invalid side, non-numeric/non-finite yRatio); a finite yRatio
 * outside [0, 1] is clamped rather than rejected. */
export function parseSavedPosition(raw) {
  if (typeof raw !== "string") return null;
  let obj;
  try {
    obj = JSON.parse(raw);
  } catch (_) {
    return null;
  }
  if (!obj || typeof obj !== "object") return null;
  const { side, yRatio } = obj;
  if (side !== "left" && side !== "right") return null;
  if (typeof yRatio !== "number" || !Number.isFinite(yRatio)) return null;
  return { side, yRatio: Math.min(1, Math.max(0, yRatio)) };
}

export function serializePosition({ side, yRatio }) {
  return JSON.stringify({ side, yRatio });
}

/** Combines a parsed (possibly null/malformed) saved position with a
 * bottom-right fallback. `viewport` is accepted for signature symmetry with
 * the rest of the render pipeline (xForSide/ratioToY); this step itself is
 * viewport-independent since positions are stored as a resolution-independent
 * ratio. */
export function resolvePosition(saved, viewport) {
  const valid =
    saved &&
    (saved.side === "left" || saved.side === "right") &&
    typeof saved.yRatio === "number" &&
    Number.isFinite(saved.yRatio);
  if (!valid) return { side: "right", yRatio: 1 };
  return { side: saved.side, yRatio: Math.min(1, Math.max(0, saved.yRatio)) };
}

/** Starts a drag session anchored at the FAB's current pixel position and the
 * pointer's down position. */
export function dragStart(fabX, fabY, px, py) {
  return {
    startFabX: fabX,
    startFabY: fabY,
    startPx: px,
    startPy: py,
    dragging: false,
  };
}

/** Advances the session to a new pointer position. `dragging` latches true
 * once the travelled distance reaches `threshold` and never un-latches, so a
 * drag that returns to its origin is still a drag, not a tap. */
export function dragMove(session, px, py, threshold) {
  const dist = Math.hypot(px - session.startPx, py - session.startPy);
  return {
    ...session,
    currentPx: px,
    currentPy: py,
    dragging: session.dragging || dist >= threshold,
  };
}

/** Resolves a session into the final docked position. `tap` is true only if
 * `dragging` never latched during the session. */
export function dragEnd(session, viewport) {
  const px = session.currentPx ?? session.startPx;
  const py = session.currentPy ?? session.startPy;
  const fabX = session.startFabX + (px - session.startPx);
  const fabY = session.startFabY + (py - session.startPy);
  const y = clampY(fabY, viewport);
  const side = chooseSide(fabX + viewport.fabSize / 2, viewport.vw);
  const x = xForSide(side, viewport);
  const yRatio = yToRatio(y, viewport);
  return { tap: !session.dragging, side, x, y, yRatio };
}

/** Inactivity timer built on injectable timer functions so it's testable
 * without real wall-clock waits. Starts counting down immediately on
 * creation; `poke()` restarts the countdown; `dispose()` cancels it and makes
 * further pokes a no-op. */
export function createIdleTimer({
  delayMs,
  onIdle,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
}) {
  let disposed = false;
  let handle = setTimeoutFn(onIdle, delayMs);
  return {
    poke() {
      if (disposed) return;
      clearTimeoutFn(handle);
      handle = setTimeoutFn(onIdle, delayMs);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      clearTimeoutFn(handle);
    },
  };
}
