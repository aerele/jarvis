// The runtime every day scene shares: beats, settle, replay, relayout, the page geometry,
// the floor row, pins on the mark and the flying copy of the worn item. A scene file only
// draws its own art and times it (see Scene's hooks below).
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
// A play() that throws leaves nothing mounted. Desktop layout with motion gets the scene; the
// phone layout and reduced motion get the resting state at once.
//
// The stage: 800 units wide with x centred on the mark's column (400) and y down from the top
// of the art layer; the page scales a unit to s = min(width / 800, 1.25) px.
import { Stage, r2 } from "../stage.mjs";
import {
  CLEAR,
  GAP,
  HERO_FLOOR_GAP_PX,
  MARK_TOP,
  fitPoint,
  floorFit,
  floorTransform,
  groundOf,
} from "../scene-geometry.mjs";
import { heroKeepOut } from "./scene-place.mjs";
import { SceneMotion } from "./scene-motion.mjs";
import { itemSVG } from "./scene-art.mjs";

export const FADE_MS = { end: 600, early: 250, instant: 0 };
const HAPPY_MS = 1500;

// The prefix of every id and class the scene draws: the page's idPrefix without anything odd,
// and starting with a letter (a class or id may not start with a digit in a CSS selector).
const prefixOf = (id) => {
  const clean = String(id || "jvs").replace(/[^\w-]/g, "");
  return `${/^[A-Za-z_]/.test(clean) ? clean : `j${clean}`}-`;
};

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

/** The entry every scene's play() is: desktop with motion gets the scene, the rest get rest. */
export function playScene(SceneClass, host, opts) {
  if (opts.layout !== "desktop" || opts.reduced)
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

export class Scene extends SceneMotion {
  /**
   * cfg, times in ms from the start of the scene:
   *   end   the scene fades out;       land  the item lands on the mark;
   *   swap  the greeting changes;       face  the face settles after the smile;
   *   prep  the mark is read again and prepare() runs: the moment the item (or whatever lands
   *         with it) leaves, so it ends up on the mark where the page has it by then;
   *   look  [dx, dy, offset] glances: the eyes go to (dx, dy) at offset * land;
   *   row   (optional) the floor row's extent: scene-art.mjs floorRow.
   */
  constructor(host, opts, cfg) {
    super();
    this.opts = opts;
    this.cfg = cfg;
    this.prep = cfg.prep;
    this.stage = new Stage(host, opts.onError);
    this.p = prefixOf(opts.idPrefix);
    this.settled = false;
    this.swapped = false;
    this.delivered = false;
    this.tail = []; // timers that outlive an early settle (the smile ending)
    this.tailDone = false;
    this.geo = null;
    this.flight = null; // the flying copy's animation, once it has started
    this.floorBottom = 0; // where the floor row was drawn; relayout moves it from there
  }

  // ---- what a scene fills in -------------------------------------------------------------
  // The hooks, all optional. `b` is the page's boxes (getBoxes()), `geo` what measure() works
  // out from them (see below), both read as the page stands now.
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

  extra() {
    return {};
  }

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

  // Where things stand now, in stage units (x centred on the mark's column). geo has: the
  // layer W x H px; s px per unit; hu the layer's height in units; cxPx the mark's centre in
  // px; k units per tile unit of the mark; mt the mark's top; ground the floor row's baseline;
  // floor its fit (floorFit; null without a row); skyPx the sky wash's height; from..to the
  // stage's x extent; keep the hero's box (heroKeepOut); shift how far the mark sits below
  // where the art was drawn for (MARK_TOP), for props that follow it; airTop..airBottom the
  // room for sky decorations above the hero.
  measure(boxes) {
    const b = boxes || this.opts.getBoxes();
    const [W, H] = [b.host.w, b.host.h];
    const s = Math.min(W / 800, 1.25);
    const cxPx = b.mark.x + b.mark.w / 2;
    const mt = b.mark.y / s; // the mark's top
    const ground = groundOf(b.cards.y + b.cards.h, s);
    const skyEnd = Math.min(b.heading.y - 4, b.mark.y + 120 * s);
    const geo = {
      W,
      H,
      s,
      hu: H / s,
      cxPx,
      k: b.mark.w / s / 56, // units per tile unit of the mark
      mt,
      ground,
      floor: this.cfg.row
        ? floorFit({
            row: this.cfg.row,
            ground,
            hostH: H,
            s,
            // The part of the row under the hero keeps clear of the hero's bottom (the
            // sub-line's), once the page gives the hero's box.
            clearTop: b.hero
              ? (b.hero.y + b.hero.h + HERO_FLOOR_GAP_PX) / s
              : undefined,
          })
        : null,
      skyPx: Math.max(40, skyEnd / 0.52),
      from: 400 - cxPx / s,
      to: 400 + (W - cxPx) / s,
      keep: heroKeepOut(b, s),
      shift: mt - MARK_TOP, // how far the mark sits below MARK_TOP, for props that follow it
      airTop: CLEAR, // the sky decorations live between here ...
      airBottom: mt - GAP, // ... and the top of the hero
    };
    return { ...geo, ...this.extra(b, geo) };
  }

  // The floor row follows the stage height and the ground, and the pins follow the mark.
  refit() {
    const { geo, p, stage } = this;
    const row = stage.q(`.${p}floor`);
    if (row && geo.floor) row.setAttribute("transform", this.floorAt());
    stage.qa(`.${p}edge`).forEach((el) => {
      el.setAttribute("transform", this.edgeAt(Number(el.dataset.x)));
    });
    stage.qa(`.${p}pin`).forEach((el) => {
      const { tx, ty, sc } = el.dataset;
      el.setAttribute(
        "transform",
        this.pinAt(Number(tx), Number(ty), sc === "1")
      );
    });
  }

  // ---- the floor row ---------------------------------------------------------------------

  /** The floor group's transform now. It was drawn at floorBottom, so it starts from there. */
  floorAt() {
    return floorTransform({ ...this.geo.floor, bottom: this.floorBottom });
  }

  /** The floor group: `content` is drawn on this stage's ground (see onGround). */
  floorGroup(content) {
    return `<g class="${
      this.p
    }floor" transform="${this.floorAt()}">${content}</g>`;
  }

  /**
   * `content`, drawn at x in the floor row's own units, kept at x while the row shrinks. The
   * row shrinks about x = 400, so a prop at the stage's edge would drift inward with it;
   * this prop shrinks about its own place instead. Only for the floor row's content.
   */
  edge(x, content) {
    return `<g class="${this.p}edge" data-x="${x}" transform="${this.edgeAt(
      x
    )}">${content}</g>`;
  }

  /** The shift that holds a prop drawn at x in place, at the row's scale now. */
  edgeAt(x) {
    // The row's transform is drawn with its scale rounded to 0.01: hold with that one.
    const scale = this.geo.floor ? r2(this.geo.floor.scale) : 1;
    return `translate(${r2((x - 400) * (1 / scale - 1))} 0)`;
  }

  /** `content` drawn at the art's own floor y: stood on the ground at the row's scale. */
  onGround(content) {
    const { base, g } = this.cfg.row;
    return `<g transform="translate(400 ${r2(
      this.geo.ground
    )}) scale(${g}) translate(-400 ${-base})">${content}</g>`;
  }

  /** Where a point of the drawn floor row stands now. */
  fitted(point) {
    return fitPoint({ ...this.geo.floor, bottom: this.floorBottom }, point);
  }

  // ---- things fixed to the mark ----------------------------------------------------------

  /** A point of the mark's 56-unit tile, in stage units. */
  tile(tx, ty) {
    const { k, mt } = this.geo;
    return [400 + (tx - 28) * k, mt + ty * k];
  }

  pinAt(tx, ty, scaled) {
    const [x, y] = this.tile(tx, ty);
    const at = `translate(${r2(x)} ${r2(y)})`;
    return scaled ? `${at} scale(${r2(this.geo.k)})` : at;
  }

  /** `content` fixed to a point of the mark's 56-unit tile; it follows the mark on a relayout. */
  pin(tx, ty, content, scaled = false) {
    return `<g class="${this.p}pin" data-tx="${tx}" data-ty="${ty}" data-sc="${
      scaled ? 1 : 0
    }" transform="${this.pinAt(tx, ty, scaled)}">${content}</g>`;
  }

  /** The flying copy of the worn item, pinned to the mark in tile space (animate it with fly). */
  item(design, options) {
    return this.pin(0, 0, itemSVG(this.p, design, options), true);
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
