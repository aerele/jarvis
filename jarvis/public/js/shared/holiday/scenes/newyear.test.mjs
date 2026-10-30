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
  FIREWORKS_MIN_SHIFT,
  FLASHES,
  PEAK,
  YEAR,
  yearPlan,
  yearRoom,
  yearText,
} from "./newyear-art.mjs";
import {
  CLEAR,
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
