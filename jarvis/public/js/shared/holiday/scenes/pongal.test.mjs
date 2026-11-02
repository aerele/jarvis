// The Pongal scene: the contract every scene keeps (scene-runtime.mjs), plus what is
// particular to Pongal. No browser: a tiny stand-in for the DOM records the markup and the
// animations the scene asks for. Nothing in this scene flashes, so the photosensitivity
// check is that the glows are slow ramps and the only fast fades are specks.
import { afterEach, mock, test } from "node:test";
import assert from "node:assert/strict";
import * as scene from "./pongal.mjs";
import { END_MS } from "./pongal.mjs";
import {
  FLOOR_ROW,
  RAMP_MS,
  SUN,
  caneSVG,
  kolamSVG,
  sunMinY,
  TORAN_BOTTOM,
  toranSVG,
} from "./pongal-art.mjs";
import {
  GAP,
  MIN_FLOOR_SCALE,
  TORAN,
  sceneHeroTop,
} from "../scene-geometry.mjs";

import {
  contractSuite,
  floorTransformOf,
  run,
  stageBoxes as boxes,
} from "./day-scene-fakes.test-support.mjs";

contractSuite("pongal", scene, { row: FLOOR_ROW });

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
  assert.equal(svg.querySelectorAll(".t-kl").length, 0);
  assert.ok(svg.querySelector(".t-toran"), "the sky art still plays");
});

test("a first showing has the floor props", () => {
  const { svg } = start();
  assert.equal(svg.querySelectorAll(".t-floor").length, 1);
  assert.equal(svg.querySelectorAll(".t-kl").length, 3);
  assert.equal(svg.querySelectorAll(".t-grow").length, 4);
});

// ---- the floor group fits a short stage ---------------------------------------------------------

test("the floor group's stated extent matches what is drawn", () => {
  const drawn = [
    ...caneSVG("t-", 742, 154, -8).matchAll(/Q([-\d.]+) ([-\d.]+) /g),
  ];
  const top = Math.min(...drawn.map((m) => Number(m[2]))) + 364;
  assert.equal(top, FLOOR_ROW.top);
  const ys = [];
  for (const d of kolamSVG("t-").matchAll(/ d="([^"]*)"/g)) {
    const n = d[1].match(/[\d.]+/g).map(Number);
    for (let i = 1; i < n.length; i += 2) ys.push(n[i]);
  }
  const low = Math.max(...ys);
  assert.ok(
    low <= FLOOR_ROW.bottom && low > FLOOR_ROW.bottom - 3,
    `kolam low ${low}`
  );
});

test("a hero with no room left for the kolam and the hearth even at the row's smallest leaves them out", () => {
  const page = boxes();
  const crowded = { ...page, hero: { x: 0, y: 0, w: 680, h: 640 } };
  const roomy = { ...page, hero: { x: 0, y: 0, w: 680, h: 300 } };
  const { svg: tight } = start({ boxes: crowded });
  assert.equal(tight.querySelectorAll(".t-kl").length, 0, "left out");
  assert.equal(
    tight.querySelectorAll(".t-floor").length,
    1,
    "the rest of the row stays"
  );
  const { svg: fine } = start({ boxes: roomy });
  assert.ok(fine.querySelectorAll(".t-kl").length > 0, "kept when it fits");
});

test("a relayout that leaves the hero no room for the kolam and the hearth takes them out then, and they stay out", () => {
  const { scene, svg, move } = start();
  const page = boxes();
  assert.ok(svg.querySelectorAll(".t-kl").length > 0, "drawn at the start");
  const crowded = { ...page, hero: { x: 0, y: 0, w: 680, h: 640 } };
  move(crowded);
  scene.relayout(crowded);
  assert.equal(svg.querySelectorAll(".t-kl").length, 0, "taken out");
  assert.equal(
    svg.querySelectorAll(".t-floor").length,
    1,
    "the rest of the row stays"
  );
  assert.equal(
    svg.querySelectorAll(".t-edge").length,
    4,
    "the held props stay"
  );
  move(page);
  scene.relayout(page);
  assert.equal(svg.querySelectorAll(".t-kl").length, 0, "not drawn again");
});

test("the canes stay at their edge of the stage when the row shrinks on a short stage", () => {
  const { scene, svg, move } = start();
  const short = boxes({ h: 500, cardsBottom: 700 });
  move(short);
  scene.relayout(short);
  const fit = floorTransformOf(current);
  assert.ok(fit.scale < 1, `shrunk to ${fit.scale}`);
  const canes = svg.querySelectorAll(".t-edge");
  assert.equal(canes.length, 4);
  for (const el of canes) {
    const x = Number(el.dataset.x);
    const [, shift] = /translate\(([-\d.]+) 0\)/.exec(
      el.getAttribute("transform")
    );
    const shown = 400 + fit.scale * (x + Number(shift) - 400);
    assert.ok(Math.abs(shown - x) < 0.01, `cane at ${x} shows at ${shown}`);
  }
});

// ---- ids and classes --------------------------------------------------------------------------

// ---- photosensitivity -------------------------------------------------------------------------

test("nothing flashes: no flash table, glows ramp up slowly and stay", () => {
  assert.equal(scene.FLASHES, undefined, "there is no flash table");
  for (const [name, ms] of Object.entries(RAMP_MS)) assert.ok(ms >= 500, name);
  start();
  for (const cls of ["t-sun", "t-hglow"]) {
    const anim = anims()
      .filter((a) => a.node.classes.includes(cls))
      .at(-1);
    assert.ok(anim, cls);
    assert.equal(anim.frames.at(-1).opacity, 1, `${cls} stays lit`);
    assert.ok(anim.options.duration >= 500);
  }
});

test("only specks fade in and out within a second", () => {
  start();
  const fast = anims().filter((a) => {
    const o = a.frames.map((f) => f.opacity).filter((v) => v !== undefined);
    return o.some((v, i) => i && v < o[i - 1]) && a.options.duration <= 1000;
  });
  assert.ok(fast.length > 0, "the bubbles are checked");
  for (const a of fast) {
    assert.equal(a.node.tag, "circle");
    assert.ok(Number(a.node.attrs.r) <= 5, `r ${a.node.attrs.r}`);
  }
});

// ---- the sun, the thoranam and the hero --------------------------------------------------------

test("the drawn thoranam stays inside the geometry (its lowest point plus the settle)", () => {
  const markup = toranSVG("t-", -300, 1100);
  let low = 0;
  for (const m of markup.matchAll(
    / d="M[-\d.]+ ([-\d.]+) q[-\d.]+ [\d.]+ 0 ([\d.]+) q/g
  )) {
    low = Math.max(low, Number(m[1]) + Number(m[2]));
  }
  for (const m of markup.matchAll(
    /<circle cx="[-\d.]+" cy="([-\d.]+)" r="4.6"/g
  )) {
    low = Math.max(low, Number(m[1]) + 4.6);
  }
  assert.ok(low > 30);
  assert.ok(Math.abs(low + TORAN.overshoot - TORAN_BOTTOM) < 0.01, `${low}`);
});

// The sun's final y, read from the animation the scene gave it.
const sunY = () => {
  const anim = anims()
    .filter((a) => a.node.classes.includes("t-sun"))
    .at(-1);
  return Number(/, ([\d.]+)px\)/.exec(anim.frames.at(-1).transform)[1]);
};

test("the sun is clamped under the thoranam wherever the mark is", () => {
  for (const markY of [0, 5, 40, 90, 160, 300]) {
    start({ boxes: boxes({ markY }) });
    assert.ok(sunY() - SUN.r >= TORAN_BOTTOM + GAP - 1e-9, `mark at ${markY}`);
  }
});

test("the sun follows a mark that sits low, beside it and above the greeting", () => {
  start({ boxes: boxes({ markY: 400 }) }); // mt 320 units
  assert.equal(sunY(), 320 + SUN.dy);
  // beside: its left ray tips clear the mark (72 units wide, centred) by GAP
  assert.ok(400 + SUN.dx - SUN.r >= 400 + 36 + GAP);
  // above the greeting, which starts 116 units under the mark's top
  assert.ok(SUN.dy + SUN.r < 116 - GAP);
});

test("the page keeps the mark low enough that the sun fits under the thoranam", () => {
  for (const s of [0.6, 1, 1.25]) {
    const mt = sceneHeroTop("pongal", s) / s;
    assert.ok(mt + SUN.dy - SUN.r >= TORAN_BOTTOM + GAP - 1e-9, `s ${s}`);
    assert.ok(sunMinY() - SUN.dy <= mt + 1e-9);
  }
});

// ---- Jarvis's own pot boils over -----------------------------------------------------------------

const r2 = (n) => Math.round(n * 100) / 100;
const K = 72 / 56;

test("the froth is read from the mark again before the pot boils over, and follows it", () => {
  const ctx = start({ boxes: boxes({ markY: 250 }) }); // mt 200 units
  assert.equal(ctx.svg.querySelectorAll(".t-fr").length, 0);
  ctx.move(boxes({ markY: 300 })); // the page moved the mark: mt 240
  tick(3050 - 780);
  assert.equal(ctx.svg.querySelectorAll(".t-fr").length, 14);
  const pin = ctx.svg.querySelectorAll(".t-pin").at(-1);
  assert.equal(
    pin.attrs.transform,
    `translate(${r2(400 + 16.5 * K)} ${r2(240 + 39 * K)})`
  );
  const delays = anims()
    .filter((a) => a.node.classes.includes("t-fr"))
    .map((a) => a.options.delay);
  assert.equal(delays[0], 830);
  assert.ok(delays.every((d, i) => i === 0 || d > delays[i - 1]));
  ctx.scene.relayout(boxes({ markY: 375 })); // mt 300
  assert.equal(
    pin.attrs.transform,
    `translate(${r2(400 + 16.5 * K)} ${r2(300 + 39 * K)})`
  );
});

test("the sugarcane stalks get a lighter end stop in the dark theme only", () => {
  const { svg } = start();
  const css = /<style>([\s\S]*?)<\/style>/.exec(current.markup())[1];
  assert.match(
    css,
    /\[data-theme="dark"\] \.t-stalk\{stroke:url\(#t-scaned\)\}/
  );
  const stop = (id) =>
    [
      ...current
        .markup()
        .matchAll(new RegExp(`id="${id}"[^>]*>(.*?)</linearGradient>`, "g")),
    ][0][1];
  assert.match(stop("t-scane"), /#2e0a1e/, "the light theme is unchanged");
  assert.doesNotMatch(stop("t-scaned"), /#2e0a1e/);
  assert.ok(svg.querySelectorAll(".t-stalk").length === 4);
});
