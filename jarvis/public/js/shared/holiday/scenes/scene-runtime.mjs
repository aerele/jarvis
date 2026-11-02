// The runtime every day scene shares: beats, settle, replay, relayout and the flying copy of
// the worn item. The page geometry, the floor row and the pins on the mark are in
// scene-layout.mjs. A scene file only draws its own art and times it (see Scene's hooks below).
//
// Contract: play(host, {layout, getBoxes, year, idPrefix, reduced, onBeat, onError, replay})
//   -> {settle(mode), relayout(boxes), done}
//   getBoxes() -> {host, mark, heading, cards, hero?}, each {x, y, w, h} in px relative to
//     the host (host has only w and h). `hero` is the mark, heading and sub-line together.
//     Read at the start, again when the item is about to land (cfg.prep) and on relayout.
//   onBeat(name, ...args) drives the page side:
//     "peek"            the face shows (the item is not on yet)
//     "look", dx, dy    the eyes move, in the mark's 56-unit tile space
//     "deliver"         the item appears on the mark
//     "happy"           the smile
//     "swap", {instant} the greeting changes to the day greeting
//     "settle", mode    the scene is ending: the page fades its cards in
//   onError(error): a failure inside a timer or a beat; the scene goes on and reaches rest.
//   relayout(boxes): the page changed size while it plays; boxes as getBoxes().
//   settle(mode): "end" fades the art over 600 ms, "early" over 250 ms and lands the item,
//     "instant" does nothing visible. `done` always resolves and never rejects.
//   `replay` plays again over a screen whose cards are showing: no floor props, no greeting
//   swap.
// A play() that throws leaves nothing mounted. The desktop and phone layouts with motion get
// the scene; reduced motion and any other layout get the resting state at once. (Whether a
// phone screen has room for the scene is the page's call, scene-phone.mjs phoneSceneFits: it
// asks before loading the art and passes "rest" when there is none.)
//
// The stage: 800 units wide with x centred on the mark's column (400) and y down from the top
// of the art layer; the page scales a unit to s = min(width / 800, 1.25) px. The phone
// layout draws the same stage through the 540-unit window 130..670, at width / 540 px a
// unit (scene-phone.mjs).
import { r2 } from "../stage.mjs";
import { SceneLayout } from "./scene-layout.mjs";

export const FADE_MS = { end: 600, early: 250, instant: 0 };
const HAPPY_MS = 1500;
const DRAWN_LAYOUTS = ["desktop", "phone"];

/** A scene that only puts the page in its resting state (reduced motion, no room). */
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

/** The entry every scene's play() is: desktop or phone with motion gets the scene, the rest get rest. */
export function playScene(SceneClass, host, opts) {
  if (!DRAWN_LAYOUTS.includes(opts.layout) || opts.reduced)
    return restingScene(opts.onBeat);
  const scene = new SceneClass(host, opts);
  try {
    return scene.start();
  } catch (error) {
    // Whatever start() had mounted or begun must not outlive the failure.
    scene.stage.dispose();
    throw error;
  }
}

export class Scene extends SceneLayout {
  /**
   * cfg, times in ms from the start of the scene:
   *   end   the scene fades out;       land  the item lands on the mark;
   *   swap  the greeting changes;       face  the face settles after the smile;
   *   prep  the mark is read again and prepare() runs: the moment the item (or whatever lands
   *         with it) leaves, so it ends up on the mark where the page has it by then;
   *   look  [dx, dy, offset] glances: the eyes go to (dx, dy) at offset * land;
   *   row   (optional) the floor row's extent: scene-art.mjs floorRow;
   *   foldMid (optional, with a row) the row's middle group is taken out when the hero has no
   *         room for it, at the start (the scene draws without it) or on a later relayout.
   */
  constructor(host, opts, cfg) {
    super(host, opts, cfg);
    this.prep = cfg.prep;
    this.settled = false;
    this.swapped = false;
    this.delivered = false;
    this.tail = []; // timers that outlive an early settle (the smile ending)
    this.tailDone = false;
    this.flight = null; // the flying copy's animation, once it has started
  }

  // ---- what a scene fills in -------------------------------------------------------------
  // The hooks, all optional. `b` is the page's boxes (getBoxes()), `geo` what measure() works
  // out from them (scene-layout.mjs measure), both read as the page stands now.
  //
  //   extra(b, geo) -> {...}  more numbers for geo. These are numbers that FOLLOW THE PAGE:
  //                           they are worked out again on every measure (the start, the
  //                           prep moment and every relayout).
  //   px(geo) -> markup       drawn once in px space, over the whole layer (style, defs, sky).
  //   inner(geo) -> markup    drawn ONCE, at the start, in stage units (the main group). Later
  //                           the page can only move what is pinned to the mark or in the
  //                           floor row; everything else stays as it was drawn.
  //   playArt()               the art's own timeline, once, right after the first drawing.
  //   prepare()               runs once, at cfg.prep, with geo read again: starts what
  //                           depends on where the mark is by then (the item's flight, the
  //                           sparkles where it lands). cfg.land - cfg.prep is `untilLand()`.

  px() {
    return "";
  }

  inner() {
    return "";
  }

  playArt() {}

  prepare() {}

  // ---- the lifecycle ---------------------------------------------------------------------

  start() {
    const { opts, cfg, stage, p } = this;
    this.geo = this.measure();
    this.floorBottom = this.geo.floor ? this.geo.floor.bottom : 0;
    const { W, H, s, cxPx } = this.geo;
    const group = `<g class="${p}main" transform="translate(${r2(
      cxPx
    )} 0) scale(${r2(s)}) translate(-400 0)">${this.inner(this.geo)}</g>`;
    stage.mount(W, H, this.px(this.geo) + group);
    this.beat("peek");
    this.look();
    this.playArt();
    stage.at(this.prep, () => this.remeasure());
    stage.at(cfg.land, () => this.land());
    if (!opts.replay) stage.at(cfg.swap, () => this.swap(false));
    stage.at(cfg.face, () => this.beat("peek"));
    stage.at(cfg.end, () => this.settle("end"));
    return {
      settle: (mode) => this.settle(mode),
      relayout: (boxes) => this.relayout(boxes),
      done: stage.done,
    };
  }

  // The page changed size under the scene (the font swapped in, the cards arrived): take
  // the new boxes, so the item lands on the mark where it now is. The art already drawn
  // stays as it is, apart from the floor group and whatever is pinned to the mark.
  relayout(boxes) {
    if (this.settled || !this.geo) return;
    this.geo = { ...this.geo, ...this.measure(boxes) };
    this.refit();
  }

  remeasure() {
    this.geo = { ...this.geo, ...this.measure() };
    this.refit();
    this.prepare();
  }

  /** Flies the item's copy; `frames` is the keyframes, or a function of the geometry now. */
  fly(frames, options) {
    const keys = typeof frames === "function" ? frames(this.geo) : frames;
    this.flight = this.stage.animate(this.stage.q(`.${this.p}drop`), keys, {
      fill: "forwards",
      ...options,
    });
    return this.flight;
  }

  /** Adds markup to the scene's main group (used once the mark has been read again). */
  append(markup) {
    const main = this.stage.q(`.${this.p}main`);
    if (main) main.insertAdjacentHTML("beforeend", markup);
  }

  /** ms from the moment prepare() runs to `extra` ms after the item lands. */
  untilLand(extra = 0) {
    return this.cfg.land - this.prep + extra;
  }

  // ---- the glances -----------------------------------------------------------------------

  look() {
    const { land, look } = this.cfg;
    look.forEach(([dx, dy, f]) =>
      this.stage.at(f * land, () => this.beat("look", dx, dy))
    );
  }

  // ---- the beats -------------------------------------------------------------------------

  /** Tell the page. A page that throws is reported (opts.onError), never allowed to stop us. */
  beat(...args) {
    try {
      this.opts.onBeat(...args);
    } catch (error) {
      this.stage.fail(error);
    }
  }

  swap(instant) {
    if (this.swapped || this.opts.replay) return;
    this.swapped = true;
    this.beat("swap", { instant });
  }

  land() {
    this.delivered = true;
    this.beat("look", 0, 0);
    this.beat("deliver");
    this.beat("happy");
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
    this.stage.clearTimers();
    this.beat("settle", mode);
    if (mode === "instant") {
      this.swap(true);
      this.beat("deliver");
      this.beat("peek");
      this.stage.dispose();
      return;
    }
    if (mode === "early") this.landEarly();
    this.stage.fadeOut(FADE_MS[mode] || 0);
  }

  // The greeting changes, the item lands with its smile, then the face settles. Whatever
  // part of this had already happened is left alone.
  landEarly() {
    this.swap(false);
    // The item is on the mark now: its flying copy must not show beside it.
    this.cancelFlight();
    if (!this.delivered) this.land();
    this.tail.push(setTimeout(() => this.finishTail(false), HAPPY_MS));
  }

  // A browser that refuses to cancel must not stop the settle before it fades out.
  cancelFlight() {
    try {
      if (this.flight) this.flight.cancel();
    } catch (error) {
      this.stage.fail(error);
    }
  }

  finishTail(now) {
    this.tail.forEach(clearTimeout);
    this.tail = [];
    if (this.tailDone) return;
    this.tailDone = true;
    if (!now || this.delivered) this.beat("peek");
  }
}
