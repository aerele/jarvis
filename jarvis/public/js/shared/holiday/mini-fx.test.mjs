// The mini chat's particles are ported from the approved design (MINI_PARTICLE, playMiniFx).
// These tests pin the numbers that make them look the same on every showing.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DESIGNS } from "./answer.mjs";
import {
  MINI_PARTICLE,
  particleMarkup,
  playParticles,
} from "./mini-fx.mjs";

// An svg double: innerHTML in, fake `.mfx` elements out, each remembering its animation.
function fakeSvg() {
  const svg = {
    innerHTML: "",
    animations: [],
    querySelectorAll(selector) {
      assert.equal(selector, ".mfx");
      const count = (this.innerHTML.match(/class="mfx"/g) || []).length;
      return Array.from({ length: count }, () => ({
        animate: (keyframes, options) => {
          const animation = {
            keyframes,
            options,
            cancelled: false,
            cancel() {
              this.cancelled = true;
            },
          };
          svg.animations.push(animation);
          return animation;
        },
      }));
    },
  };
  return svg;
}

test("every design the answer can name has a particle", () => {
  assert.deepEqual(Object.keys(MINI_PARTICLE).sort(), [...DESIGNS].sort());
});

test("an unknown design draws nothing", () => {
  assert.equal(particleMarkup("nope"), "");
  assert.equal(particleMarkup(undefined), "");
});

test("playing runs one 'both'-filled fall per particle with the approved timings", () => {
  const svg = fakeSvg();
  playParticles(svg, "diwali");
  assert.equal(svg.animations.length, 24);
  for (const { keyframes, options } of svg.animations) {
    assert.ok(
      options.duration >= 2600 && options.duration <= 4100,
      options.duration
    );
    assert.ok(options.delay >= 200 && options.delay <= 1100, options.delay);
    assert.equal(options.fill, "both");
    assert.equal(keyframes.length, 4);
    assert.match(keyframes[3].transform, /280px/);
    assert.equal(keyframes[1].opacity, 0.95);
  }
});

test("the same particles fall the same way on every showing", () => {
  const a = fakeSvg();
  const b = fakeSvg();
  playParticles(a, "holi");
  playParticles(b, "holi");
  assert.deepEqual(
    a.animations.map((x) => x.options),
    b.animations.map((x) => x.options)
  );
});

test("cancel stops every animation and clears the svg", () => {
  const svg = fakeSvg();
  const run = playParticles(svg, "eid");
  run.cancel();
  assert.ok(svg.animations.every((a) => a.cancelled));
  assert.equal(svg.innerHTML, "");
});

test("an unknown design plays nothing and leaves the svg empty", () => {
  const svg = fakeSvg();
  svg.innerHTML = "old";
  const run = playParticles(svg, "nope");
  assert.equal(svg.innerHTML, "");
  assert.equal(svg.animations.length, 0);
  run.cancel(); // a no-op, not a throw
});

// mini-fx.design.json holds, for each of the nine designs, the exact svg children the approved
// design draws (its MINI_PARTICLE table and the markup line of playMiniFx, evaluated with the
// design's own rng(7), r2, STAR and flakeSVG). Each design's markup is pinned whole, so a
// colour, a shape, a position or the draw order cannot drift without this test saying so.
const APPROVED = JSON.parse(
  readFileSync(new URL("./mini-fx.design.json", import.meta.url), "utf8")
);

test("the pinned markup covers exactly the nine designs", () => {
  assert.deepEqual(Object.keys(APPROVED).sort(), [...DESIGNS].sort());
});

for (const design of DESIGNS) {
  test(`${design}: the particles are the approved design's, character for character`, () => {
    assert.equal(particleMarkup(design), APPROVED[design]);
  });
}
