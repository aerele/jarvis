// The Diwali scene (desktop stage layout), ported from the approved desktop mock.
//
// Contract: play(host, {layout, getBoxes, year, idPrefix, reduced, onBeat, replay})
//   -> {settle(mode), done}
//   getBoxes() -> {host, mark, heading, cards}, each {x, y, w, h} in px relative to the
//     host (host has only w and h). Read at the start and again when the item is lit.
//   onBeat(name, ...args) drives the page side:
//     "peek"            the face shows (the item is not on yet)
//     "look", dx, dy    the eyes move, in the mark's 56-unit tile space
//     "deliver"         the item appears on the mark
//     "happy"           the smile
//     "swap", {instant} the greeting changes to the day greeting
//     "settle", mode    the scene is ending: the page fades its cards in
//   relayout(boxes), optional: the page changed size while it plays; boxes as getBoxes().
//   settle(mode): "end" fades the art over 600 ms, "early" over 250 ms and lands the item,
//     "instant" does nothing visible. `done` always resolves and never rejects.
// `replay` plays again over a screen whose cards are showing: no floor props, no greeting swap.
import { Stage, ballistic, r2, rng, tr } from "../stage.mjs";
import {
  FLAME_TILE,
  FLASHES,
  LAND_MS,
  PEAK,
  defsSVG,
  floorSVG,
  lightSVG,
  skyShowSVG,
  skySVG,
  styleSVG,
  toranSVG,
} from "./diwali-art.mjs";

export const END_MS = 7600;
export const FADE_MS = { end: 600, early: 250, instant: 0 };
const SWAP_MS = 4350;
const SETTLE_FACE_MS = 5800;
const HAPPY_MS = 1500;

// The flash table (FLASHES, PEAK) lives with the drawing in diwali-art.mjs, so the radius
// drawn and the timing used here come from one place; it is re-exported for the tests.
export { FLASHES, PEAK };

// [dx, dy, offset] of the eyes over the first 4080 ms: down at the diyas, up at the sky,
// then to the flame.
const LOOK = [
  [-9, 3, 0],
  [-9, 3, 0.06],
  [6, 3, 0.36],
  [-6, -7, 0.6],
  [8, -7, 0.75],
  [9, -6, 0.82],
  [3, -1, 1],
];

/** A scene that only puts the page in its resting state (reduced motion, phone layout). */
function restingScene(onBeat) {
  let done = false;
  const rest = () => {
    if (done) return;
    done = true;
    onBeat("settle", "instant");
    onBeat("swap", { instant: true });
    onBeat("deliver");
    onBeat("peek");
  };
  return { settle: rest, done: Promise.resolve() };
}

export function play(host, opts) {
  const { layout, reduced, onBeat } = opts;
  if (layout !== "desktop" || reduced) return restingScene(onBeat);
  return new DiwaliScene(host, opts).start();
}

class DiwaliScene {
  constructor(host, opts) {
    this.opts = opts;
    this.stage = new Stage(host);
    this.p = `${String(opts.idPrefix || "jvs").replace(/[^\w-]/g, "")}-`;
    this.settled = false;
    this.swapped = false;
    this.delivered = false;
    this.tail = []; // timers that outlive an early settle (the smile ending)
    this.geo = null;
  }

  start() {
    const beat = this.opts.onBeat;
    this.geo = this.measure();
    const { W, H, s, cxPx, dy, ground, skyPx, from, to } = this.geo;
    const p = this.p;
    const inner = `${styleSVG(p)}${defsSVG(p)}${skySVG(p, W, skyPx)}
      <g transform="translate(${r2(cxPx)} 0) scale(${r2(s)}) translate(-400 0)">
        ${skyShowSVG(p, dy)}${toranSVG(p, from, to)}${
      this.opts.replay ? "" : floorSVG(p, ground)
    }
      </g>`;
    this.stage.mount(W, H, inner);
    beat("peek");
    this.look();
    this.playArt();
    this.stage.at(LAND_MS - 780, () => this.light());
    this.stage.at(LAND_MS, () => this.land());
    if (!this.opts.replay) this.stage.at(SWAP_MS, () => this.swap(false));
    this.stage.at(SETTLE_FACE_MS, () => beat("peek"));
    this.stage.at(END_MS, () => this.settle("end"));
    return {
      settle: (mode) => this.settle(mode),
      relayout: (boxes) => this.relayout(boxes),
      done: this.stage.done,
    };
  }

  // The page changed size under the scene (the font swapped in, the cards arrived): take
  // the new boxes, so the item is lit on the mark where it now is. The sky already drawn
  // stays as it is.
  relayout(boxes) {
    if (this.settled || !this.geo) return;
    this.geo = { ...this.geo, ...this.measure(boxes) };
  }

  // Where things stand now, in the mock's 800-unit space (x centred on the mark).
  measure(boxes) {
    const b = boxes || this.opts.getBoxes();
    const W = b.host.w;
    const s = Math.min(W / 800, 1.25);
    const markU = b.mark.w / s;
    const cxPx = b.mark.x + b.mark.w / 2;
    const skyEnd = Math.min(b.heading.y - 4, b.mark.y + 120 * s);
    return {
      W,
      H: b.host.h,
      s,
      cxPx,
      dy: b.mark.y / s - 84,
      ground: (b.cards.y + b.cards.h) / s - 2,
      skyPx: Math.max(40, skyEnd / 0.52),
      from: 400 - cxPx / s,
      to: 400 + (W - cxPx) / s,
      flame: [
        400 + (FLAME_TILE[0] - 28) * (markU / 56),
        b.mark.y / s + FLAME_TILE[1] * (markU / 56),
      ],
    };
  }

  look() {
    LOOK.forEach(([dx, dy, f]) =>
      this.stage.at(f * LAND_MS, () => this.opts.onBeat("look", dx, dy))
    );
  }

  swap(instant) {
    if (this.swapped || this.opts.replay) return;
    this.swapped = true;
    this.opts.onBeat("swap", { instant });
  }

  land() {
    this.delivered = true;
    this.opts.onBeat("look", 0, 0);
    this.opts.onBeat("deliver");
    this.opts.onBeat("happy");
  }

  // The art's own timeline: toran, flames, fountains, rockets and bursts.
  playArt() {
    const { stage, p } = this;
    const q = (n) => stage.qa(`.${p}${n}`);
    stage.animate(
      stage.q(`.${p}sky`),
      [
        { opacity: 0 },
        { opacity: 1, offset: 0.1 },
        { opacity: 1, offset: 0.78 },
        { opacity: 0 },
      ],
      { duration: END_MS }
    );
    stage.animate(
      stage.q(`.${p}toran`),
      [
        { transform: tr(0, -52), opacity: 1 },
        { transform: tr(0, 4), opacity: 1, offset: 0.7 },
        { transform: tr(0, 0), opacity: 1 },
      ],
      { duration: 850, delay: 100, easing: "ease-out" }
    );
    q("dflame").forEach((f, i) =>
      stage.animate(
        f,
        [
          { transform: "scale(0)" },
          { transform: "scale(1.18)", offset: 0.7 },
          { transform: "scale(1)" },
        ],
        { duration: 380, delay: 250 + i * 170, easing: "ease-out" }
      )
    );
    q("dglow").forEach((g, i) =>
      stage.animate(g, [{ opacity: 0 }, { opacity: 1 }], {
        duration: 500,
        delay: 300 + i * 170,
      })
    );
    const rand = rng(21);
    q("fount").forEach((f) => {
      const d = 900 + rand() * 500;
      stage.animate(
        f,
        ballistic((rand() - 0.5) * 70, -(150 + rand() * 110), 320, d),
        { duration: d, delay: 1450 + rand() * 2400, fill: "forwards" }
      );
    });
    this.rockets();
  }

  rockets() {
    const { geo } = this;
    const stage = this.stage;
    const base = geo.ground - 30;
    const launch = (n, x, to, delay) =>
      stage.animate(
        stage.q(`.${this.p}rk${n}`),
        [
          { transform: tr(x, base), opacity: 1 },
          { transform: tr(...to), opacity: 1, offset: 0.95 },
          { transform: tr(to[0], to[1] - 3), opacity: 0 },
        ],
        {
          duration: 650,
          delay,
          easing: "cubic-bezier(.3,.6,.4,1)",
          fill: "forwards",
        }
      );
    const dy = geo.dy;
    launch(1, 234, [250, 98 + dy], 1800);
    launch(2, 552, [560, 80 + dy], 2400);
    launch(3, 672, [680, 118 + dy], 3250);
    this.flashes();
    this.burst(".b1", 120, 2450, 3, 1300, 70);
    this.burst(".b2", 120, 3050, 6, 1300, 70);
    this.burst(".b3", 85, 3900, 9, 2300, 150);
  }

  flashes() {
    FLASHES.filter((f) => f.cls !== "lit").forEach((f) => this.flash(f, f.at));
  }

  flash(f, delay) {
    const el = this.stage.q(`.${this.p}${f.cls}`);
    this.stage.animate(
      el,
      [
        { opacity: 0, transform: "scale(.2)" },
        { opacity: 1, transform: "scale(1)", offset: 0.2 },
        { opacity: 0, transform: `scale(${PEAK})` },
      ],
      { duration: f.ms, delay, fill: "forwards" }
    );
  }

  burst(sel, speed, delay, seed, dur, gravity) {
    const rand = rng(seed);
    this.stage.qa(`.${this.p}${sel.slice(1)}`).forEach((el) => {
      const a = (Number(el.dataset.a) * Math.PI) / 180;
      const v = speed * (0.6 + rand() * 0.5);
      this.stage.animate(
        el,
        ballistic(Math.cos(a) * v, Math.sin(a) * v, gravity, dur),
        {
          duration: dur,
          delay,
          easing: "cubic-bezier(.15,.7,.3,1)",
          fill: "forwards",
        }
      );
    });
  }

  // The last spark of the second burst drifts down to the flame on the mark. The mark's
  // position is read again here, because the page may have moved since the scene began.
  light() {
    this.geo = { ...this.geo, ...this.measure() };
    const { flame, dy } = this.geo;
    const stage = this.stage;
    const group = stage.q("g");
    if (!group) return;
    group.insertAdjacentHTML("beforeend", lightSVG(this.p, flame[0], flame[1]));
    const [fx, fy] = flame;
    const carry = Array.from({ length: 13 }, (_, k) => {
      const t = k / 12;
      const x = (1 - t) ** 2 * 560 + 2 * (1 - t) * t * 492 + t * t * fx;
      const y =
        (1 - t) ** 2 * (80 + dy) + 2 * (1 - t) * t * (36 + dy) + t * t * fy;
      return { transform: tr(x, y), opacity: k === 12 ? 0 : 1 };
    });
    stage.animate(stage.q(`.${this.p}carry`), carry, {
      duration: 780,
      easing: "ease-in",
      fill: "forwards",
    });
    this.flash(
      FLASHES.find((f) => f.cls === "lit"),
      780
    );
  }

  settle(mode) {
    if (this.settled) {
      // A later instant settle (the page is leaving) also ends the fade and the smile's tail.
      if (mode === "instant") {
        this.finishTail(true);
        this.stage.dispose();
      }
      return;
    }
    this.settled = true;
    const { onBeat } = this.opts;
    this.stage.clearTimers();
    onBeat("settle", mode);
    if (mode === "instant") {
      this.swap(true);
      onBeat("deliver");
      onBeat("peek");
      this.stage.dispose();
      return;
    }
    if (mode === "early") this.landEarly();
    this.stage.fadeOut(FADE_MS[mode] || 0);
  }

  // The greeting changes, the item lands with its smile, then the face settles. Whatever
  // part of this had already happened is left alone.
  landEarly() {
    const { onBeat } = this.opts;
    this.swap(false);
    if (!this.delivered) {
      this.delivered = true;
      onBeat("look", 0, 0);
      onBeat("deliver");
      onBeat("happy");
    }
    this.tail.push(setTimeout(() => this.finishTail(false), HAPPY_MS));
  }

  finishTail(now) {
    this.tail.forEach(clearTimeout);
    this.tail = [];
    if (this.tailDone) return;
    this.tailDone = true;
    if (!now || this.delivered) this.opts.onBeat("peek");
  }
}
