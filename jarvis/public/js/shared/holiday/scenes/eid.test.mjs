// The Eid scene: the shared contract, the moon and lanterns staying under the clearance line
// and out of the hero's column, the low lanterns fitting a short stage as one floor group,
// and photosensitivity (no flash; the stars twinkle once each, only the lantern sway repeats).
import { test } from "node:test";
import assert from "node:assert/strict";
import * as scene from "./eid.mjs";
import {
  FLOOR_ROW,
  LANTERN,
  LOW,
  MOON,
  lanternSpot,
  moonY,
} from "./eid-art.mjs";
import {
  CLEAR,
  GAP,
  HERO_TOP_UNITS,
  MARK_TOP,
  sceneHeroTop,
} from "../scene-geometry.mjs";
import {
  boxes,
  contractSuite,
  floorTransformOf,
  loops,
  run,
} from "./day-scene-fakes.test-support.mjs";

contractSuite("eid", scene, { launch: 2500 });

test("the moon stops under the clearance line, wherever the mark sits", () => {
  for (const shift of [-80, -39, 0, 60, 300]) {
    const y = moonY({ shift });
    assert.ok(y - MOON.r >= CLEAR - 1e-6, `shift ${shift}`);
    if (shift >= 0) assert.equal(y, MOON.y + shift, "it follows a lower mark");
  }
});

test("at the lowest mark the page allows, the moon and every lantern are already under the clearance line", () => {
  const shift = HERO_TOP_UNITS.eid - MARK_TOP;
  assert.ok(moonY({ shift }) - MOON.r >= CLEAR - 1e-6);
  for (let i = 0; i < 5; i++) {
    const [, y] = lanternSpot(i, { shift, ground: 380 });
    assert.ok(y - LANTERN.string >= CLEAR - 1e-6, `lantern ${i}`);
  }
  const plain = CLEAR + GAP; // so the hero is asked for nothing more than that
  assert.ok(HERO_TOP_UNITS.eid >= plain && HERO_TOP_UNITS.eid - plain < 0.01);
});

test("the moon is beside the hero, not over it", () => {
  const heroHalf = 96 / 1.25 / 2; // the biggest mark, in units
  assert.ok(
    MOON.x - MOON.glow > 400 + heroHalf,
    "even its glow clears the hero"
  );
});

test("the moon rises from below its place to it", () => {
  const r = run(scene, { boxes: boxes({ w: 1000, markY: 120 }) });
  const a = r.host.svg.animations.find((x) =>
    x.node.classes.includes("t-moon")
  );
  const y = (f) => Number(f.transform.match(/, ([\d.-]+)px/)[1]);
  assert.equal(y(a.frames.at(-1)), moonY({ shift: 120 / 1.25 - 84 }));
  assert.equal(y(a.frames[0]) - y(a.frames.at(-1)), MOON.rise);
  r.api.settle("instant");
  r.end();
});

test("an air lantern hangs under the clearance line; the low pair hang from the ground", () => {
  for (const shift of [-80, 0, 60]) {
    for (const i of [1, 2, 4]) {
      const [, y] = lanternSpot(i, { shift, ground: 380 });
      assert.ok(
        y - LANTERN.string >= CLEAR - 1e-6,
        `lantern ${i} shift ${shift}`
      );
    }
  }
  assert.deepEqual(lanternSpot(0, { shift: 0, ground: 380 }), [110, 245]);
  assert.deepEqual(lanternSpot(3, { shift: 0, ground: 380 }), [700, 302]);
  assert.deepEqual(LOW, [0, 3]);
});

test("no air lantern is over the hero's column", () => {
  for (const i of [1, 2, 4]) {
    const [x] = lanternSpot(i, { shift: 0, ground: 380 });
    assert.ok(Math.abs(x - 400) > 36 + LANTERN.glowR, `lantern ${i}`);
  }
});

test("five lanterns rise from below the stage and sway for ever, slowly", () => {
  const r = run(scene);
  assert.equal(r.host.svg.querySelectorAll(".t-lan").length, 5);
  const rises = r.host.svg.animations.filter((a) =>
    a.node.classes.includes("t-lan")
  );
  assert.equal(rises.length, 5);
  rises.forEach((a) =>
    assert.ok(
      Number(a.frames[0].transform.match(/, ([\d.-]+)px/)[1]) > 640 / 1.25
    )
  );
  r.api.settle("instant");
  r.end();
});

test("the low lanterns start further down on a shrunk floor row, so no string stubs up from the bottom edge", () => {
  const page = boxes({ h: 480, cardsH: 100, bottomGap: -120 });
  const r = run(scene, { boxes: page });
  const fit = floorTransformOf(r);
  assert.ok(fit.scale < 1, `scale ${fit.scale}`);
  const hu = page.host.h / 1.25;
  const startY = (n) =>
    Number(
      r.host.svg.animations
        .find(
          (a) => a.node.classes.includes(`t-lan${n}`) && a.frames[0].transform
        )
        .frames[0].transform.match(/, ([\d.-]+)px/)[1]
    );
  for (const n of [0, 3])
    assert.ok(
      Math.abs(startY(n) - (hu + 90 + 60 / fit.scale)) < 0.6,
      `low lantern ${n}: ${startY(n)}`
    );
  for (const n of [1, 2, 4]) assert.equal(startY(n), hu + 90);
  r.api.settle("instant");
  r.end();
});

test("the low pair is one floor group; a short stage fits it, a tall one leaves it", () => {
  const tall = run(scene, { boxes: boxes({ h: 700 }) });
  assert.equal(floorTransformOf(tall).scale, 1);
  const group = tall.host.svg.querySelector(".t-floor");
  assert.deepEqual(
    group
      .querySelectorAll(".t-lan")
      .map((n) => n.classes.find((c) => /lan\d/.test(c))),
    ["t-lan0", "t-lan3"]
  );
  tall.api.settle("instant");
  tall.end();
  const short = run(scene, {
    boxes: boxes({ h: 480, cardsH: 100, bottomGap: -120 }),
  });
  const fit = floorTransformOf(short);
  assert.ok(fit.scale < 1 && fit.scale >= 0.55, `scale ${fit.scale}`);
  short.api.settle("instant");
  short.end();
});

test("a replay has no floor row, so no low lanterns, and still lowers the crescent", () => {
  const r = run(scene, { replay: true });
  assert.equal(r.host.svg.querySelectorAll(".t-lan").length, 3);
  r.tick(2501);
  const a = r.host.svg.animations.find((x) =>
    x.node.classes.includes("t-drop")
  );
  assert.equal(a.options.duration, 1200);
  r.api.settle("instant");
  r.end();
});

test("the floor row's extent covers the lanterns' string top and glow bottom", () => {
  assert.equal(FLOOR_ROW.up, 135 + LANTERN.string);
  assert.equal(FLOOR_ROW.rise, 78 - LANTERN.drop);
});

test("the page keeps the mark below the plain clearance: the art needs no more than the table", () => {
  assert.ok(sceneHeroTop("eid", 1) >= CLEAR + GAP - 1e-9);
});

// ---- photosensitivity --------------------------------------------------------------------
// No flash is drawn. The 16 stars each grow and settle once; the lanterns sway in loops of
// 2.6 s or longer, far under 3 changes a second.

test("the scene has no flash; stars twinkle once, only the sways repeat, slowly", () => {
  const r = run(scene);
  assert.ok(!/flash|strobe/i.test(r.markup()));
  const stars = r.host.svg.animations.filter((a) =>
    a.node.classes.includes("t-sp")
  );
  assert.equal(stars.length, 16);
  stars.forEach((a) => assert.notEqual(a.options.iterations, Infinity));
  stars.forEach((a) =>
    assert.ok(a.options.duration >= 900, `star ${a.options.duration} ms`)
  );
  const repeating = loops(r);
  assert.equal(repeating.length, 5);
  for (const [cls, ms] of repeating) assert.ok(ms >= 2600, `${cls} ${ms}`);
  r.api.settle("instant");
  r.end();
});
