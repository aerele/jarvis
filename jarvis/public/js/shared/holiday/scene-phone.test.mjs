// The phone stage's own numbers, as the page needs them before the art loads: the window the
// scene is drawn in, its scale, and the rule that sends a screen with no room to rest.
import { test } from "node:test";
import assert from "node:assert/strict";
import { MIN_FLOOR_SCALE, sceneHeroTop } from "./scene-geometry.mjs";
import {
  PHONE,
  phoneFloorMinScale,
  phoneScale,
  phoneSceneFits,
} from "./scene-phone.mjs";

test("the phone window is 540 units centred on the mark's column", () => {
  assert.deepEqual([PHONE.from, PHONE.to, PHONE.units], [130, 670, 540]);
  assert.equal(400 - PHONE.from, PHONE.to - 400);
});

test("a unit is width / 540 px, never more than the desktop's cap", () => {
  assert.equal(phoneScale(540), 1);
  assert.ok(Math.abs(phoneScale(390) - 390 / 540) < 1e-12);
  assert.equal(phoneScale(2000), 1.25);
});

const roomy = { design: "christmas", W: 390, H: 844, markTopPx: 140 };

test("a portrait phone with room for the scene's top decoration plays it", () => {
  assert.equal(phoneSceneFits(roomy), true);
});

test("landscape rests, and so does a square screen", () => {
  assert.equal(phoneSceneFits({ ...roomy, W: 844, H: 390 }), false);
  assert.equal(phoneSceneFits({ ...roomy, W: 600, H: 600 }), false);
});

test("the mark's top must clear the scene's need at the phone scale, to the pixel", () => {
  const need = sceneHeroTop("christmas", phoneScale(390));
  assert.equal(phoneSceneFits({ ...roomy, markTopPx: need }), true);
  assert.equal(phoneSceneFits({ ...roomy, markTopPx: need - 0.01 }), false);
});

test("the worn item's top counts too, when it is higher than the scene's need", () => {
  assert.equal(phoneSceneFits({ ...roomy, wornTopPx: 150 }), false);
  assert.equal(phoneSceneFits({ ...roomy, wornTopPx: 140 }), true);
});

test("a design with no top decoration (Holi) needs no room", () => {
  assert.equal(
    phoneSceneFits({ ...roomy, design: "holi", markTopPx: 0 }),
    true
  );
});

test("a size that is not a number rests", () => {
  for (const bad of [
    { W: NaN },
    { H: undefined },
    { markTopPx: NaN },
    { W: 0 },
  ])
    assert.equal(phoneSceneFits({ ...roomy, ...bad }), false);
});

test("the floor row keeps 0.5 px a unit on a phone, and the desktop's floor on a big one", () => {
  assert.equal(phoneFloorMinScale(390 / 540) * (390 / 540), PHONE.floorMinPx);
  assert.ok(phoneFloorMinScale(0.667) > MIN_FLOOR_SCALE);
  assert.equal(phoneFloorMinScale(1.25), MIN_FLOOR_SCALE);
});
