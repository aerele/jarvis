// The Diwali art's own geometry, as the page needs it before the art is loaded: where the
// toran ends and how far above the mark the fireworks reach.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  BURSTS,
  DIYA,
  FLOOR_UNDER_HERO,
  groundOf,
  heroFloorTop,
  FLOOR_GAP_PX,
  CLEAR,
  GAP,
  FLOOR_ROW,
  HERO_FLOOR_GAP_PX,
  HERO_TOP_UNITS,
  MARK_TOP,
  MIN_FLOOR_SCALE,
  TORAN,
  WORN_GAP_PX,
  WORN_OVERHANG,
  wornOverhangUnits,
  burstRise,
  fireworksAboveMark,
  fireworksMinDy,
  fitPoint,
  floorFit,
  floorTransform,
  TILE_K,
  dayHeroTop,
  heroTopMin,
  sceneHeroTop,
  toranBottom,
  TIGHT_SLACK_PX,
} from "./scene-geometry.mjs";
import { DESIGNS } from "./answer.mjs";
import { r2 } from "./stage.mjs";
import * as birthdayArt from "./scenes/birthday-art.mjs";
import * as onamArt from "./scenes/onam-art.mjs";
import * as holiArt from "./scenes/holi-art.mjs";
import * as christmasArt from "./scenes/christmas-art.mjs";
import * as eidArt from "./scenes/eid-art.mjs";
import * as newyearArt from "./scenes/newyear-art.mjs";
import * as pongalArt from "./scenes/pongal-art.mjs";
import * as independenceArt from "./scenes/independence-art.mjs";
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
// Numbers below are in stage units unless named px. A tall stage keeps the row as
// drawn; a short one pulls the row's bottom up to the stage bottom minus FLOOR_GAP_PX and
// shrinks the row by the same amount, as a group.
test("the floor row's extent comes from the diya the art draws", () => {
  const svg = floorSVG("t-", 300);
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

// ---- how low the hero must sit, per design (the page needs it before the art arrives) ---------

// What each scene's own art needs, in px at scale s, from the numbers the art states. The
// table in scene-geometry.mjs is held to these.
const MARK_UNITS = TILE_K;
const DERIVED = {
  diwali: heroTopMin,
  christmas: (s) => dayHeroTop(s, christmasArt.SLEIGH_MIN_ROOM),
  newyear: (s) => (CLEAR + newyearArt.yearRoom()) * s,
  birthday: (s) => dayHeroTop(s, birthdayArt.STREAMER.min),
  eid: (s) => dayHeroTop(s),
  pongal: (s) => (pongalArt.sunMinY() - pongalArt.SUN.dy) * s,
  onam: (s) =>
    (onamArt.UMBRELLA.minRise - onamArt.UMBRELLA.top + onamArt.UMBRELLA.gap) *
    MARK_UNITS *
    s,
  independence: (s) =>
    (independenceArt.KITE.up + GAP - independenceArt.HIGH_KITE_DY) * s,
};

test("every design with a top decoration has a hero minimum; Holi has none", () => {
  const withEntry = Object.keys(HERO_TOP_UNITS).sort();
  assert.deepEqual(withEntry, Object.keys(DERIVED).sort());
  assert.deepEqual(
    DESIGNS.filter((d) => !withEntry.includes(d)),
    ["holi"]
  );
  assert.equal(sceneHeroTop("holi", 1), 0);
  assert.equal(sceneHeroTop("toString", 1), 0);
});

test("the table holds each scene's own number: never below it (the art needs no more than the table), and within 0.01 above", () => {
  for (const [design, units] of Object.entries(HERO_TOP_UNITS)) {
    const own = DERIVED[design](1);
    assert.ok(units >= own - 1e-9, `${design}: ${units} < ${own}`);
    assert.ok(
      units - own < 0.01,
      `${design}: ${units} is more than the art needs ${own}`
    );
  }
});

test("the hero minimum scales with the stage; the day scenes keep the Diwali clearance", () => {
  for (const design of Object.keys(HERO_TOP_UNITS)) {
    assert.equal(sceneHeroTop(design, 1.25), HERO_TOP_UNITS[design] * 1.25);
  }
  for (const design of ["christmas", "newyear", "birthday", "eid"]) {
    assert.ok(HERO_TOP_UNITS[design] >= CLEAR, design);
  }
});

test("a floor row of its own height: floorFit takes the row, the Diwali row by default", () => {
  const args = { ground: 560, hostH: 500, s: 1 };
  assert.deepEqual(floorFit({ ...args, row: FLOOR_ROW }), floorFit(args));
  const tall = { up: 200, rise: 0 };
  const over = 560 - (500 - FLOOR_GAP_PX);
  assert.equal(floorFit({ ...args, row: tall }).scale, 1 - over / 200);
  assert.equal(
    floorFit({ ...args, row: { up: 20, rise: 0 } }).scale,
    MIN_FLOOR_SCALE
  );
});

// ---- the row keeps clear of the hero's bottom ----------------------------------------------------

// A stage where the row, at scale 1, would reach up past the hero's bottom: the baseline at 400
// units, the row 100 units tall, and the hero ends (plus its gap) at y = 340.
const CROWDED = {
  ground: 400,
  hostH: 2000,
  s: 1,
  row: { up: 100, rise: 0 },
  clearTop: 340,
};

test("a row that would reach past the hero's bottom shrinks about its bottom until it clears", () => {
  const fit = floorFit(CROWDED);
  assert.equal(fit.scale, 0.6); // 60 units of room under clearTop, 100 units of row
  assert.equal(fit.anchor, 400); // the bottom stays put: only the top comes down
  assert.ok(Math.abs(fit.anchor - fit.scale * 100 - 340) < 1e-9);
});

test("a row with room, or with no hero given, is not touched by the hero rule", () => {
  assert.equal(floorFit({ ...CROWDED, clearTop: 300 }).scale, 1);
  assert.equal(floorFit({ ...CROWDED, clearTop: undefined }).scale, 1);
  assert.equal(floorFit({ ...CROWDED, clearTop: NaN }).scale, 1);
});

test("the hero rule never shrinks past the minimum scale, and a stage-height shrink still counts", () => {
  assert.equal(floorFit({ ...CROWDED, clearTop: 395 }).scale, MIN_FLOOR_SCALE);
  // Short stage: the row is already at 0.7 from the stage-height rule; the hero rule asks for 0.6.
  const both = floorFit({
    ...CROWDED,
    hostH: 400 - 0 + FLOOR_GAP_PX - 0,
    clearTop: 340,
  });
  assert.ok(
    both.scale <= 0.6 + 1e-9 && both.scale >= MIN_FLOOR_SCALE,
    `${both.scale}`
  );
  // The hero rule alone never makes a row bigger than the stage-height rule left it.
  const tall = floorFit({
    ground: 560,
    hostH: 500,
    s: 1,
    row: { up: 200, rise: 0 },
    clearTop: 0,
  });
  assert.equal(
    tall.scale,
    floorFit({ ground: 560, hostH: 500, s: 1, row: { up: 200, rise: 0 } }).scale
  );
});

test("only the part of the row under the hero counts: `mid` is its height, null means nothing is there", () => {
  const sides = { up: 100, rise: 0, mid: null }; // tall props at the ends, nothing in the middle
  assert.equal(floorFit({ ...CROWDED, row: sides }).scale, 1);
  const low = { up: 100, rise: 0, mid: 40 }; // a low centrepiece between tall ends
  assert.equal(floorFit({ ...CROWDED, row: low, clearTop: 340 }).scale, 1);
  assert.equal(floorFit({ ...CROWDED, row: low, clearTop: 370 }).scale, 0.75);
  // No `mid` at all: the whole row's height counts.
  assert.equal(floorFit({ ...CROWDED, row: { up: 100, rise: 0 } }).scale, 0.6);
});

test("a raised bottom (rise) is part of the row's height for the hero rule", () => {
  const raised = { up: 100, rise: 20 };
  const fit = floorFit({ ...CROWDED, row: raised, clearTop: 340 });
  // The drawn bottom is at 380; 40 units of room, 80 units of row.
  assert.equal(fit.scale, 0.55);
  assert.ok(
    fit.anchor - fit.scale * 80 >= 340 - 1e-9 || fit.scale === MIN_FLOOR_SCALE
  );
});

test("the hero gap is 16 px", () => {
  assert.equal(HERO_FLOOR_GAP_PX, 16);
});

test("the worn item's overhang table names only designs that have worn art, in tile units", async () => {
  const { DESIGNS } = await import("./worn.mjs");
  for (const [design, units] of Object.entries(WORN_OVERHANG)) {
    assert.ok(DESIGNS.includes(design), design);
    assert.ok(units > 0 && units < 56, `${design} ${units}`);
  }
});

test("wornOverhangUnits: the table value, 0 for a design without one and for a prototype key", () => {
  assert.equal(wornOverhangUnits("christmas"), WORN_OVERHANG.christmas);
  assert.equal(wornOverhangUnits("diwali"), 0);
  assert.equal(wornOverhangUnits("toString"), 0);
  assert.equal(wornOverhangUnits(undefined), 0);
});

test("the item keeps 12 px below the top of the stage", () => {
  assert.equal(WORN_GAP_PX, 12);
});

test("a row the hero is too close to even at its smallest is reported as tight, one that fits is not", () => {
  assert.equal(floorFit({ ...CROWDED, clearTop: 300 }).tight, false);
  assert.equal(floorFit({ ...CROWDED, clearTop: 340 }).tight, false); // fits exactly at 0.6
  assert.equal(floorFit({ ...CROWDED, clearTop: 345 }).tight, false); // fits exactly at 0.55
  assert.equal(floorFit({ ...CROWDED, clearTop: 380 }).tight, true);
  assert.equal(floorFit({ ...CROWDED, clearTop: 395 }).tight, true);
  // The page rounds the hero's place to a whole pixel, so up to a pixel over still fits.
  assert.equal(floorFit({ ...CROWDED, clearTop: 345.9 }).tight, false);
  assert.equal(floorFit({ ...CROWDED, clearTop: 346.2 }).tight, true);
  assert.equal(floorFit({ ...CROWDED, s: 2, clearTop: 345.4 }).tight, false); // 0.8 px
  assert.equal(floorFit({ ...CROWDED, s: 2, clearTop: 345.6 }).tight, true); // 1.2 px
  assert.equal(floorFit({ ...CROWDED, clearTop: undefined }).tight, false);
  assert.equal(floorFit({ ground: NaN, hostH: 500, s: 1 }).tight, false);
});

test("the tight slack is one pixel: a row exactly that far short still fits, a hair more is tight", () => {
  assert.equal(TIGHT_SLACK_PX, 1);
  // At 0.55 the row's top reaches 345 (CROWDED); the slack is in px, so it is 1 / s in units.
  for (const s of [1, 2, 4]) {
    const edge = 345 + TIGHT_SLACK_PX / s;
    const fit = (clearTop) => floorFit({ ...CROWDED, s, clearTop });
    assert.equal(fit(edge).scale, MIN_FLOOR_SCALE);
    assert.equal(fit(edge).tight, false, `s ${s}: at the slack`);
    assert.equal(fit(edge + 1e-6).tight, true, `s ${s}: past the slack`);
  }
});

test("a larger minScale stops the shrink there, and reports tight sooner", () => {
  const roomy = floorFit({ ...CROWDED, clearTop: 346.2 });
  assert.equal(roomy.scale, MIN_FLOOR_SCALE);
  assert.equal(roomy.tight, true);
  const phone = floorFit({ ...CROWDED, clearTop: 300, minScale: 0.8 });
  assert.equal(phone.scale >= 0.8, true);
  assert.equal(
    floorFit({ ...CROWDED, clearTop: 360, minScale: 0.8 }).scale,
    0.8
  );
  assert.equal(
    floorFit({ ...CROWDED, clearTop: 360, minScale: 0.8 }).tight,
    true
  );
  // Without it nothing changes.
  assert.deepEqual(
    floorFit({ ...CROWDED, clearTop: 360 }),
    floorFit({ ...CROWDED, clearTop: 360, minScale: MIN_FLOOR_SCALE })
  );
});

// ---- the rows with something tall under the hero (the page places the hero by them) ----------

const ROWS = {
  christmas: christmasArt.FLOOR_ROW,
  diwali: FLOOR_ROW,
  eid: eidArt.FLOOR_ROW,
  holi: holiArt.FLOOR_ROW,
  newyear: newyearArt.FLOOR_ROW,
  onam: onamArt.FLOOR_ROW,
  pongal: pongalArt.FLOOR_ROW,
};

test("FLOOR_UNDER_HERO lists exactly the rows with a tall part under the hero, as the art states them", () => {
  for (const [design, row] of Object.entries(ROWS)) {
    const under = row.mid === null ? undefined : FLOOR_UNDER_HERO[design];
    if (row.mid === null) {
      assert.ok(!Object.hasOwn(FLOOR_UNDER_HERO, design), design);
      continue;
    }
    const reach = row.mid === undefined ? row.up : row.mid;
    assert.ok(
      Math.abs(under.reach - reach) < 0.01,
      `${design} reach ${under.reach} vs ${reach}`
    );
    assert.ok(Math.abs(under.rise - row.rise) < 0.01, `${design} rise`);
  }
});

test("heroFloorTop is where floorFit puts the middle part of the row at its smallest scale", () => {
  for (const design of Object.keys(FLOOR_UNDER_HERO)) {
    const row = ROWS[design];
    for (const [hostH, cardsBottomPx] of [
      [700, 684],
      [560, 700],
      [900, 884],
    ]) {
      const s = 1.25;
      const got = heroFloorTop({ design, s, hostH, cardsBottomPx });
      // A hero so low that the row is squeezed to its minimum: the fit's own core top.
      const fit = floorFit({
        row,
        ground: groundOf(cardsBottomPx, s),
        hostH,
        s,
        clearTop: 10000,
      });
      assert.equal(fit.scale, MIN_FLOOR_SCALE);
      const reach = row.mid === undefined ? row.up : row.mid;
      const want = (fit.anchor - fit.scale * (reach - row.rise)) * s;
      assert.ok(
        Math.abs(got - want) < 1e-6,
        `${design} ${hostH}: ${got} vs ${want}`
      );
    }
  }
});

test("heroFloorTop has nothing to say for a design with no tall part under the hero, or for no ground", () => {
  assert.equal(
    heroFloorTop({ design: "holi", s: 1, hostH: 700, cardsBottomPx: 680 }),
    undefined
  );
  assert.equal(
    heroFloorTop({ design: "toString", s: 1, hostH: 700, cardsBottomPx: 680 }),
    undefined
  );
  assert.equal(
    heroFloorTop({ design: "pongal", s: 1, hostH: 0, cardsBottomPx: 680 }),
    undefined
  );
  assert.equal(
    heroFloorTop({ design: "pongal", s: 1, hostH: 700, cardsBottomPx: NaN }),
    undefined
  );
});
