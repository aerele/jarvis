// The New Year scene: the shared contract, the year coming from the option (as text), the
// year and the fireworks staying clear of the top and of the hero, the cannons fitting a
// short stage, and photosensitivity (four flashes, read from the table the drawing and the
// animation both use).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as scene from "./newyear.mjs";
import {
  BURSTS,
  CANNON,
  CANNON_REACH,
  FIREWORKS_MIN_SHIFT,
  FLOOR_ROW,
  FLASHES,
  PEAK,
  YEAR,
  yearPlan,
  yearRoom,
  yearText,
} from "./newyear-art.mjs";
import {
  CLEAR,
  HERO_FLOOR_GAP_PX,
  HERO_TOP_UNITS,
  MARK_TOP,
  TILE_K,
  WORN_OVERHANG,
  burstRise,
  sceneHeroTop,
} from "../scene-geometry.mjs";
import {
  boxes,
  contractSuite,
  edgeDrift,
  floorTransformOf,
  run,
  stageBoxes,
} from "./day-scene-fakes.test-support.mjs";

contractSuite("newyear", scene, { launch: 2300 });

const text = (r) => r.host.svg.querySelector(".t-yr");

test("the year is the option, drawn as text", () => {
  for (const year of [2027, 2031, "2040"]) {
    const r = run(scene, { year });
    assert.equal(text(r).text, String(year));
    assert.equal(text(r).tag, "text");
    r.api.settle("instant");
    r.end();
  }
});

test("a year that is not four digits draws no year, and nothing is injected", () => {
  for (const year of [
    undefined,
    null,
    "",
    "abc",
    20277,
    99,
    "<script>1</script>",
  ]) {
    const r = run(scene, { year });
    assert.equal(text(r), null, String(year));
    assert.ok(!r.markup().includes("<script"), String(year));
    r.api.settle("instant");
    r.end();
  }
  assert.equal(yearText("2027"), "2027");
  assert.equal(yearText(2027), "2027");
  assert.equal(yearText(" 2027 "), "", "no digits picked out of other text");
  assert.equal(yearText("a2b0c2d7"), "");
});

test("no year is written into the art or the scene code", () => {
  for (const file of ["newyear.mjs", "newyear-art.mjs"]) {
    const src = readFileSync(new URL(file, import.meta.url), "utf8");
    assert.ok(!/["'`>](19|20|21)\d\d["'`<]/.test(src), `${file} holds a year`);
  }
});

test("the year fits between the clearance line and the party hat, at any mark height", () => {
  const hatReach = WORN_OVERHANG.newyear * TILE_K;
  for (let mt = CLEAR + yearRoom(); mt <= CLEAR + 260; mt += 3) {
    const { px, baseline } = yearPlan({ airTop: CLEAR, mt, k: TILE_K });
    const top = baseline - px * YEAR.cap;
    assert.ok(px >= YEAR.minPx && px <= YEAR.maxPx, `px ${px}`);
    assert.ok(top >= CLEAR + YEAR.pad - 0.01, `mt ${mt}: top ${top}`);
    assert.ok(baseline <= mt - hatReach, `mt ${mt}: the year touches the hat`);
    assert.ok(
      baseline >= mt - hatReach - 40,
      `mt ${mt}: the year floats ${
        mt - hatReach - baseline
      } units above the hat`
    );
  }
  assert.equal(
    yearPlan({ airTop: CLEAR, mt: CLEAR + 300, k: TILE_K }).px,
    YEAR.maxPx
  );
});

test("the year is pinned just above the hat, not centred in the sky, on short and tall stages", () => {
  for (const [h, markY] of [
    [520, sceneHeroTop("newyear", 1.25)],
    [760, 220],
    [1000, 420],
  ]) {
    const r = run(scene, { boxes: stageBoxes({ h, markY }) });
    const s = 1.25;
    const mt = markY / s;
    const hat = mt - WORN_OVERHANG.newyear * TILE_K;
    const baseline = Number(text(r).getAttribute("y"));
    assert.ok(baseline <= hat, `h ${h}: the year is on the hat`);
    assert.ok(baseline >= hat - 40, `h ${h}: ${hat - baseline} units above`);
    const top = baseline - Number(text(r).getAttribute("font-size")) * YEAR.cap;
    assert.ok(top >= CLEAR, `h ${h}: the year is under the clearance line`);
    r.api.settle("instant");
    r.end();
  }
});

test("the year is drawn as laid out for the mark's place", () => {
  const r = run(scene, { boxes: boxes({ w: 1000, markY: 130 }) });
  const s = 1000 / 800;
  const plan = yearPlan({ airTop: CLEAR, mt: 130 / s, k: 96 / s / 56 });
  assert.equal(Number(text(r).getAttribute("font-size")), plan.px);
  assert.equal(Number(text(r).getAttribute("y")), plan.baseline);
  r.api.settle("instant");
  r.end();
});

test("the page keeps the mark low enough that the year fits above the hat", () => {
  const need = CLEAR + yearRoom();
  assert.ok(HERO_TOP_UNITS.newyear >= need - 1e-9);
});

// The centre y of burst n as drawn, from the layer.
const burstY = (r, n) =>
  Number(
    r.host.svg
      .querySelector(`.t-nflash${n}`)
      .parent.getAttribute("transform")
      .match(/translate\([\d.-]+ ([\d.-]+)\)/)[1]
  );

test("at the lowest mark the page allows, the fireworks and the year are already under the clearance line", () => {
  const markY = HERO_TOP_UNITS.newyear * 1.25;
  const r = run(scene, { boxes: stageBoxes({ markY }) });
  for (const b of BURSTS)
    assert.ok(burstY(r, b.n) - burstRise(b) >= CLEAR - 1e-6, `burst ${b.n}`);
  const top = Number(text(r).getAttribute("y")) - YEAR.minPx * YEAR.cap;
  assert.ok(top >= CLEAR - 0.05, `the year's top ${top}`);
  r.api.settle("instant");
  r.end();
});

test("the fireworks never rise past the clearance line, wherever the mark sits", () => {
  for (const markY of [40, 80, 110, 160, 240]) {
    const r = run(scene, { boxes: boxes({ w: 1000, markY }) });
    for (const b of BURSTS) {
      const top = burstY(r, b.n) - burstRise(b);
      assert.ok(top >= CLEAR - 1e-6, `markY ${markY} burst ${b.n}: ${top}`);
    }
    r.api.settle("instant");
    r.end();
  }
  assert.ok(FIREWORKS_MIN_SHIFT > 0);
});

test("the fireworks follow a mark that sits lower than MARK_TOP", () => {
  const low = run(scene, { boxes: boxes({ w: 1000, markY: 400 }) });
  const s = 1000 / 800;
  const shift = 400 / s - MARK_TOP;
  assert.ok(shift > FIREWORKS_MIN_SHIFT);
  BURSTS.forEach((b) => assert.equal(burstY(low, b.n), b.y + shift));
  low.api.settle("instant");
  low.end();
});

test("each rocket leaves the bottom edge and meets its burst", () => {
  const r = run(scene);
  r.tick(1);
  for (const b of BURSTS) {
    const a = r.host.svg.animations.find((x) =>
      x.node.classes.includes(`t-nrk${b.n}`)
    );
    assert.match(
      a.frames[0].transform,
      new RegExp(`${(640 / 1.25).toFixed(0)}px`)
    );
    assert.match(a.frames[1].transform, new RegExp(`translate\\(${b.x}px`));
  }
  r.api.settle("instant");
  r.end();
});

test("the cannons fit a short stage as one group, and a tall stage leaves them", () => {
  const tall = run(scene, { boxes: boxes({ h: 700 }) });
  assert.equal(floorTransformOf(tall).scale, 1);
  tall.api.settle("instant");
  tall.end();
  const short = run(scene, {
    boxes: boxes({ h: 480, cardsH: 100, bottomGap: -40 }),
  });
  const fit = floorTransformOf(short);
  assert.ok(fit.scale < 1 && fit.scale >= 0.55, `scale ${fit.scale}`);
  const row = short.host.svg.querySelector(".t-floor");
  assert.equal(row.querySelectorAll(".t-cl").length, 44);
  assert.equal(row.querySelectorAll(".t-cr").length, 44);
  short.api.settle("instant");
  short.end();
});

// ---- the cannons stay clear of the hero ----------------------------------------------------

// A short window: the stage 1060 x 408 px with the card band running below its bottom, so
// the floor row is lifted and shrunk to its smallest. The hero is the page's own box: the
// mark, heading and sub-line, at most 680 px wide.
function shortPage({ w = 1060, h = 408, heroH = 250 } = {}) {
  const page = stageBoxes({ w, h, markY: 119, cardsBottom: h + 192 });
  return {
    ...page,
    hero: {
      x: w / 2 - Math.min(340, w / 2 - 60),
      y: 119,
      w: Math.min(680, w - 120),
      h: heroH,
    },
  };
}

// Each drawn cannon as a box in px: where it stands, and how far up it reaches, as the row is
// fitted now.
function cannonBoxes(r, page) {
  const s = Math.min(page.host.w / 800, 1.25);
  const cx = page.mark.x + page.mark.w / 2;
  const fit = floorTransformOf(r);
  return r.host.svg.querySelectorAll(".t-edge").map((el) => {
    const x = Number(el.dataset.x);
    const reach = CANNON_REACH * fit.scale;
    return {
      l: cx + (x - 400 - reach) * s,
      r: cx + (x - 400 + reach) * s,
      t: (fit.anchor - FLOOR_ROW.up * fit.scale) * s,
    };
  });
}

// Clear of the hero's box by the gap on one side at least: beside it, or under it.
function clearOfHeroBox(box, hero) {
  const gap = HERO_FLOOR_GAP_PX - 1e-6;
  const beside = box.r <= hero.x - gap || box.l >= hero.x + hero.w + gap;
  const under = box.t >= hero.y + hero.h + gap - 0.5;
  return beside || under;
}

test("the cannon's stated reach is what the drawn barrel reaches sideways", () => {
  const a = (CANNON.tilt * Math.PI) / 180;
  // The farthest corner of the barrel's rim (15 wide, its top 29 above the base) once tilted.
  const reach = (7.5 * Math.cos(a) + 29 * Math.sin(a)) * CANNON.scale;
  assert.ok(CANNON_REACH >= reach && CANNON_REACH <= reach + 1, `${reach}`);
});

test("the cannons stay at their own places when the row shrinks, not drawn in towards the hero", () => {
  const page = shortPage();
  const r = run(scene, { boxes: page });
  assert.ok(floorTransformOf(r).scale < 1, "the row shrank");
  const drift = edgeDrift(r);
  assert.equal(drift.length, 2, "both cannons are held");
  for (const [x, off] of drift)
    assert.ok(off < 0.01, `cannon at ${x} drifts ${off}`);
  r.api.settle("instant");
  r.end();
});

test("at a roomy size the cannons are where they always were, held with an identity shift", () => {
  const r = run(scene);
  assert.deepEqual(
    r.host.svg
      .querySelectorAll(".t-edge")
      .map((el) => [el.dataset.x, el.getAttribute("transform")]),
    [
      ["70", "translate(0 0)"],
      ["730", "translate(0 0)"],
    ]
  );
  r.api.settle("instant");
  r.end();
});

test("on a short stage no cannon is inside the hero's box, or within 16 px of it", () => {
  for (const w of [780, 900, 1060, 1300])
    for (const h of [408, 480, 560, 700]) {
      const page = shortPage({ w, h });
      const r = run(scene, { boxes: page });
      assert.equal(cannonBoxes(r, page).length, 2, "both cannons are drawn");
      for (const box of cannonBoxes(r, page))
        assert.ok(
          clearOfHeroBox(box, page.hero),
          `${w}x${h}: ${JSON.stringify(box)}`
        );
      r.api.settle("instant");
      r.end();
    }
});

test("a stage too narrow to keep both clear puts the cannons as far out as the stage goes", () => {
  const page = shortPage({ w: 700, h: 408 });
  const r = run(scene, { boxes: page });
  const s = 700 / 800;
  const [left, right] = r.host.svg
    .querySelectorAll(".t-edge")
    .map((el) => Number(el.dataset.x));
  const reach = CANNON_REACH * floorTransformOf(r).scale;
  assert.ok(
    left - reach >= 400 - 350 / s - 1e-6,
    "the left one is on the stage"
  );
  assert.ok(
    right + reach <= 400 + 350 / s + 1e-6,
    "the right one is on the stage"
  );
  r.api.settle("instant");
  r.end();
});

test("on a short stage the hat leaves the right cannon's muzzle where that cannon now stands", () => {
  const page = shortPage();
  const r = run(scene, { boxes: page });
  r.tick(2301);
  const fit = floorTransformOf(r);
  const x0 = Number(r.host.svg.querySelectorAll(".t-edge")[1].dataset.x);
  const a = r.host.svg.animations.find((x) =>
    x.node.classes.includes("t-drop")
  );
  const [tx, ty] = a.frames[0].transform
    .match(/translate\(([\d.-]+)px, ([\d.-]+)px\)/)
    .slice(1)
    .map(Number);
  const s = Math.min(page.host.w / 800, 1.25);
  const k = page.mark.w / s / 56;
  // The tile's units back to the stage's: the mark's own column is x = 400.
  const [x, y] = [400 + (tx - 28) * k, page.mark.y / s + ty * k];
  const P = CANNON.scale;
  assert.ok(Math.abs(x - (x0 - 17.9 * P * fit.scale)) < 0.2, `x ${x}`);
  assert.ok(
    Math.abs(y - (fit.anchor - fit.scale * (CANNON.lift + 22.9 * P))) < 0.2,
    `y ${y}`
  );
  r.api.settle("instant");
  r.end();
});

test("the party hat leaves the right cannon and lands on the mark, handing over", () => {
  const r = run(scene);
  r.tick(2301);
  const a = r.host.svg.animations.find((x) =>
    x.node.classes.includes("t-drop")
  );
  const [x, y] = a.frames[0].transform
    .match(/translate\(([\d.-]+)px, ([\d.-]+)px\)/)
    .slice(1)
    .map(Number);
  assert.ok(x > 0 && y > 0, "from the lower right of the mark");
  assert.equal(a.frames.at(-1).opacity, 0);
  assert.equal(a.options.duration, 1150);
  r.api.settle("instant");
  r.end();
});

test("a replay has no cannons; the hat then comes in from the bottom right corner", () => {
  const r = run(scene, { replay: true });
  assert.equal(r.host.svg.querySelectorAll(".t-cl").length, 0);
  r.tick(2301);
  const a = r.host.svg.animations.find((x) =>
    x.node.classes.includes("t-drop")
  );
  assert.ok(a, "the hat still flies");
  r.api.settle("instant");
  r.end();
});

// ---- photosensitivity --------------------------------------------------------------------
// Static, as the Diwali check is: read the scene's own flash table, then the radii actually
// drawn and the starts and lengths actually animated, so none can drift from the table and
// no new flash can slip in.

test("at most 3 flashes start in any 1 s window, and all fit inside the scene", () => {
  for (const f of FLASHES)
    assert.ok(f.at >= 0 && f.at + f.ms <= scene.END_MS, f.cls);
  for (const start of FLASHES.map((f) => f.at)) {
    const n = FLASHES.filter(
      (f) => f.at >= start && f.at < start + 1000
    ).length;
    assert.ok(n <= 3, `${n} flashes from ${start} ms`);
  }
});

test("every flash is drawn once at the table's radius and animated at the table's time", () => {
  const r = run(scene);
  const flashes = r.host.svg.querySelectorAll(".t-fb");
  assert.equal(
    flashes.length,
    FLASHES.length,
    "no flash that the table does not list"
  );
  for (const f of FLASHES) {
    const node = r.host.svg.querySelector(`.t-${f.cls}`);
    assert.equal(Number(node.getAttribute("r")), f.r, f.cls);
    const a = r.host.svg.animations.find((x) => x.node === node);
    assert.equal(a.options.delay, f.at, f.cls);
    assert.equal(a.options.duration, f.ms, f.cls);
  }
  r.api.settle("instant");
  r.end();
});

test("no flash covers more than 25% of the layer, at the desktop sizes", () => {
  for (const [vw, vh] of [
    [1024, 640],
    [1440, 800],
    [1920, 1080],
  ]) {
    // The welcome area is the viewport less the sidebar and the bars, so a worse case than the viewport.
    const [w, h] = [vw - 260, vh - 120];
    const s = Math.min(w / 800, 1.25);
    for (const f of FLASHES) {
      const area = Math.PI * (f.r * PEAK * s) ** 2;
      assert.ok(area / (w * h) <= 0.25, `${f.cls} at ${vw}x${vh}`);
    }
  }
  for (let w = 540; w <= 2600; w += 40)
    for (let h = 300; h <= 1600; h += 40) {
      const s = Math.min(w / 800, 1.25);
      for (const f of FLASHES)
        assert.ok(
          (Math.PI * (f.r * PEAK * s) ** 2) / (w * h) <= 0.25,
          `${f.cls} ${w}x${h}`
        );
    }
});
