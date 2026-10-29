// Photosensitivity: the Diwali scene has at most 3 flashes in any second and no flash
// larger than 25% of its layer. Static: it reads the scene's own flash table (the one the
// animation is driven by and the drawing takes its radii from), then renders the scene's
// markup and checks the circle radii actually drawn, so a radius hard-coded in the art
// cannot drift from the table, and a new flash cannot slip in without this test seeing it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { FLASHES, PEAK, END_MS } from "./diwali.mjs";
import { lightSVG, skyShowSVG } from "./diwali-art.mjs";

const P = "t-";

// The radius of every circle the scene draws for a flash, keyed by flash class.
function drawnRadii() {
  const markup = skyShowSVG(P, 0) + lightSVG(P, 0, 0);
  const drawn = {};
  for (const m of markup.matchAll(/<circle class="([^"]*)" r="([\d.]+)"/g)) {
    for (const f of FLASHES) {
      if (m[1].split(" ").includes(`${P}${f.cls}`)) {
        (drawn[f.cls] ||= []).push(Number(m[2]));
      }
    }
  }
  return drawn;
}

test("the scene has flashes, each inside the scene's length", () => {
  assert.ok(FLASHES.length >= 3);
  for (const f of FLASHES) {
    assert.ok(f.at >= 0 && f.at + f.ms <= END_MS, f.cls);
    assert.ok(f.r > 0 && f.ms > 0, f.cls);
  }
});

test("at most 3 flashes start in any 1 s window", () => {
  for (const start of FLASHES.map((f) => f.at)) {
    const inWindow = FLASHES.filter(
      (f) => f.at >= start && f.at < start + 1000
    );
    assert.ok(
      inWindow.length <= 3,
      `${inWindow.length} flashes from ${start} ms`
    );
  }
});

test("every flash is drawn once, at the radius in the table", () => {
  const drawn = drawnRadii();
  for (const f of FLASHES) {
    assert.deepEqual(drawn[f.cls], [f.r], `${f.cls} drawn radius`);
  }
});

test("no drawn flash covers more than 25% of the layer, at any desktop size", () => {
  // Desktop stage layout starts at 768 px wide; the layer is the welcome area, so it is
  // narrower than the viewport. The scale is min(W / 800, 1.25), as in the scene.
  const drawn = drawnRadii();
  for (let w = 540; w <= 2600; w += 20) {
    for (let h = 300; h <= 1600; h += 20) {
      const s = Math.min(w / 800, 1.25);
      for (const f of FLASHES) {
        const r = drawn[f.cls][0];
        const area = Math.PI * (r * PEAK * s) ** 2;
        assert.ok(area / (w * h) <= 0.25, `${f.cls} at ${w}x${h}`);
      }
    }
  }
});

test("the biggest drawn flash is far below the limit, with room to grow", () => {
  const drawn = drawnRadii();
  const worst =
    Math.max(
      ...FLASHES.map((f) => Math.PI * (drawn[f.cls][0] * PEAK * 1.25) ** 2)
    ) /
    (540 * 300);
  assert.ok(worst < 0.1, `worst case ${worst}`);
});
