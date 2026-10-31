// The Onam scene: the contract every scene keeps (scene-runtime.mjs), plus what is
// particular to Onam. No browser: a tiny stand-in for the DOM records the markup and the
// animations the scene asks for. The one bright event is the burst of sparkles where the
// umbrella lands; the photosensitivity checks read it from the scene's own data.
import { afterEach, mock, test } from "node:test";
import assert from "node:assert/strict";
import * as scene from "./onam.mjs";
import { END_MS } from "./onam.mjs";
import {
  FLOOR_ROW,
  RAMP_MS,
  UMBRELLA,
  lampSVG,
  pookalamSVG,
  riseFor,
} from "./onam-art.mjs";
import { SPARKLE } from "./scene-art.mjs";

// The one bright event: the burst of sparkles where the item lands, on the deliver beat.
const LAND = 3400;
const FLASHES = [{ cls: "sp", at: LAND, ms: SPARKLE.ms, r: SPARKLE.r }];
import { MIN_FLOOR_SCALE, sceneHeroTop } from "../scene-geometry.mjs";
import { wornMarkup } from "../worn.mjs";

import {
  contractSuite,
  edgeDrift,
  floorTransformOf,
  run,
  stageBoxes as boxes,
} from "./day-scene-fakes.test-support.mjs";

contractSuite("onam", scene, { row: FLOOR_ROW, launch: 2100 });

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
  assert.equal(svg.querySelectorAll(".t-pring").length, 0);
  assert.ok(svg.querySelector(".t-drop"), "the umbrella still comes down");
});

test("a first showing has the floor props", () => {
  const { svg } = start();
  assert.equal(svg.querySelectorAll(".t-floor").length, 1);
  assert.equal(svg.querySelectorAll(".t-pring").length, 9);
  assert.equal(svg.querySelectorAll(".t-nlg").length, 2);
  assert.equal(svg.querySelectorAll(".t-nlf").length, 8);
});

// ---- the floor group fits a short stage ---------------------------------------------------------

test("the floor group's stated extent matches what is drawn", () => {
  const finial = /cy="(-101)" r="(2.4)"/.exec(lampSVG("t-", 0));
  assert.ok(
    Math.abs(372 + Number(finial[1]) - Number(finial[2]) - FLOOR_ROW.top) < 1e-9
  );
  const shadow = /<ellipse cx="400" cy="(356)" rx="160" ry="(38)"/.exec(
    pookalamSVG("t-")
  );
  assert.equal(Number(shadow[1]) + Number(shadow[2]), FLOOR_ROW.bottom);
  assert.ok(FLOOR_ROW.bottom <= FLOOR_ROW.base);
});

test("a hero with no room left for the pookalam even at the row's smallest leaves them out", () => {
  const page = boxes();
  const crowded = { ...page, hero: { x: 0, y: 0, w: 680, h: 720 } };
  const roomy = { ...page, hero: { x: 0, y: 0, w: 680, h: 300 } };
  const { svg: tight } = start({ boxes: crowded });
  assert.equal(tight.querySelectorAll(".t-pring").length, 0, "left out");
  assert.equal(
    tight.querySelectorAll(".t-floor").length,
    1,
    "the rest of the row stays"
  );
  const { svg: fine } = start({ boxes: roomy });
  assert.ok(fine.querySelectorAll(".t-pring").length > 0, "kept when it fits");
});

test("the lamps stay at the stage's ends when the row shrinks on a short stage, never drifting in under the sub-line", () => {
  const ctx = start();
  const short = boxes({ h: 500, cardsBottom: 700 });
  ctx.move(short);
  ctx.scene.relayout(short);
  assert.ok(floorTransformOf(current).scale < 1, "the row shrinks");
  const drift = edgeDrift(current, FLOOR_ROW.g);
  assert.equal(drift.length, 2, "both lamps are held");
  for (const [x, off] of drift)
    assert.ok(off < 0.01, `lamp at ${x} drifts ${off}`);
});

// ---- ids and classes --------------------------------------------------------------------------

// ---- photosensitivity -------------------------------------------------------------------------

// Every sparkle's farthest point from where the burst starts, read from the animations the
// scene gave them (translate in px of the last frame), plus half a star at full size.
const STAR_HALF = 9.5;
function drawnBurstRadius() {
  const sparks = anims().filter((a) => a.node.classes.includes("t-sp"));
  assert.equal(sparks.length, SPARKLE.n);
  return (
    Math.max(
      ...sparks.map((a) => {
        const [x, y] = /translate\(([-\d.]+)px, ([-\d.]+)px\)/
          .exec(a.frames.at(-1).transform)
          .slice(1)
          .map(Number);
        return Math.hypot(x, y);
      })
    ) + STAR_HALF
  );
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
  assert.deepEqual([...names].sort(), ["t-sp"], "only the sparkles");
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

test("the lamp glows come up slowly and stay lit", () => {
  assert.ok(RAMP_MS.lamp >= 500);
  start();
  const glows = anims().filter((a) => a.node.classes.includes("t-nlg"));
  assert.equal(glows.length, 2);
  for (const g of glows) {
    assert.equal(g.frames.at(-1).opacity, 1);
    assert.equal(g.options.duration, RAMP_MS.lamp);
  }
});

// ---- the umbrella -----------------------------------------------------------------------------

test("the flying umbrella is the worn umbrella alone: no kasavu, no contact shadow", () => {
  const ctx = start();
  const copy = wornMarkup("onam", {
    idPrefix: "t-it",
    motion: false,
    noShadow: true,
    crownOnly: true,
  });
  assert.ok(ctx.svg.markup.includes(copy));
  assert.doesNotMatch(copy, /kv|kasavu|clip-path/);
});

// The umbrella's frames: tile-space y of its start, read from the animation.
function startRise() {
  tick(2110); // the umbrella leaves at 2100 ms
  const anim = anims()
    .filter((a) => a.node.classes.includes("t-drop"))
    .at(-1);
  return -Number(/, ([-\d.]+)px\)/.exec(anim.frames[0].transform)[1]);
}

test("the umbrella starts at its full height when there is room, and lower when there is not", () => {
  start({ boxes: boxes({ markY: 300 }) });
  assert.equal(startRise(), UMBRELLA.rise);
  start({ boxes: boxes({ markY: 60 }) });
  assert.ok(startRise() < UMBRELLA.rise && startRise() >= UMBRELLA.minRise);
});

test("the umbrella's top never goes above the stage, down to the page's lowest hero position", () => {
  // The finial of the canopy, in tile space: (0, -7) in the canopy's own space.
  const rot = (12 * Math.PI) / 180;
  const finialY = -6 + -7 * 0.85 * Math.cos(rot);
  assert.ok(UMBRELLA.top <= finialY, "the stated top is above the drawn one");
  const k = 72 / 56;
  for (const s of [0.75, 1, 1.25]) {
    for (const extra of [0, 5, 40, 200]) {
      const mt = sceneHeroTop("onam", s) / s + extra;
      const topY = mt + (UMBRELLA.top - riseFor(mt, k)) * k;
      assert.ok(topY >= UMBRELLA.gap * k - 1e-9, `s ${s} +${extra}: ${topY}`);
    }
  }
});

test("the flying umbrella and the landing sparkles follow the mark", () => {
  const ctx = start({ boxes: boxes({ markY: 220 }) });
  const drop = ctx.svg.querySelector(".t-drop");
  assert.ok(drop);
  const pin = ctx.svg.querySelectorAll(".t-pin")[0];
  const before = pin.attrs.transform;
  assert.match(before, /scale\(1\.29\)$/);
  ctx.scene.relayout(boxes({ markY: 330 }));
  assert.notEqual(pin.attrs.transform, before);
  tick(3400 - 780);
  assert.equal(ctx.svg.querySelectorAll(".t-sp").length, SPARKLE.n);
});

test("petals fall past the bottom of the stage and sway", () => {
  start({ boxes: boxes({ h: 700 }) });
  const falls = anims().filter((a) => a.node.classes.includes("t-pet"));
  assert.equal(falls.length, 36);
  const s = 1.25;
  for (const f of falls) {
    assert.ok(f.options.duration >= 3800);
    const end = f.frames.at(-1).transform;
    assert.ok(Number(/, ([\d.]+)px\)/.exec(end)[1]) >= 700 / s + 80 - 1e-9);
  }
});
