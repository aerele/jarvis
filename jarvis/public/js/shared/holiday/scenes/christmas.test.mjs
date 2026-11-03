// The Christmas scene: the shared contract, the sleigh keeping clear of the hero and the
// top of the stage, the floor row fitting a short stage, and photosensitivity (the tree's
// fairy lights are the only thing that blinks).
import { test } from "node:test";
import assert from "node:assert/strict";
import * as scene from "./christmas.mjs";
import {
  FLOOR_ROW,
  LIGHTS,
  SLEIGH,
  SLEIGH_MIN_ROOM,
  defsSVG,
  floorSVG,
  sleighAt,
  sleighPlan,
} from "./christmas-art.mjs";
import {
  CLEAR,
  GAP,
  HERO_TOP_UNITS,
  floorFit,
  sceneHeroTop,
  FLOOR_ROW as DIWALI_ROW,
} from "../scene-geometry.mjs";
import {
  boxes,
  contractSuite,
  edgeDrift,
  floorTransformOf,
  run,
} from "./day-scene-fakes.test-support.mjs";

contractSuite("christmas", scene, { launch: 1950 });

// The sleigh's bounding box at u, in stage units, after scale, tilt and move.
function sleighBox(plan, u) {
  const [x, y, ang] = sleighAt(plan, u);
  const a = (ang * Math.PI) / 180;
  const pts = [SLEIGH.left, SLEIGH.right].flatMap((px) =>
    [SLEIGH.top, SLEIGH.bottom].map((py) => {
      const [sx, sy] = [px * plan.k, py * plan.k];
      return [
        x + sx * Math.cos(a) - sy * Math.sin(a),
        y + sx * Math.sin(a) + sy * Math.cos(a),
      ];
    })
  );
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  return {
    l: Math.min(...xs),
    r: Math.max(...xs),
    t: Math.min(...ys),
    b: Math.max(...ys),
  };
}

const room = (oy) => ({
  airTop: CLEAR,
  airBottom: oy - GAP,
  from: -100,
  to: 900,
});

test("the sleigh never comes closer than the gap to the hero, wherever it flies", () => {
  for (const oy of [100, 120, 150, 200]) {
    const plan = sleighPlan(room(oy));
    const [heroL, heroR] = [400 - 36, 400 + 36];
    for (let i = 0; i <= 400; i++) {
      const box = sleighBox(plan, i / 400);
      if (box.r < heroL - 8 || box.l > heroR + 8) continue;
      assert.ok(box.b <= oy - GAP + 1e-6, `oy ${oy} u ${i / 400}: ${box.b}`);
    }
  }
});

test("with room the sleigh stays under the clearance line, over the whole flight", () => {
  for (const oy of [110, 130, 160]) {
    const plan = sleighPlan(room(oy));
    for (let i = 0; i <= 100; i++) {
      assert.ok(sleighBox(plan, i / 100).t >= CLEAR - 1e-6, `oy ${oy}`);
    }
  }
});

test("a small room shrinks the sleigh instead of letting it touch the hero", () => {
  const big = sleighPlan(room(160));
  const small = sleighPlan(room(CLEAR + SLEIGH_MIN_ROOM + GAP));
  assert.equal(big.k, SLEIGH.maxK);
  assert.ok(small.k < big.k && small.k >= SLEIGH.minK);
  assert.ok(
    Math.abs(small.k - SLEIGH.minK) < 1e-9,
    "the smallest room gives the smallest size"
  );
});

test("the sleigh is level over the mark and crosses the whole stage", () => {
  const plan = sleighPlan({
    airTop: CLEAR,
    airBottom: 100,
    from: -300,
    to: 1100,
  });
  const [x, y, ang] = sleighAt(plan, 0.5);
  assert.equal(x, 400);
  assert.equal(y, plan.apexY);
  assert.ok(Math.abs(ang) < 1e-9);
  assert.ok(sleighAt(plan, 0)[0] < -300 && sleighAt(plan, 1)[0] > 1100);
});

test("the page keeps the mark low enough for the sleigh: the art needs no more than the table", () => {
  for (const s of [1, 1.25])
    assert.ok(
      sceneHeroTop("christmas", s) >= (CLEAR + SLEIGH_MIN_ROOM + GAP) * s - 1e-9
    );
});

test("at the lowest mark the page allows, the sleigh fits at its smallest size, and nothing asks the hero to sit lower", () => {
  const mt = HERO_TOP_UNITS.christmas;
  const plan = sleighPlan(room(mt));
  assert.ok(Math.abs(plan.k - SLEIGH.minK) < 0.01, `size ${plan.k}`);
  for (let i = 0; i <= 200; i++) {
    const box = sleighBox(plan, i / 200);
    assert.ok(box.t >= CLEAR - 0.05, `u ${i / 200}: top ${box.t}`);
    if (box.r >= 400 - 36 - 8 && box.l <= 400 + 36 + 8)
      assert.ok(box.b <= mt - GAP + 0.05, `u ${i / 200}: bottom ${box.b}`);
  }
});

test("floorFit gives the Diwali answer for the Diwali row, and uses this row's own height", () => {
  const args = { ground: 560, hostH: 500, s: 1 };
  assert.deepEqual(floorFit({ ...args, row: DIWALI_ROW }), floorFit(args));
  const over = 560 - (500 - 12); // how far the row's bottom is past the stage's gap line
  assert.equal(
    floorFit({ ...args, row: FLOOR_ROW }).scale,
    1 - over / FLOOR_ROW.up
  );
});

test("the tree and gifts stay at the stage's ends when the row shrinks, never drifting in over the heading", () => {
  const short = run(scene, {
    boxes: boxes({ w: 1100, h: 480, cardsH: 120, bottomGap: -40 }),
  });
  assert.ok(floorTransformOf(short).scale < 1, "the row shrinks");
  const drift = edgeDrift(short, FLOOR_ROW.g);
  assert.equal(drift.length, 2, "tree and gifts are held");
  for (const [x, off] of drift)
    assert.ok(off < 0.01, `prop at ${x} drifts ${off}`);
});

test("a short stage fits the tree and gifts as one group; a tall one leaves them alone", () => {
  const tall = run(scene, { boxes: boxes({ w: 1100, h: 700 }) });
  assert.equal(floorTransformOf(tall).scale, 1);
  tall.api.settle("instant");
  tall.end();
  const short = run(scene, {
    boxes: boxes({ w: 1100, h: 480, cardsH: 120, bottomGap: -40 }),
  });
  const fit = floorTransformOf(short);
  assert.ok(fit.scale < 1 && fit.scale >= 0.55, `scale ${fit.scale}`);
  const row = short.host.svg.querySelector(".t-floor");
  assert.ok(
    row.children.length === 1,
    "one group holds the tree and the gifts"
  );
  assert.ok(
    short.markup().includes("t-tstar") && short.markup().includes("t-gift")
  );
  short.api.settle("instant");
  short.end();
});

test("a relayout refits the floor row from where it was drawn", () => {
  const r = run(scene, { boxes: boxes({ h: 700 }) });
  const before = floorTransformOf(r);
  r.state.current = boxes({ h: 470, cardsH: 120, bottomGap: -40 });
  r.api.relayout(r.state.current);
  const after = floorTransformOf(r);
  assert.ok(after.scale < before.scale);
  const drawn = r.host.svg.querySelector(".t-floor").getAttribute("transform");
  const bottom = Number(drawn.match(/translate\(-400 (-?[\d.]+)\)/)[1]);
  assert.ok(bottom < 0, "starts from the drawn bottom");
  r.api.settle("instant");
  r.end();
});

test("the snow spans the whole stage, wider than the 800 units", () => {
  const r = run(scene, { boxes: boxes({ w: 1800, h: 700 }) });
  const xs = r.host.svg
    .querySelectorAll(".t-flake")
    .map((n) =>
      Number(
        n.parent.getAttribute("transform").match(/translate\(([\d.-]+)/)[1]
      )
    );
  assert.equal(xs.length, 34);
  assert.ok(
    Math.min(...xs) < 100 && Math.max(...xs) > 700,
    "uses the extra width"
  );
  r.api.settle("instant");
  r.end();
});

test("the hat is let go from where the sleigh is, above the mark", () => {
  const r = run(scene);
  r.tick(1951);
  const anim = r.host.svg.animations.find((a) =>
    a.node.classes.includes("t-drop")
  );
  const first = anim.frames[0].transform.match(
    /translate\(([\d.-]+)px, ([\d.-]+)px\)/
  );
  assert.ok(Number(first[2]) < 0, "starts above the mark");
  assert.equal(
    anim.frames.at(-1).opacity,
    0,
    "it hands over to the mark's own item"
  );
  assert.equal(anim.options.duration, 950);
  r.api.settle("instant");
  r.end();
});

// ---- photosensitivity --------------------------------------------------------------------
// The scene has no flash: nothing lights the layer. What blinks is the tree's 12 small
// fairy lights, each a gold dot fading between 35% and 100%. Checked statically: the
// slowest-changing light changes at most once per 0.9 s (about 1.1 Hz, three at most), and
// all the lights together cover a tiny part of the layer.

test("a fairy light twinkles at most about once a second, well under 3 per second", () => {
  assert.ok(LIGHTS.periodMs >= 900);
  assert.ok(1000 / LIGHTS.periodMs <= 3);
  // And the twinkles actually drawn: every light's endless fade takes at least that long.
  const r = run(scene);
  const twinkles = r.host.svg.animations.filter(
    (a) =>
      a.options.iterations === Infinity &&
      a.node.parent.classes.includes("t-tl")
  );
  assert.equal(twinkles.length, LIGHTS.n);
  for (const a of twinkles)
    assert.ok(a.options.duration >= 900, `${a.options.duration} ms`);
  r.api.settle("instant");
  r.end();
});

test("the fairy lights together cover far less than 25% of the layer", () => {
  const glow = `<circle cx="0" cy="0" r="${LIGHTS.glowR}"`;
  const r = run(scene);
  const drawn = [
    ...r.markup().matchAll(/class="t-tl"[^>]*><circle[^>]* r="([\d.]+)"/g),
  ];
  assert.equal(drawn.length, LIGHTS.n);
  for (const m of drawn) assert.equal(Number(m[1]), LIGHTS.glowR, glow);
  for (const [vw, vh] of [
    [1024, 640],
    [1440, 800],
    [1920, 1080],
  ]) {
    const [w, h] = [vw - 260, vh - 120]; // the welcome area: the viewport less sidebar and bar
    const s = Math.min(w / 800, 1.25);
    const area = LIGHTS.n * Math.PI * (LIGHTS.glowR * 1.2 * s) ** 2; // 1.2: the floor row's growth
    assert.ok(area / (w * h) <= 0.25, `${vw}x${vh}`);
    assert.ok(area / (w * h) < 0.01, `${vw}x${vh} is tiny`);
  }
  r.api.settle("instant");
  r.end();
});

test("the art defs and floor draw the same ids the scene refers to", () => {
  const defs = defsSVG("z-");
  const floor = floorSVG("z-");
  for (const ref of floor.matchAll(/url\(#([^)]+)\)/g))
    assert.ok(defs.includes(`id="${ref[1]}"`), ref[1]);
});
