// The page side of a day scene, as methods on the scene: where things stand on the page now
// (measure), the floor row and the props that hold their place on it, and the pins that follow
// the mark. Scene (scene-runtime.mjs) extends this and adds the lifecycle and the beats; the
// hooks (extra) and the contract are documented there. The stage is 800 units wide with x
// centred on the mark's column (400) and y down from the top of the art layer.
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
import { phoneFloorMinScale, phoneScale } from "../scene-phone.mjs";
import { BELOW_HEADING_PX, heroKeepOut, squeezeOf } from "./scene-place.mjs";
import { SceneMotion } from "./scene-motion.mjs";
import { itemSVG } from "./scene-art.mjs";

// The prefix of every id and class the scene draws: the page's idPrefix without anything odd,
// and starting with a letter (a class or id may not start with a digit in a CSS selector).
const prefixOf = (id) => {
  const clean = String(id || "jvs").replace(/[^\w-]/g, "");
  return `${/^[A-Za-z_]/.test(clean) ? clean : `j${clean}`}-`;
};

/**
 * The card band the floor row stands in the bottom of. A page with no starter cards (a short
 * phone) gives none, or an empty one: the band is then what is left between the hero's bottom
 * and the layer's bottom.
 */
const cardBand = (b) => {
  if (b.cards && b.cards.h > 0) return b.cards;
  const top = b.hero
    ? b.hero.y + b.hero.h
    : b.heading.y + b.heading.h + BELOW_HEADING_PX;
  return { y: top, h: b.host.h - top };
};

export class SceneLayout extends SceneMotion {
  constructor(host, opts, cfg) {
    super();
    this.opts = opts;
    this.cfg = cfg;
    this.stage = new Stage(host, opts.onError);
    this.p = prefixOf(opts.idPrefix);
    this.geo = null;
    this.floorBottom = 0; // where the floor row was drawn; relayout moves it from there
  }

  // The hook measure() calls; scene-runtime.mjs documents it with the others.
  extra() {
    return {};
  }

  // Where things stand now, in stage units (x centred on the mark's column). geo has: the
  // layer W x H px; s px per unit (the phone: width / 540, see scene-phone.mjs); hu the layer's
  // height in units; cxPx the mark's centre in px; k units per tile unit of the mark; mt the
  // mark's top; ground the floor row's baseline; floor its fit (floorFit; null without a row);
  // skyPx the sky wash's height; from..to the stage's x extent (130..670 on a phone); squeeze
  // how much narrower than 800 units that is (1 on desktop; scene-place.mjs squeezeX); keep the
  // hero's box (heroKeepOut); shift how far the mark sits below where the art was drawn for
  // (MARK_TOP), for props that follow it; airTop..airBottom the room for sky decorations above
  // the hero.
  measure(boxes) {
    const b = boxes || this.opts.getBoxes();
    const phone = this.opts.layout === "phone";
    const [W, H] = [b.host.w, b.host.h];
    const s = phone ? phoneScale(W) : Math.min(W / 800, 1.25);
    const cxPx = b.mark.x + b.mark.w / 2;
    const mt = b.mark.y / s; // the mark's top
    const band = cardBand(b);
    const ground = groundOf(band.y + band.h, s);
    const skyEnd = Math.min(b.heading.y - 4, b.mark.y + 120 * s);
    const [from, to] = [400 - cxPx / s, 400 + (W - cxPx) / s];
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
            minScale: phone ? phoneFloorMinScale(s) : undefined,
          })
        : null,
      skyPx: Math.max(40, skyEnd / 0.52),
      from,
      to,
      squeeze: squeezeOf(from, to),
      keep: heroKeepOut(b, s),
      shift: mt - MARK_TOP, // how far the mark sits below MARK_TOP, for props that follow it
      airTop: CLEAR, // the sky decorations live between here ...
      airBottom: mt - GAP, // ... and the top of the hero
    };
    return { ...geo, ...this.extra(b, geo) };
  }

  // The floor row follows the stage height and the ground, and the pins follow the mark. A
  // row whose middle group no longer has room (cfg.foldMid) gives it up.
  refit() {
    const { geo, p, stage } = this;
    if (this.cfg.foldMid && geo.floor && geo.floor.tight) this.foldMid();
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
  heldShift(x) {
    // The row's transform is drawn with its scale rounded to 0.01: hold with that one.
    const scale = this.geo.floor ? r2(this.geo.floor.scale) : 1;
    return r2((x - 400) * (1 / scale - 1));
  }

  edgeAt(x) {
    return `translate(${this.heldShift(x)} 0)`;
  }

  /** `content` drawn at the art's own floor y: stood on the ground at the row's scale. */
  onGround(content) {
    const { base, g } = this.cfg.row;
    return `<g transform="translate(400 ${r2(
      this.geo.ground
    )}) scale(${g}) translate(-400 ${-base})">${content}</g>`;
  }

  /**
   * Takes the row's middle group out: what is drawn by onGround and is not held by edge, so the
   * props at the stage's ends stay. The start leaves it out when the hero has no room for it
   * even at the row's smallest (geo.floor.tight); this is the same call made later, when a
   * relayout (the font swapping in, the cards arriving) leaves it none. Once out it stays out:
   * the art is drawn once, and a group that blinks back in over the text is worse than a gap.
   */
  foldMid() {
    const row = this.stage.q(`.${this.p}floor`);
    const ground = row && row.children[0];
    if (!ground) return;
    const held = `${this.p}edge`;
    [...ground.children]
      .filter(
        (el) => !(el.getAttribute("class") || "").split(/\s+/).includes(held)
      )
      .forEach((el) => el.remove());
  }

  /** Where a point of the drawn floor row stands now. */
  fitted(point) {
    return fitPoint({ ...this.geo.floor, bottom: this.floorBottom }, point);
  }

  /** Where a point of a prop held at x (see edge) stands now. */
  fittedHeld(x, [px, py]) {
    return this.fitted([px + this.heldShift(x), py]);
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
}
