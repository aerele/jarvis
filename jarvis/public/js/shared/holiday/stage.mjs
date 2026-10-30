// Shared helpers for the holiday scenes (lazy art: this file and scenes/*.mjs load only
// when a scene may play). Framework-free: it touches the DOM only through the host it is
// given.

export const STAR =
  "M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z";

export const r2 = (n) => Math.round(n * 100) / 100;

/** Small seeded random numbers, so a scene looks the same on every showing. */
export function rng(seed) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const lin = (id, stops, x2 = 1, y2 = 1, x1 = 0, y1 = 0) =>
  `<linearGradient id="${id}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}">${stops
    .map(
      ([o, c, op = 1]) =>
        `<stop offset="${o}" stop-color="${c}" stop-opacity="${op}"/>`
    )
    .join("")}</linearGradient>`;

export const rad = (id, stops) =>
  `<radialGradient id="${id}">${stops
    .map(
      ([o, c, op = 1]) =>
        `<stop offset="${o}" stop-color="${c}" stop-opacity="${op}"/>`
    )
    .join("")}</radialGradient>`;

export const ring = (n, cx, cy, radius, fn, offset = 0) =>
  Array.from({ length: n }, (_, i) => {
    const a = (((i / n) * 360 + offset) * Math.PI) / 180;
    return fn(r2(cx + Math.cos(a) * radius), r2(cy + Math.sin(a) * radius), i);
  }).join("");

/** A CSS translate in SVG user units. */
export const tr = (x, y, extra = "") =>
  `translate(${r2(x)}px, ${r2(y)}px)${extra}`;

/** Keyframes of a thrown object: steps along a parabola, fading out in the last 45%. */
export function ballistic(vx, vy, g, dur, steps = 8) {
  return Array.from({ length: steps + 1 }, (_, k) => {
    const t = (dur / 1000) * (k / steps);
    const f = k / steps;
    return {
      transform: tr(vx * t, vy * t + 0.5 * g * t * t),
      opacity: f < 0.55 ? 1 : 1 - (f - 0.55) / 0.45,
    };
  });
}

/**
 * Keyframes along the quadratic from `from` through `ctrl` to `to` (points in stage units),
 * `steps` samples, the last one faded out. `spin` degrees are unwound along the way.
 */
export function quadPath(from, ctrl, to, steps, spin = 0) {
  return Array.from({ length: steps + 1 }, (_, k) => {
    const t = k / steps;
    const at = (i) =>
      (1 - t) ** 2 * from[i] + 2 * (1 - t) * t * ctrl[i] + t * t * to[i];
    return {
      transform: tr(
        at(0),
        at(1),
        spin ? ` rotate(${r2(spin * (1 - t))}deg)` : ""
      ),
      opacity: k === steps ? 0 : 1,
    };
  });
}

/** Sparks of a burst: each a short streak with a bright head, aimed along `data-a`. */
export function burstSVG(cls, n, colours, seed) {
  const rand = rng(seed);
  return Array.from({ length: n }, (_, i) => {
    const a = (i / n) * 360 + rand() * 14;
    const c = colours[i % colours.length];
    const len = 5 + rand() * 5;
    return `<g class="${cls}" data-a="${r2(
      a
    )}" opacity="0"><g transform="rotate(${r2(a)})">
      <path d="M${r2(
        -len
      )} 0 H0" stroke="${c}" stroke-width="2" stroke-linecap="round" opacity=".75"/>
      <circle r="${r2(1.5 + rand() * 0.9)}" fill="${c}"/></g></g>`;
  }).join("");
}

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * One scene's drawing, its animations and its timers, so that cancelling it is one call.
 * Everything it starts is tracked; nothing outlives `dispose()`.
 */
export class Stage {
  /** `onError(error)` hears every error a timer callback throws (see at). */
  constructor(host, onError) {
    this.host = host;
    this.onError = onError;
    this.svg = null;
    this.anims = [];
    this.timers = [];
    this.finish = null;
    // Always resolves, never rejects: a cancelled scene must not reach the
    // unhandledrejection hook.
    this.done = new Promise((resolve) => (this.finish = resolve));
  }

  /** Draw `inner` (SVG markup) into a layer that fills the host. */
  mount(width, height, inner) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${r2(width)} ${r2(height)}`);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", "100%");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.style.cssText = "position:absolute;inset:0;overflow:hidden";
    svg.innerHTML = inner;
    this.host.appendChild(svg);
    this.svg = svg;
    return svg;
  }

  q(selector) {
    return this.svg ? this.svg.querySelector(selector) : null;
  }

  qa(selector) {
    return this.svg ? [...this.svg.querySelectorAll(selector)] : [];
  }

  /**
   * Web Animations call, remembered so it can be cancelled. Null when unsupported or when the
   * browser refuses these keyframes: one bad animation must not take the scene down.
   */
  animate(el, frames, options = {}) {
    if (!el || typeof el.animate !== "function") return null;
    let anim;
    try {
      anim = el.animate(frames, { fill: "both", ...options });
    } catch {
      return null;
    }
    this.anims.push(anim);
    return anim;
  }

  /**
   * A timer that dispose() clears. A callback that throws is handed to onError and the other
   * timers go on, so the scene still reaches its end.
   */
  at(ms, fn) {
    const id = setTimeout(() => {
      this.timers = this.timers.filter((t) => t !== id);
      try {
        fn();
      } catch (error) {
        this.fail(error);
      }
    }, ms);
    this.timers.push(id);
    return id;
  }

  /** Tell onError about a failure; an onError that throws is ignored. */
  fail(error) {
    try {
      if (this.onError) this.onError(error);
    } catch {
      // Reporting is best effort: it must not stop the scene.
    }
  }

  clearTimers() {
    this.timers.forEach(clearTimeout);
    this.timers = [];
  }

  /** Fade the whole layer out over `ms`, then dispose. `ms` 0 disposes at once. */
  fadeOut(ms) {
    if (!ms || !this.svg) return this.dispose();
    this.animate(this.svg, [{ opacity: 1 }, { opacity: 0 }], {
      duration: ms,
      easing: "ease",
    });
    this.at(ms + 30, () => this.dispose());
  }

  dispose() {
    this.clearTimers();
    this.anims.forEach((a) => {
      try {
        a.cancel();
      } catch {
        // Already finished or detached: nothing to cancel.
      }
    });
    this.anims = [];
    if (this.svg) this.svg.remove();
    this.svg = null;
    this.finish();
  }
}
