// Test support for the day scenes: a tiny stand-in for the SVG DOM (just what stage.mjs
// and the scenes use), a recorder for beats and animations, and the contract tests every
// day scene must pass. No browser, no timers of the real clock: node:test mock timers.
import { mock, test } from "node:test";
import assert from "node:assert/strict";
import {
  HERO_FLOOR_GAP_PX,
  MIN_FLOOR_SCALE,
  centreUp,
  floorFit,
} from "../scene-geometry.mjs";

// ---- a tiny DOM --------------------------------------------------------------------------

const TAG = /<(\/?)([a-zA-Z][\w:-]*)([^>]*?)(\/?)>|([^<]+)/g;
const ATTR = /([\w:-]+)="([^"]*)"/g;

class Node {
  constructor(svg, tag, attrs, parent) {
    Object.assign(this, { svg, tag, attrs, parent, children: [], text: "" });
    this.dataset = {};
    for (const [k, v] of Object.entries(attrs))
      if (k.startsWith("data-")) this.dataset[k.slice(5)] = v;
  }

  get classes() {
    return (this.attrs.class || "").split(/\s+/).filter(Boolean);
  }

  get lastElementChild() {
    return this.children[this.children.length - 1] || null;
  }

  getAttribute(k) {
    return this.attrs[k] ?? null;
  }

  setAttribute(k, v) {
    this.attrs[k] = String(v);
  }

  animate(frames, options) {
    const anim = { node: this, frames, options, cancelled: false };
    anim.cancel = () => (anim.cancelled = true);
    this.svg.animations.push(anim);
    return anim;
  }

  insertAdjacentHTML(_where, html) {
    this.svg.markup += html;
    parseInto(this.svg, html, this);
  }

  matches(simple) {
    return simple.startsWith(".")
      ? this.classes.includes(simple.slice(1))
      : this.tag === simple;
  }

  descendants() {
    return this.children.flatMap((c) => [c, ...c.descendants()]);
  }

  // Inside this node only (a single class or tag; FakeSvg has the descendant form).
  querySelectorAll(selector) {
    return this.descendants().filter((n) => n.matches(selector));
  }
}

function parseInto(svg, markup, root) {
  let at = root;
  for (const m of markup.matchAll(TAG)) {
    if (m[5] !== undefined) {
      at.text += m[5].trim();
    } else if (m[1]) {
      at = at.parent && at !== root ? at.parent : at;
    } else {
      const attrs = Object.fromEntries(
        [...m[3].matchAll(ATTR)].map((a) => [a[1], a[2]])
      );
      const node = new Node(svg, m[2], attrs, at);
      at.children.push(node);
      svg.all.push(node);
      if (!m[4]) at = node;
    }
  }
}

class FakeSvg extends Node {
  constructor() {
    super(null, "svg", {}, null);
    this.svg = this;
    this.markup = "";
    this.all = [];
    this.animations = [];
    this.style = {};
    this.removed = false;
  }

  set innerHTML(html) {
    this.markup = html;
    this.children = [];
    this.all = [];
    parseInto(this, html, this);
  }

  remove() {
    this.removed = true;
  }

  find(selector) {
    const parts = selector.split(/\s+/);
    return this.all.filter((n) => {
      let want = parts.length - 1;
      if (!n.matches(parts[want])) return false;
      for (let a = n.parent; a && want > 0; a = a.parent)
        if (a.matches(parts[want - 1])) want--;
      return want === 0;
    });
  }

  querySelector(selector) {
    return this.find(selector)[0] || null;
  }

  querySelectorAll(selector) {
    return this.find(selector);
  }
}

/** A host that records the layer a scene mounts. Installs the document stand-in. */
export function fakeHost() {
  const host = { svgs: [], appendChild: (svg) => host.svgs.push(svg) };
  globalThis.document = { createElementNS: () => new FakeSvg() };
  Object.defineProperty(host, "svg", { get: () => host.svgs.at(-1) || null });
  return host;
}

// ---- running a scene ---------------------------------------------------------------------

/** The boxes the page would report, for a stage `w` x `h` px with the cards in the bottom band. */
export function boxes({
  w = 1100,
  h = 640,
  markY = 110,
  cardsH = 150,
  bottomGap = 28, // the card band's distance from the stage bottom; negative: it overflows
} = {}) {
  return {
    host: { w, h },
    mark: { x: (w - 96) / 2, y: markY, w: 96, h: 96 },
    heading: { x: w / 2 - 200, y: markY + 130, w: 400, h: 44 },
    cards: { x: w / 2 - 340, y: h - cardsH - bottomGap, w: 680, h: cardsH },
  };
}

/**
 * The page of the pongal-family tests: a 1200 x 760 stage whose mark is 72 units wide at any
 * scale, `cardsBottom` is how far down the bottom block ends (the real page leaves 16 px).
 */
export function stageBoxes({
  w = 1200,
  h = 760,
  markY = 220,
  cardsBottom = h - 16,
} = {}) {
  const s = Math.min(w / 800, 1.25);
  return {
    host: { w, h },
    mark: { x: w / 2 - 36 * s, y: markY, w: 72 * s, h: 72 * s },
    heading: { x: w / 2 - 140, y: markY + 116 * s, w: 280, h: 40 * s },
    cards: { x: 0, y: cardsBottom - 150, w, h: 150 },
  };
}

/** Play a scene on mock timers; `beats` records every onBeat call as [name, ...args]. */
export function run(mod, over = {}) {
  mock.timers.reset(); // a failed test may have left the last one enabled
  mock.timers.enable({ apis: ["setTimeout"] });
  const host = fakeHost();
  const beats = [];
  const state = { current: over.boxes || boxes() };
  const opts = {
    layout: "desktop",
    getBoxes: () => state.current,
    year: 2027,
    idPrefix: "t",
    reduced: false,
    onBeat: (...a) => beats.push(a),
    replay: false,
    ...over,
  };
  const api = mod.play(host, opts);
  return {
    api,
    host,
    beats,
    state,
    names: () => beats.map((b) => b[0]),
    count: (name) => beats.filter((b) => b[0] === name).length,
    tick: (ms) => mock.timers.tick(ms),
    // The run to its end and through the fade (two steps: the fade is scheduled at the end).
    finish: () => {
      mock.timers.tick(mod.END_MS);
      mock.timers.tick(700);
    },
    markup: () => (host.svg ? host.svg.markup : ""),
    done: () => api.done,
    end: () => mock.timers.reset(),
  };
}

const ids = (markup) =>
  [...markup.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
const refs = (markup) =>
  [...markup.matchAll(/url\(#([^)]+)\)/g)].map((m) => m[1]);
const REJECTED =
  /dove|bird|cow|countdown|garland|ornament|hanging|kathakali|mahabali|jasmine/i;

// Is `node` inside an element of class `cls`?
const inside = (node, cls) => {
  for (let a = node.parent; a; a = a.parent)
    if (a.classes.includes(cls)) return true;
  return false;
};

/** The contract every day scene meets (scene-runtime.mjs header). `ms` are the scene's own times. */
export function contractSuite(label, mod, ms) {
  const scene = (over) => run(mod, over);

  test(`${label}: play gives settle, relayout and a done promise`, () => {
    const r = scene();
    assert.equal(typeof r.api.settle, "function");
    assert.equal(typeof r.api.relayout, "function");
    assert.ok(r.api.done instanceof Promise);
    assert.ok(r.host.svg && !r.host.svg.removed, "the art is mounted");
    r.api.settle("instant");
    r.end();
  });

  test(`${label}: a full run beats peek, look, deliver, happy, swap, settle in order`, async () => {
    const r = scene();
    r.tick(mod.END_MS - 1);
    const at = (name) => r.names().indexOf(name);
    assert.equal(r.names()[0], "peek");
    assert.ok(at("look") > 0);
    assert.ok(at("deliver") > at("look"));
    assert.ok(at("happy") > at("deliver"));
    assert.ok(at("swap") > at("happy"));
    assert.equal(r.count("settle"), 0);
    r.tick(1);
    assert.deepEqual(
      r.beats.find((b) => b[0] === "settle"),
      ["settle", "end"]
    );
    assert.deepEqual(
      r.beats.find((b) => b[0] === "swap"),
      ["swap", { instant: false }]
    );
    r.tick(700);
    await r.done();
    assert.ok(r.host.svg === null || r.host.svg.removed, "the art is gone");
    assert.equal(r.count("deliver"), 1);
    r.end();
  });

  test(`${label}: early settle lands the item once, swaps, and done resolves`, async () => {
    const r = scene();
    r.tick(300);
    r.api.settle("early");
    assert.deepEqual(
      r.beats.find((b) => b[0] === "settle"),
      ["settle", "early"]
    );
    assert.equal(r.count("deliver"), 1);
    assert.equal(r.count("happy"), 1);
    assert.equal(r.count("swap"), 1);
    r.tick(400);
    await r.done();
    r.api.settle("early"); // a second settle changes nothing
    assert.equal(r.count("deliver"), 1);
    r.tick(2000);
    assert.ok(r.names().includes("peek"));
    r.end();
  });

  test(`${label}: instant settle shows the rest state with no motion`, async () => {
    const r = scene();
    r.api.settle("instant");
    assert.deepEqual(
      r.beats.find((b) => b[0] === "swap"),
      ["swap", { instant: true }]
    );
    assert.equal(r.count("deliver"), 1);
    assert.ok(r.host.svg.removed);
    await r.done();
    r.end();
  });

  test(`${label}: settle after the end of the run does nothing more`, async () => {
    const r = scene();
    r.finish();
    await r.done();
    const before = r.beats.length;
    r.api.settle("early");
    assert.equal(r.beats.length, before);
    r.end();
  });

  test(`${label}: done resolves even when the page leaves late (instant after early)`, async () => {
    const r = scene();
    r.tick(100);
    r.api.settle("early");
    r.api.settle("instant");
    await r.done();
    assert.ok(r.host.svg.removed);
    r.end();
  });

  test(`${label}: replay has no greeting swap and no floor props`, () => {
    const r = scene({ replay: true });
    r.finish();
    assert.equal(r.count("swap"), 0);
    assert.ok(!r.markup().includes("floor"), "no floor row");
    r.end();
  });

  test(`${label}: reduced motion and the phone layout only rest`, async () => {
    for (const over of [{ reduced: true }, { layout: "phone" }]) {
      const r = scene(over);
      assert.equal(r.host.svgs.length, 0, "nothing drawn");
      r.api.settle("end");
      r.api.settle("end");
      assert.deepEqual(r.names(), ["settle", "swap", "deliver", "peek"]);
      await r.done();
      r.end();
    }
  });

  test(`${label}: every id carries the idPrefix, every reference resolves`, () => {
    const a = scene({ idPrefix: "alpha" });
    a.tick(mod.END_MS - 1); // and everything added while it plays
    const markup = a.markup();
    const mine = ids(markup);
    assert.ok(mine.length > 0);
    // worn.mjs puts its own letters before the prefix (t<prefix>, hat<prefix>)
    assert.ok(
      mine.every((id) => id.includes("alpha-")),
      mine.join()
    );
    assert.equal(new Set(mine).size, mine.length, "no duplicate id");
    for (const ref of refs(markup)) assert.ok(mine.includes(ref), ref);
    a.api.settle("instant");
    a.end();
    const b = scene({ idPrefix: "beta" });
    assert.ok(ids(b.markup()).every((id) => !mine.includes(id)));
    b.api.settle("instant");
    b.end();
  });

  test(`${label}: every class and style selector is the scene's own; an odd idPrefix is cleaned`, () => {
    const r = scene({ idPrefix: "jvs1" });
    r.tick(mod.END_MS - 1); // and everything added while it plays
    const css = /<style>([\s\S]*?)<\/style>/.exec(r.markup())[1];
    const selectors = [...css.matchAll(/\.([a-zA-Z_][\w-]*)/g)].map(
      (m) => m[1]
    );
    assert.ok(
      selectors.length && selectors.every((c) => c.startsWith("jvs1-"))
    );
    // The item's copy keeps the worn art's own class names; everything else is prefixed.
    for (const n of r.host.svg.all) {
      if (inside(n, "jvs1-drop")) continue;
      assert.ok(
        n.classes.every((c) => c.startsWith("jvs1-")),
        n.classes.join()
      );
    }
    r.api.settle("instant");
    r.end();
    const odd = scene({ idPrefix: "a b!c" });
    assert.ok(ids(odd.markup()).every((id) => id.includes("abc-")));
    odd.api.settle("instant");
    odd.end();
    const none = scene({ idPrefix: "" });
    assert.ok(ids(none.markup()).every((id) => id.includes("jvs-")));
    none.api.settle("instant");
    none.end();
    // A prefix that would start with a digit gets a letter, so every selector stays valid.
    for (const [idPrefix, want] of [
      ["7up", "j7up-"],
      ["!!!", "j-"],
      ["-9", "j-9-"],
    ]) {
      const digit = scene({ idPrefix });
      const sheet = /<style>([\s\S]*?)<\/style>/.exec(digit.markup())[1];
      const selectors = [...sheet.matchAll(/\.([a-zA-Z_][\w-]*)/g)].map(
        (m) => m[1]
      );
      assert.ok(selectors.length && selectors.every((c) => c.startsWith(want)));
      assert.ok(
        ids(digit.markup()).every((id) => id.includes(want)),
        idPrefix
      );
      digit.api.settle("instant");
      digit.end();
    }
  });

  test(`${label}: no drawn class or id names a rejected design`, () => {
    const r = scene();
    r.tick(mod.END_MS - 1); // and everything added while it plays
    const names = [...r.markup().matchAll(/\s(?:class|id)="([^"]+)"/g)].map(
      (m) => m[1]
    );
    for (const n of names) assert.ok(!REJECTED.test(n), n);
    assert.ok(!/undefined|NaN/.test(r.markup()));
    assert.ok(!/dove|bird|\bcow|kathakali|mahabali|jasmine/i.test(r.markup()));
    r.api.settle("instant");
    r.end();
  });

  if (ms.launch !== undefined)
    test(`${label}: the flying item is pinned to the mark where the page has it by then`, () => {
      const r = scene();
      r.state.current = boxes({ markY: 180 }); // the font swapped in and the page moved
      r.api.relayout(r.state.current);
      r.tick(ms.launch + 1);
      const at = r.host.svg
        .querySelectorAll(".t-pin")
        .find((n) => n.dataset.sc === "1");
      const [, x, y, k] = at
        .getAttribute("transform")
        .match(/translate\(([\d.-]+) ([\d.-]+)\) scale\(([\d.-]+)\)/)
        .map(Number);
      const s = 1.25; // min(1100 / 800, 1.25)
      assert.ok(Math.abs(y - 180 / s) < 0.01, `y ${y}`);
      assert.ok(Math.abs(k - 96 / s / 56) < 0.01);
      assert.ok(Math.abs(x - (400 - 96 / s / 2)) < 0.01);
      assert.ok(
        r.host.svg.querySelector(".t-drop"),
        "the item's copy is drawn"
      );
      r.api.settle("instant");
      r.end();
    });

  if (ms.launch !== undefined)
    test(`${label}: an early settle in mid-flight takes the flying copy away, as the item lands`, () => {
      const r = scene();
      r.tick(ms.launch + 100);
      const copy = r.host.svg.querySelector(".t-drop");
      const flights = r.host.svg.animations.filter((a) => a.node === copy);
      assert.ok(flights.length > 0, "the copy is flying");
      assert.ok(flights.every((a) => !a.cancelled));
      r.api.settle("early");
      assert.ok(
        flights.every((a) => a.cancelled),
        "the copy is gone"
      );
      assert.equal(r.count("deliver"), 1, "and the item is on the mark");
      r.api.settle("instant");
      r.end();
    });

  if (ms.row)
    test(`${label}: the floor row fits a short stage, and follows the ground when the page moves`, () => {
      const r = scene({ boxes: boxes({ h: 700 }) });
      const drawn = floorTransformOf(r);
      assert.equal(drawn.scale, 1);
      // Each page the row is relayed out to: where floorFit says its drawn bottom must land.
      for (const page of [
        boxes({ h: 470, cardsH: 120, bottomGap: -40 }), // short: shrinks
        boxes({ h: 900, cardsH: 150, bottomGap: 90 }), // tall, ground moved: scale 1
        boxes({ h: 560, cardsH: 100, bottomGap: -10 }),
      ]) {
        r.state.current = page;
        r.api.relayout(page);
        const s = Math.min(page.host.w / 800, 1.25);
        const ground = (page.cards.y + page.cards.h) / s - 2;
        const want = floorFit({ row: ms.row, ground, hostH: page.host.h, s });
        const got = floorTransformOf(r);
        assert.ok(
          Math.abs(got.anchor - want.anchor) < 0.01,
          `anchor ${got.anchor} vs ${want.anchor}`
        );
        assert.ok(
          Math.abs(got.scale - want.scale) < 0.01,
          `scale ${got.scale} vs ${want.scale}`
        );
        assert.equal(
          got.bottom,
          drawn.bottom,
          "it is fitted from where it was drawn"
        );
      }
      r.api.settle("instant");
      r.end();
    });

  // The part of the row under the hero keeps HERO_FLOOR_GAP_PX below the hero's bottom (the
  // sub-line's), shrinking the row, to the minimum scale at most. A row whose tall props are
  // all at the ends (mid: null) is left alone.
  if (ms.row)
    test(`${label}: the floor row keeps ${HERO_FLOOR_GAP_PX} px below the hero's bottom`, () => {
      const r = scene({ boxes: boxes({ h: 700 }) });
      const page = boxes({ h: 700 });
      const s = Math.min(page.host.w / 800, 1.25);
      const ground = (page.cards.y + page.cards.h) / s - 2;
      const mid = centreUp(ms.row);
      const unconstrained = floorFit({ row: ms.row, ground, hostH: 700, s });
      // The hero ends 30 units into the row's centre part (or, with none, anywhere low).
      const heroBottom = (ground - (mid ?? 0) + 30) * s - HERO_FLOOR_GAP_PX;
      const crowded = { ...page, hero: { x: 0, y: 0, w: 680, h: heroBottom } };
      r.state.current = crowded;
      r.api.relayout(crowded);
      const got = floorTransformOf(r);
      if (mid === null) {
        assert.equal(got.scale, unconstrained.scale, "nothing under the hero");
      } else {
        const clearTop = (heroBottom + HERO_FLOOR_GAP_PX) / s;
        const coreTop = got.anchor - got.scale * (mid - ms.row.rise);
        assert.ok(got.scale < unconstrained.scale, `shrunk, ${got.scale}`);
        assert.ok(
          // The drawn scale is rounded to 0.01 (floorTransform).
          coreTop >= clearTop - 0.005 * (mid - ms.row.rise) - 0.01 ||
            got.scale === MIN_FLOOR_SCALE,
          `core top ${coreTop} vs ${clearTop}`
        );
      }
      // The hero's box absent: the old fit, so a page that does not say is not shrunk.
      r.state.current = page;
      r.api.relayout(page);
      assert.equal(floorTransformOf(r).scale, unconstrained.scale);
      r.api.settle("instant");
      r.end();
    });

  test(`${label}: a settled scene ignores relayout`, () => {
    const r = scene();
    r.api.settle("instant");
    assert.doesNotThrow(() => r.api.relayout(boxes({ markY: 200 })));
    r.end();
  });
}

/** The floor row's transform on the mounted layer, as {scale, lift}. */
export function floorTransformOf(r) {
  const g = r.host.svg.querySelector(".t-floor");
  if (!g) return null;
  const m = g
    .getAttribute("transform")
    .match(
      /translate\(400 ([\d.-]+)\) scale\(([\d.]+)\) translate\(-400 ([\d.-]+)\)/
    );
  return { anchor: Number(m[1]), scale: Number(m[2]), bottom: -Number(m[3]) };
}

/**
 * How far (stage units) the props held at their edge (Scene.edge) have drifted from where
 * they stand when the floor row is at its full size, as the row is fitted now. `g` is the row's
 * own scale. Every held prop is listed: [x it was drawn at, drift]; 0 means held in place.
 */
export function edgeDrift(r, g = 1) {
  const fit = floorTransformOf(r);
  return r.host.svg.querySelectorAll(".t-edge").map((el) => {
    const x = Number(el.dataset.x);
    const [, shift] = /translate\(([-\d.]+) 0\)/.exec(
      el.getAttribute("transform")
    );
    const shown = 400 + fit.scale * g * (x + Number(shift) - 400);
    return [x, Math.abs(shown - (400 + g * (x - 400)))];
  });
}

/** The animations that repeat for ever, as [class list, duration ms]. */
export function loops(r) {
  return r.host.svg.animations
    .filter((a) => a.options.iterations === Infinity)
    .map((a) => [a.node.attrs.class || "", a.options.duration]);
}
