// Where a scene's props go on a stage that is not 800 units wide: the desktop's edge rule
// stays as it was, and the phone's 540-unit window squeezes the stage about its centre.
import { test } from "node:test";
import assert from "node:assert/strict";
import { edgeX, squeezeOf, squeezeX } from "./scene-place.mjs";

const stage = (from, to) => ({ from, to, squeeze: squeezeOf(from, to) });

test("a stage 800 units or wider has no squeeze, however the floats fall", () => {
  assert.equal(squeezeOf(0, 800), 1);
  assert.equal(squeezeOf(-130, 930), 1);
  const s = 768 / 800; // 768 px host: from..to is 800 up to rounding
  assert.equal(squeezeOf(400 - 384 / s, 400 + 384 / s), 1);
});

test("the phone window 130..670 squeezes to 0.675", () => {
  assert.ok(Math.abs(squeezeOf(130, 670) - 0.675) < 1e-12);
});

test("on a wide stage squeezeX is exactly the x it was given, decimals too", () => {
  for (const x of [0, 70, 285.6, 400, 730, 800])
    assert.equal(squeezeX(x, stage(-130, 930)), x);
});

test("on the phone window squeezeX keeps the centre and maps the stage's ends to the window's", () => {
  const phone = stage(130, 670);
  assert.equal(squeezeX(400, phone), 400);
  assert.ok(Math.abs(squeezeX(0, phone) - 130) < 1e-9);
  assert.ok(Math.abs(squeezeX(800, phone) - 670) < 1e-9);
  assert.ok(Math.abs(squeezeX(150, phone) - 231.25) < 1e-9);
});

test("edgeX on a wide stage keeps a prop its distance from its own edge, as before", () => {
  const wide = stage(-130, 930);
  assert.equal(edgeX(58, wide), -72);
  assert.equal(edgeX(742, wide), 872);
  assert.equal(edgeX(400, wide), 930 - 400);
  assert.equal(edgeX(100, stage(0, 800)), 100);
});

test("edgeX on the phone window squeezes, so a prop never crosses the centre or leaves the window", () => {
  const phone = stage(130, 670);
  const xs = [0, 58, 150, 330, 399, 516, 650, 742, 800];
  const out = xs.map((x) => edgeX(x, phone));
  assert.deepEqual(
    out,
    [...out].sort((a, b) => a - b),
    "order is kept"
  );
  out.forEach((x) => assert.ok(x >= 130 - 1e-9 && x <= 670 + 1e-9, String(x)));
});
