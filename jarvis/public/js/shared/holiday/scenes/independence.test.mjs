// The Independence Day scene (it serves Republic Day too): the contract every scene keeps
// (scene-runtime.mjs), plus what is particular to it. No browser: a tiny stand-in for
// the DOM records the markup and the animations the scene asks for. The one bright event is
// the burst of sparkles where the rosette lands; the photosensitivity checks read it from
// the scene's own data.
import { afterEach, mock, test } from "node:test";
import assert from "node:assert/strict";
import * as scene from "./independence.mjs";
import { END_MS } from "./independence.mjs";
import { KITE, kiteSpots } from "./independence-art.mjs";
import { SPARKLE } from "./scene-art.mjs";

// The one bright event: the burst of sparkles where the item lands, on the deliver beat.
const LAND = 3500;
const FLASHES = [{ cls: "sp", at: LAND, ms: SPARKLE.ms, r: SPARKLE.r }];
import { GAP, sceneHeroTop } from "../scene-geometry.mjs";
import { heroKeepOut } from "./scene-place.mjs";
import { wornMarkup } from "../worn.mjs";

import {
  contractSuite,
  run,
  stageBoxes as boxes,
} from "./day-scene-fakes.test-support.mjs";

contractSuite("independence", scene, { launch: 2500 });

// The scene most recently started, for the helpers below.
let current = null;
function start(over = {}) {
  current = run(scene, { boxes: boxes(), ...over });
  return {
    scene: current.api,
    beats: current.beats,
    svg: current.host.svg,
    names: current.names,
    move: (b) => (current.state.current = b),
  };
}
const anims = () => current.host.svg.animations;
afterEach(() => mock.timers.reset());
// In small steps: timers a callback sets are only picked up by the next tick.
const tick = (ms) => {
  for (let at = 0; at < ms; at += 10) current.tick(10);
};
const scaleOf = (el) => Number(/scale\(([\d.]+)\)/.exec(el.attrs.transform)[1]);

test("a replay has no floor props and never swaps the greeting", async () => {
  const { scene, names, svg } = start({ replay: true });
  tick(END_MS + 700);
  await scene.done;
  assert.ok(!names().includes("swap"));
  assert.ok(names().includes("deliver"));
  assert.equal(svg.querySelectorAll(".t-floor").length, 0);
  assert.equal(svg.querySelectorAll(".t-kite").length, 4);
  assert.ok(svg.querySelector(".t-drop"), "the rosette still spins in");
});

test("a first showing has four kites and thirty paper bits; there are no floor props", () => {
  const { svg } = start();
  assert.equal(svg.querySelectorAll(".t-kite").length, 4);
  assert.equal(svg.querySelectorAll(".t-tb").length, 30);
  assert.equal(svg.querySelectorAll(".t-floor").length, 0);
  assert.equal(svg.querySelectorAll(".t-nat").length, 0);
});

test("a relayout re-reads the page without redrawing or throwing", () => {
  const ctx = start();
  const html = ctx.svg.markup;
  ctx.scene.relayout(boxes({ h: 420, cardsBottom: 700 }));
  assert.equal(ctx.svg.markup, html);
});

// ---- ids and classes --------------------------------------------------------------------------

// ---- photosensitivity -------------------------------------------------------------------------

// Every sparkle's farthest point from where the burst starts, read from the animations the
// scene gave them (translate in px of the last frame), plus half a star at full size.
const STAR_HALF = 9.5;
function drawnBurstRadius() {
  const sparks = anims().filter((a) => a.node.classes.includes("t-sp"));
  assert.equal(sparks.length, SPARKLE.n);
  const reach = sparks.map((a) => {
    const [x, y] = /translate\(([-\d.]+)px, ([-\d.]+)px\)/
      .exec(a.frames.at(-1).transform)
      .slice(1)
      .map(Number);
    return Math.hypot(x, y);
  });
  return Math.max(...reach) + STAR_HALF;
}

test("one burst of sparkles, inside its stated radius, and nothing else flashes", () => {
  start();
  tick(LAND - 10); // in steps of 10 ms
  assert.equal(current.count("deliver"), 0);
  tick(10);
  assert.equal(current.count("deliver"), 1, "the sparkles go off as it lands");
  assert.equal(FLASHES.length, 1);
  assert.ok(drawnBurstRadius() <= FLASHES[0].r + 1e-9, `${drawnBurstRadius()}`);
  const fast = anims().filter((a) => {
    const o = a.frames.map((f) => f.opacity).filter((v) => v !== undefined);
    return o.some((v, i) => i && v < o[i - 1]) && a.options.duration <= 1000;
  });
  const names = new Set(fast.flatMap((a) => a.node.classes));
  assert.deepEqual(
    [...names].sort(),
    ["t-drop", "t-sp"],
    "the sparkles and the rosette's own landing"
  );
});

test("at most 3 flashes in any second, none over 25% of the layer at 1024, 1440, 1920", () => {
  for (const f of FLASHES) {
    const inWindow = FLASHES.filter((g) => g.at >= f.at && g.at < f.at + 1000);
    assert.ok(inWindow.length <= 3);
    assert.ok(f.at >= 0 && f.at + f.ms <= END_MS);
  }
  for (const w of [1024, 1440, 1920]) {
    const s = Math.min(w / 800, 1.25);
    for (const h of [400, 600, 900]) {
      for (const f of FLASHES) {
        assert.ok(
          (Math.PI * (f.r * s) ** 2) / (w * h) <= 0.25,
          `${f.cls} ${w}x${h}`
        );
      }
    }
  }
});

test("the kites rise and sway slowly; paper bits are small and drift for seconds", () => {
  start();
  for (const a of anims().filter((x) => x.node.classes.includes("t-kite"))) {
    assert.ok(a.options.duration >= 2400);
  }
  const bits = anims().filter((x) => x.node.classes.includes("t-tb"));
  assert.equal(bits.length, 30);
  for (const b of bits) assert.ok(b.options.duration >= 3800);
});

// ---- the rosette ------------------------------------------------------------------------------

test("the flying rosette is the worn rosette, without the contact shadow", () => {
  const ctx = start();
  const copy = wornMarkup("independence", {
    idPrefix: "t-it",
    motion: false,
    noShadow: true,
  });
  assert.ok(ctx.svg.markup.includes(copy));
  assert.doesNotMatch(copy, /clip-path/);
  for (const colour of ["#ff9933", "#138808"]) assert.ok(copy.includes(colour));
});

test("the rosette spins in from the left and ends on the mark, pinned and following it", () => {
  const ctx = start({ boxes: boxes({ markY: 220 }) });
  tick(2510); // the rosette leaves at 2500 ms
  const anim = anims()
    .filter((a) => a.node.classes.includes("t-drop"))
    .at(-1);
  assert.equal(
    2500 + anim.options.duration,
    LAND,
    "it lands as the item is handed over"
  );
  assert.match(
    anim.frames[0].transform,
    /translate\(-240px, 110px\) rotate\(-540deg\)/
  );
  assert.match(
    anim.frames[2].transform,
    /translate\(0px, 0px\) rotate\(0deg\)/
  );
  const pin = ctx.svg.querySelectorAll(".t-pin")[0];
  const before = pin.attrs.transform;
  ctx.scene.relayout(boxes({ markY: 330 }));
  assert.notEqual(pin.attrs.transform, before);
  tick(3500 - 780);
  assert.equal(ctx.svg.querySelectorAll(".t-sp").length, SPARKLE.n);
});

test("the scene carries no occasion words: one design serves both days", () => {
  const { svg } = start();
  assert.doesNotMatch(svg.markup, /independence|republic|<text/i);
});

// ---- the kites stay clear of the top, the floor and the hero -----------------------------------

function layout({
  w = 1200,
  h = 760,
  markY = 220,
  heading = 280,
  cardsBottom,
}) {
  const b = boxes({ w, h, markY, cardsBottom });
  b.heading.w = heading;
  const s = Math.min(w / 800, 1.25);
  const cx = b.mark.x + b.mark.w / 2;
  const geo = {
    from: 400 - cx / s,
    to: 400 + (w - cx) / s,
    mt: b.mark.y / s,
    ground: (b.cards.y + b.cards.h) / s - 2,
    hu: h / s,
    keep: heroKeepOut(b, s),
  };
  return { geo, kites: kiteSpots(geo), s };
}

test("every kite is whole: under the top edge, above the floor, however the page is laid out", () => {
  for (const w of [768, 1000, 1440, 1920]) {
    for (const markY of [0, 30, 120, 300]) {
      for (const h of [380, 560, 760]) {
        const { geo, kites } = layout({ w, h, markY });
        for (const k of kites) {
          assert.ok(
            k.y - KITE.up >= GAP - 1e-9,
            `top: ${w}/${markY}/${h} kite ${k.i}`
          );
          assert.ok(
            k.y + KITE.down <= geo.ground - GAP + 1e-9 ||
              k.y - KITE.up <= GAP + 1e-9,
            `floor: ${w}/${markY}/${h} kite ${k.i}`
          );
        }
      }
    }
  }
});

test("the highest kite never needs the page to move the hero lower than the page already does", () => {
  for (const s of [0.75, 1, 1.25]) {
    const markTop = sceneHeroTop("independence", s) / s;
    const { kites } = layout({ w: 800 * s, h: 760, markY: markTop * s });
    const high = kites.find((k) => k.i === 2);
    assert.ok(
      Math.abs(high.y - (markTop + 2)) < 1e-9,
      `s ${s}: the kite keeps its place`
    );
  }
});

test("no kite touches the hero (mark, heading, sub-line)", () => {
  for (const w of [768, 1000, 1200, 1600, 1920]) {
    for (const heading of [180, 280, 520, 680]) {
      for (const markY of [40, 160, 280]) {
        const { geo, kites } = layout({ w, heading, markY });
        for (const k of kites) {
          const hit =
            k.x + KITE.hw > geo.keep.l &&
            k.x - KITE.hw < geo.keep.r &&
            k.y + KITE.down > geo.keep.t &&
            k.y - KITE.up < geo.keep.b;
          assert.ok(!hit, `kite ${k.i} at ${w}/${heading}/${markY}`);
        }
      }
    }
  }
});

test("a long heading moves the inner kites out; a normal one keeps all four", () => {
  const normal = layout({ w: 1000, heading: 280 });
  assert.equal(normal.kites.length, 4);
  const long = layout({ w: 1000, heading: 680 });
  const x = (r, i) => r.kites.find((k) => k.i === i)?.x;
  assert.ok(x(long, 1) < x(normal, 1));
  assert.ok(x(long, 2) > x(normal, 2));
});

test("on a wide stage the outer kites keep their distance from the edge", () => {
  const { geo, kites } = layout({ w: 1920 });
  assert.equal(kites.find((k) => k.i === 0).x, geo.from + 120);
  assert.equal(kites.find((k) => k.i === 3).x, geo.to - (800 - 690));
});

test("the outer kites fly lower on a taller stage", () => {
  const tall = layout({ h: 900, markY: 220 });
  const short = layout({ h: 600, markY: 220, cardsBottom: 584 });
  const y = (r, i) => r.kites.find((k) => k.i === i).y;
  assert.ok(y(tall, 0) > y(short, 0));
  assert.equal(y(tall, 1) - y(short, 1), 0, "the inner ones stay by the mark");
});

test("the white paper bits are dimmed in the dark theme only", () => {
  const r = run(scene);
  const css = /<style>([\s\S]*?)<\/style>/.exec(r.markup())[1];
  assert.match(
    css,
    /\[data-theme="dark"\] \.t-tbw\{fill-opacity:\.55;stroke-opacity:\.55\}/
  );
  assert.doesNotMatch(css.replace(/\[data-theme="dark"\][^}]*\}/g, ""), /tbw/);
  const white = r.host.svg.querySelectorAll(".t-tbw");
  assert.equal(white.length, 10);
  assert.ok(white.every((n) => n.getAttribute("fill") === "#ffffff"));
  r.api.settle("instant");
  r.end();
});
