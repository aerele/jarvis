// The Diwali art's own geometry, as the page needs it before the art is loaded: where the
// toran ends and how far above the mark the fireworks reach.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  BURSTS,
  DIYA,
  FLOOR_GAP_PX,
  FLOOR_ROW,
  MARK_TOP,
  MIN_FLOOR_SCALE,
  TORAN,
  burstRise,
  fireworksAboveMark,
  fireworksMinDy,
  fitPoint,
  floorFit,
  floorTransform,
  heroTopMin,
  toranBottom,
} from "./scene-geometry.mjs";
import { r2 } from "./stage.mjs";
import { floorSVG, toranSVG } from "./scenes/diwali-art.mjs";

test("the toran bottom is the lowest thing drawn, plus the settle overshoot", () => {
  // Lowest point of a mango leaf: the deepest swag point plus the leaf's height.
  const t = 5 / 9; // the deepest swag point that carries a leaf
  const swag = TORAN.top + TORAN.sag * t * (1 - t);
  assert.equal(toranBottom(), swag + TORAN.leaf + TORAN.overshoot);
});

test("the drawn toran stays inside the geometry (every y it draws is above the bottom)", () => {
  const svg = toranSVG("t-", 0, 400);
  const ys = [...svg.matchAll(/ cy="([\d.]+)"/g)].map((m) => Number(m[1]));
  assert.ok(ys.length > 0);
  for (const y of ys) {
    assert.ok(
      y + 5.4 <= toranBottom() - TORAN.overshoot + 0.01,
      `bloom at ${y}`
    );
  }
  const leafY = [...svg.matchAll(/<path d="M[\d.-]+ ([\d.]+) q/g)].map((m) =>
    Number(m[1])
  );
  for (const y of leafY) {
    assert.ok(
      y + TORAN.leaf <= toranBottom() - TORAN.overshoot + 0.01,
      `leaf at ${y}`
    );
  }
});

test("the fireworks reach above the mark by their highest spark, from the table", () => {
  const above = fireworksAboveMark();
  // Burst 2 is the highest: centre 4 units above the mark's top, sparks rise ~112 more.
  assert.ok(above > 100 && above < 130, `reach ${above}`);
  // It is the highest of the three bursts (burst 2), not just any one.
  const rises = BURSTS.map((b) => MARK_TOP - b.y + burstRise(b));
  assert.equal(above, rises[1]);
});

test("the hero's top is held just below the toran, scaled with the stage", () => {
  assert.ok(heroTopMin(1) > toranBottom());
  assert.ok(heroTopMin(1) < toranBottom() + 20);
  assert.ok(Math.abs(heroTopMin(1.25) - 1.25 * heroTopMin(1)) < 1e-9);
});

test("at the least fireworks shift the highest spark is still below the toran", () => {
  const dy = fireworksMinDy();
  const tops = BURSTS.map((b) => b.y + dy - burstRise(b));
  assert.ok(
    Math.min(...tops) >= toranBottom(),
    `highest spark at ${Math.min(...tops)}`
  );
  // ... and any less shift would put it over the toran.
  const less = BURSTS.map((b) => b.y + dy - 1 - burstRise(b));
  assert.ok(Math.min(...less) < toranBottom() + 8);
});

// ---- the floor row (diyas and flowerpots) on a short stage ------------------------------
// Numbers below are in the mock's units unless named px. A tall stage keeps the row as
// drawn; a short one pulls the row's bottom up to the stage bottom minus FLOOR_GAP_PX and
// shrinks the row by the same amount, as a group.
test("the floor row's extent comes from the diya the art draws", () => {
  const svg = floorSVG("t-", 300, floorFit({ ground: 300, hostH: 2000, s: 1 }));
  // The diya sits `lift` above the baseline at DIYA.scale; the glow and shadow numbers
  // the extent is built from are the ones in the markup.
  assert.ok(
    svg.includes(`translate(90 ${300 - DIYA.lift}) scale(${r2(DIYA.scale)})`)
  );
  assert.ok(svg.includes('cy="-10" r="22"')); // glow: top at -10 - 22 = glowTop
  assert.ok(svg.includes('cy="14.5" rx="16" ry="2.4"')); // shadow: bottom at 16.9
  assert.equal(DIYA.glowTop, -10 - 22);
  assert.equal(DIYA.shadowBottom, 14.5 + 2.4);
  assert.ok(FLOOR_ROW.up > 50 && FLOOR_ROW.up < 60, `${FLOOR_ROW.up}`);
  assert.ok(FLOOR_ROW.rise >= 0 && FLOOR_ROW.rise < 2);
});

test("a tall stage keeps the row exactly as drawn", () => {
  for (const hostH of [700, 722, 900, 1200, 2000]) {
    const fit = floorFit({ ground: 440, hostH, s: 1.25 });
    assert.deepEqual([fit.scale, fit.lift], [1, 0], `hostH ${hostH}`);
  }
});

test("a short stage shrinks the row and lifts its bottom to the gap above the stage bottom", () => {
  const s = 1.25;
  const hostH = 528;
  const fit = floorFit({ ground: 547 / s, hostH, s });
  assert.ok(fit.scale < 1 && fit.scale >= MIN_FLOOR_SCALE, `${fit.scale}`);
  // The row's bottom lands exactly the gap above the stage bottom.
  assert.ok(Math.abs(fit.anchor * s - (hostH - FLOOR_GAP_PX)) < 1e-9);
  // Its top stays where the unfitted row would have put it.
  const topNatural = fit.bottom - (FLOOR_ROW.up - FLOOR_ROW.rise);
  const topFitted = fit.anchor - fit.scale * (FLOOR_ROW.up - FLOOR_ROW.rise);
  assert.ok(Math.abs(topNatural - topFitted) < 1e-9);
});

test("the scale only grows with the stage height, and never goes below the floor", () => {
  let last = 0;
  for (let hostH = 200; hostH <= 1000; hostH += 4) {
    const { scale } = floorFit({ ground: 440, hostH, s: 1.25 });
    assert.ok(scale >= MIN_FLOOR_SCALE && scale <= 1, `${hostH}: ${scale}`);
    assert.ok(scale >= last, `${hostH}: ${scale} < ${last}`);
    last = scale;
  }
  assert.equal(floorFit({ ground: 440, hostH: 120, s: 1.25 }).scale, 0.55);
  assert.ok(MIN_FLOOR_SCALE >= 0.55);
});

test("however short the stage, the row's bottom is never lower than the stage bottom minus the gap", () => {
  for (const s of [0.7, 1, 1.25]) {
    for (let hostH = 150; hostH <= 900; hostH += 10) {
      const fit = floorFit({ ground: 600 / s, hostH, s });
      assert.ok(
        fit.anchor * s <= hostH - FLOOR_GAP_PX + 1e-9,
        `s ${s} hostH ${hostH}`
      );
    }
  }
});

test("a baseline that is not a number leaves the row alone", () => {
  const fit = floorFit({ ground: NaN, hostH: 500, s: 1 });
  assert.deepEqual([fit.scale, fit.lift], [1, 0]);
});

test("the transform maps the natural bottom onto the anchor and scales about the centre", () => {
  const fit = floorFit({ ground: 440, hostH: 480, s: 1.25 });
  const [x, y] = fitPoint(fit, [90, fit.bottom]);
  assert.ok(Math.abs(y - fit.anchor) < 1e-9);
  assert.ok(Math.abs(x - (400 + fit.scale * (90 - 400))) < 1e-9);
  assert.ok(floorTransform(fit).startsWith("translate(400 "));
  const still = floorFit({ ground: 440, hostH: 2000, s: 1.25 });
  const [px, py] = fitPoint(still, [123, 456]);
  assert.ok(Math.abs(px - 123) < 1e-9 && Math.abs(py - 456) < 1e-9);
});
