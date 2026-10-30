// The mini chat's particles are ported from the frozen design (MINI_PARTICLE, playMiniFx).
// These tests pin the numbers that make them look the same on every showing.
import { test } from "node:test";
import assert from "node:assert/strict";
import { DESIGNS } from "./answer.mjs";
import {
  MINI_PARTICLE,
  PARTICLE_COUNT,
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

test("a design draws 24 particles and the same ones every time", () => {
  for (const design of DESIGNS) {
    const markup = particleMarkup(design);
    assert.equal(
      (markup.match(/class="mfx"/g) || []).length,
      PARTICLE_COUNT,
      design
    );
    assert.equal(markup, particleMarkup(design), design);
  }
  assert.equal(PARTICLE_COUNT, 24);
});

test("an unknown design draws nothing", () => {
  assert.equal(particleMarkup("nope"), "");
  assert.equal(particleMarkup(undefined), "");
});

test("christmas drops the shared flake art, birthday its confetti", () => {
  assert.match(particleMarkup("christmas"), /class="mfx-snow"/);
  assert.match(particleMarkup("christmas"), /stroke-width="1.1"/);
  assert.match(particleMarkup("birthday"), /<rect /);
});

test("particles start above the top edge, inside the 350 wide view", () => {
  const groups = [
    ...particleMarkup("diwali").matchAll(
      /<g transform="translate\(([-\d.]+) ([-\d.]+)\)"><g class="mfx"/g
    ),
  ];
  assert.equal(groups.length, 24);
  for (const [, x, y] of groups) {
    assert.ok(x >= 12 && x <= 338, x);
    assert.ok(y <= -12 && y >= -52, y);
  }
});

test("playing runs one 'both'-filled fall per particle with the frozen timings", () => {
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
