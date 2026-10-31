// The Holi scene: the contract every scene keeps (scene-runtime.mjs), plus what is
// particular to Holi. No browser: a tiny stand-in for the DOM records the markup and the
// animations the scene asks for. Nothing in Holi flashes: the splats build up one after
// another and stay, which the photosensitivity checks read from the scene's own data.
import { afterEach, mock, test } from "node:test";
import assert from "node:assert/strict";
import * as scene from "./holi.mjs";
import { END_MS } from "./holi.mjs";
import {
  BUILD,
  FLOOR_ROW,
  REACH,
  SPLATS,
  bandOf,
  placeSplats,
} from "./holi-art.mjs";
import { heroKeepOut } from "./scene-place.mjs";
import { MIN_FLOOR_SCALE } from "../scene-geometry.mjs";

import {
  contractSuite,
  floorTransformOf,
  run,
  stageBoxes as boxes,
} from "./day-scene-fakes.test-support.mjs";

contractSuite("holi", scene, { row: FLOOR_ROW });

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
  assert.equal(
    svg.querySelectorAll(".t-splat").length,
    9,
    "the low splats are floor props"
  );
});

test("a first showing has every splat; the low ones are the floor group", () => {
  const { svg } = start();
  assert.equal(svg.querySelectorAll(".t-splat").length, SPLATS.length);
  assert.equal(svg.querySelectorAll(".t-floor").length, 1);
  assert.equal(svg.querySelectorAll(".t-splats").length, 2);
});

// ---- the floor group fits a short stage ---------------------------------------------------------

test("the floor group's stated extent matches the low splats", () => {
  const low = SPLATS.filter((d) => bandOf(d[1]) === "low");
  assert.equal(low.length, 4);
  const top = Math.min(...low.map(([, y, r]) => y - r * 1.34));
  assert.ok(FLOOR_ROW.top <= top, `top ${top}`);
  assert.ok(Math.max(...low.map(([, y]) => y)) <= FLOOR_ROW.bottom);
});

// ---- ids and classes --------------------------------------------------------------------------

// ---- photosensitivity: the splats build up, they never flash ------------------------------------

const splatAnims = () =>
  anims().filter((a) => a.node.classes.includes("t-splat"));

test("nothing flashes: there is no flash table", () => {
  assert.equal(scene.FLASHES, undefined, "there is no flash table");
});

test("every splat lands in order, at least 150 ms after the last, and stays opaque", () => {
  start();
  const anims = splatAnims();
  assert.equal(anims.length, SPLATS.length);
  const starts = anims.map((a) => a.options.delay).sort((a, b) => a - b);
  starts.forEach(
    (d, i) => i && assert.ok(d - starts[i - 1] >= 150, `splat ${i}`)
  );
  for (const a of anims) {
    const held = a.frames
      .slice(1)
      .map((f) => f.opacity)
      .filter((o) => o !== undefined);
    assert.ok(
      held.length >= 2 && held.every((o) => o >= BUILD.held),
      "no splat dips back"
    );
    assert.equal(a.frames[0].opacity, 0);
    assert.equal(a.frames.at(-1).opacity, BUILD.held);
  }
});

test("the layer fades out slowly at the end, in one gentle ramp", () => {
  start();
  const fades = anims().filter((a) => a.node.classes.includes("t-splats"));
  assert.equal(fades.length, 2);
  for (const f of fades) {
    assert.deepEqual(
      f.frames.map((x) => x.opacity),
      [1, 1, 0]
    );
    assert.ok(f.options.duration * (1 - BUILD.fadeFrom) >= 1000);
  }
});

test("no splat covers more than 25% of the layer at 1024, 1440 or 1920 wide", () => {
  const biggest = Math.max(...SPLATS.map((d) => d[2])) * 1.34 * 1.16; // body at the squash peak
  for (const w of [1024, 1440, 1920]) {
    const s = Math.min(w / 800, 1.25);
    for (const h of [400, 600, 900]) {
      assert.ok((Math.PI * (biggest * s) ** 2) / (w * h) <= 0.25, `${w}x${h}`);
    }
  }
  assert.ok(
    BUILD.first + (SPLATS.length - 1) * BUILD.every + BUILD.ms <
      END_MS * BUILD.fadeFrom
  );
});

// ---- the splats stay clear of the hero ---------------------------------------------------------

function placement({
  w = 1200,
  h = 760,
  markY = 220,
  heading = 280,
  shiftMark = 0,
}) {
  const b = boxes({ w, h, markY });
  b.heading.w = heading;
  const s = Math.min(w / 800, 1.25);
  const cx = b.mark.x + b.mark.w / 2;
  const geo = {
    from: 400 - cx / s,
    to: 400 + (w - cx) / s,
    mt: b.mark.y / s + shiftMark,
    ground: (b.cards.y + b.cards.h) / s - 2,
    keep: heroKeepOut(b, s),
  };
  return { geo, placed: placeSplats(geo), s };
}

test("no splat, however far it reaches, touches the hero (mark, heading, sub-line)", () => {
  for (const w of [768, 1000, 1200, 1600, 1920, 2560]) {
    for (const heading of [180, 280, 520, 680]) {
      for (const markY of [40, 160, 280]) {
        const { geo, placed } = placement({ w, heading, markY });
        for (const sp of placed) {
          const hw = sp.def[2] * REACH;
          const k = geo.keep;
          const hit =
            sp.x + hw > k.l &&
            sp.x - hw < k.r &&
            sp.y + hw > k.t &&
            sp.y - hw < k.b;
          assert.ok(!hit, `splat ${sp.i} at ${w}/${heading}/${markY}`);
          // A splat the page had to move is moved to where it fits; the others bleed off
          // the edge on purpose.
          const home =
            sp.def[0] < 400 ? geo.from + sp.def[0] : geo.to - (800 - sp.def[0]);
          if (sp.x !== home) {
            assert.ok(
              sp.x - hw >= geo.from - 1e-9 && sp.x + hw <= geo.to + 1e-9
            );
          }
        }
      }
    }
  }
});

test("a normal screen keeps all thirteen splats; each stays on its own side", () => {
  for (const w of [1000, 1440, 1920]) {
    const { placed, geo } = placement({ w });
    assert.equal(placed.length, SPLATS.length, `${w}`);
    for (const sp of placed) {
      assert.equal(sp.x < 400, SPLATS[sp.i][0] < 400, `splat ${sp.i}`);
    }
    assert.ok(geo.to - geo.from >= 800);
  }
});

test("a long heading pushes a splat that would sit under it out to the side", () => {
  const roomy = placement({ w: 1000, heading: 180 });
  const long = placement({ w: 1000, heading: 680 });
  const x = (r, i) => r.placed.find((p) => p.i === i).x;
  assert.equal(
    x(roomy, 11),
    roomy.geo.from + SPLATS[11][0],
    "this one is fine at rest"
  );
  assert.ok(
    x(long, 11) < x(roomy, 11),
    "and moves out when the heading is long"
  );
});

test("on a wide stage the edge splats keep their distance from the edge", () => {
  const { placed, geo } = placement({ w: 1920 });
  const first = placed.find((p) => p.i === 0);
  assert.equal(first.x, geo.from + SPLATS[0][0]);
  const last = placed.find((p) => p.i === 4);
  assert.equal(last.x, geo.to - (800 - SPLATS[4][0]));
});

test("the low splats ride the ground and the side ones follow the mark", () => {
  const roomy = placement({ markY: 220 });
  const lower = placement({ markY: 320 });
  const y = (r, i) => r.placed.find((p) => p.i === i).y;
  assert.equal(y(roomy, 0), SPLATS[0][1], "top row stays");
  assert.ok(y(lower, 10) > y(roomy, 10), "a side splat moves with the mark");
  assert.equal(y(roomy, 7) - y(lower, 7), 0, "a low splat does not");
  assert.equal(y(roomy, 7), SPLATS[7][1] + roomy.geo.ground - FLOOR_ROW.base);
});

// ---- the gulal ----------------------------------------------------------------------------------

test("the gulal is thrown from the lower left and lands on the left cheek, where it now is", () => {
  const ctx = start({ boxes: boxes({ markY: 220 }) });
  assert.equal(ctx.svg.querySelectorAll(".t-hsp").length, 0);
  ctx.move(boxes({ markY: 260 }));
  tick(3100 - 800);
  const ball = anims()
    .filter((a) => a.node.classes.includes("t-ball"))
    .at(-1);
  assert.equal(ball.options.duration, 800);
  assert.equal(ball.frames.length, 13);
  const at = (f) =>
    /translate\(([-\d.]+)px, ([-\d.]+)px\)/
      .exec(f.transform)
      .slice(1)
      .map(Number);
  const [sx, sy] = at(ball.frames[0]);
  const [ex, ey] = at(ball.frames[12]);
  const s = 1.25;
  const k = 72 / 56;
  assert.ok(sx < ex && sy > ey, "from the lower left, up to the face");
  assert.ok(Math.abs(ex - (400 + (14.5 - 28) * k)) < 0.01);
  assert.ok(Math.abs(ey - (260 / s + 38 * k)) < 0.01);
  assert.equal(ball.frames[12].opacity, 0);
});

test("22 sparks fly from the cheek the moment it lands", () => {
  const ctx = start();
  tick(3100 - 800);
  assert.equal(ctx.svg.querySelectorAll(".t-hsp").length, 22);
  const delays = new Set(
    anims()
      .filter((a) => a.node.classes.includes("t-hsp"))
      .map((a) => a.options.delay)
  );
  assert.deepEqual([...delays], [800]);
});

test("the splats on the floor row stay at their edge of the stage when the row shrinks on a short stage", () => {
  const ctx = start();
  const short = boxes({ h: 500, cardsBottom: 700 });
  ctx.move(short);
  ctx.scene.relayout(short);
  const fit = floorTransformOf(current);
  assert.ok(fit.scale < 1, `shrunk to ${fit.scale}`);
  const held = ctx.svg.querySelectorAll(".t-edge");
  assert.ok(held.length >= 3, "the low splats are held");
  for (const el of held) {
    const x = Number(el.dataset.x);
    const [, shift] = /translate\(([-\d.]+) 0\)/.exec(
      el.getAttribute("transform")
    );
    const shown = 400 + fit.scale * (x + Number(shift) - 400);
    assert.ok(Math.abs(shown - x) < 0.01, `splat at ${x} shows at ${shown}`);
  }
});
