import { test } from "node:test";
import assert from "node:assert/strict";
import { FAB_PX, lookStyle, wornLayerBox, wornShown } from "./fab_worn.mjs";

const base = {
  wear: true,
  faceShown: true,
  dragging: false,
  panelOpen: false,
  brandLogo: false,
  maintenance: false,
};

test("the item shows with the face and nothing else in the way", () => {
  assert.equal(wornShown(base), true);
});

test("the item is off when there is nothing to wear or the face is hidden", () => {
  assert.equal(wornShown({ ...base, wear: false }), false);
  assert.equal(wornShown({ ...base, faceShown: false }), false);
});

test("the item is hidden while dragging and while the panel is open", () => {
  assert.equal(wornShown({ ...base, dragging: true }), false);
  assert.equal(wornShown({ ...base, panelOpen: true }), false);
});

test("the item never shows on a brand logo or under the maintenance hold", () => {
  assert.equal(wornShown({ ...base, brandLogo: true }), false);
  assert.equal(wornShown({ ...base, maintenance: true }), false);
});

test("the layer is the FAB's box with the art's 16 unit margin on every side", () => {
  const unit = FAB_PX / 56;
  const box = wornLayerBox();
  assert.equal(box.left, `${-16 * unit}px`);
  assert.equal(box.top, `${-16 * unit}px`);
  assert.equal(box.width, `${88 * unit}px`);
  assert.equal(box.height, `${88 * unit}px`);
});

test("the eyes look at the item: tile units become pixels of the face", () => {
  const style = lookStyle({ diwali: [9, 3] }, "diwali");
  const unit = FAB_PX / 56;
  assert.equal(style["--jvw-lx"], `${9 * unit}px`);
  assert.equal(style["--jvw-ly"], `${3 * unit}px`);
  assert.equal(style["--jvw-look"], "jvh-look-diwali");
});

test("a design with no look entry (holi) keeps the default glance", () => {
  assert.deepEqual(lookStyle({ diwali: [9, 3] }, "holi"), {});
  assert.deepEqual(lookStyle(undefined, "diwali"), {});
  assert.deepEqual(lookStyle({}, "toString"), {});
});
