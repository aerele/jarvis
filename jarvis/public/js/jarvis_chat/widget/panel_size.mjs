// The user-preferred mini-chat window size, persisted per browser.
//
// Widget.vue owns the localStorage I/O and the pointer events; this module is
// the pure parse + corner-drag geometry so every rule (never shrink below the
// shipped default, which way a drag grows the panel) is a node:test unit rather
// than a drag session in a browser — same split as fab_position.mjs.

import { PANEL_W, PANEL_MAX_H } from "./panel_anchor.mjs";

export const STORAGE_KEY = "jarvis-panel-size";

// The product rule: the panel can be GROWN but never shrunk below the shipped
// default. These are the floors resizeFrom enforces; panelLayout additionally
// caps the result to the viewport at render time, so a preference saved on a
// large monitor simply clamps down (never below the default) on a small one.
export const MIN_W = PANEL_W; // 400
export const MIN_H = PANEL_MAX_H; // 624

function num(x) {
  return typeof x === "number" && Number.isFinite(x) ? x : NaN;
}

// Parse the stored payload. Returns { width, height } floored at the default
// (so a tampered or pre-feature value can never encode a sub-default size), or
// null when there is nothing valid to restore.
export function parseSavedSize(raw) {
  if (!raw) return null;
  let obj;
  try {
    obj = JSON.parse(raw);
  } catch (e) {
    return null;
  }
  if (!obj || typeof obj !== "object") return null;
  const w = num(obj.width);
  const h = num(obj.height);
  if (Number.isNaN(w) || Number.isNaN(h)) return null;
  return { width: Math.max(MIN_W, w), height: Math.max(MIN_H, h) };
}

// Serialize for localStorage, floored at the default so a bad in-memory value
// can never be persisted as a sub-default size.
export function serializeSize(size) {
  const s = size || {};
  return JSON.stringify({
    width: Math.max(MIN_W, num(s.width) || MIN_W),
    height: Math.max(MIN_H, num(s.height) || MIN_H),
  });
}

// The new size from a corner drag. `start` is the size at pointer-down, `side`
// is which way the panel opens ("left" => grip on the top-left, panel grows
// leftward; anything else => grip on the top-right, grows rightward), and dx/dy
// are the pointer deltas since pointer-down. Height always grows by dragging UP
// (dy negative). Floored at the default so a drag can never shrink past it; the
// viewport cap is left to panelLayout so the stored preference keeps the user's
// intended size across viewport changes.
export function resizeFrom(start, side, dx, dy) {
  const s = start || {};
  const w0 = num(s.width) || MIN_W;
  const h0 = num(s.height) || MIN_H;
  const ddx = num(dx) || 0;
  const ddy = num(dy) || 0;
  const width = side === "left" ? w0 - ddx : w0 + ddx;
  const height = h0 - ddy;
  return {
    width: Math.max(MIN_W, Math.round(width)),
    height: Math.max(MIN_H, Math.round(height)),
  };
}
