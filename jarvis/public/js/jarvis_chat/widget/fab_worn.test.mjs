import { test } from "node:test";
import assert from "node:assert/strict";
import { FAB_PX, lookStyle, wornLayerBox, isItemShown } from "./fab_worn.mjs";

const base = {
  itemReady: true,
  faceShown: true,
  dragging: false,
  panelOpen: false,
  brandLogo: false,
  maintenance: false,
};

test("the item shows with the face and nothing else in the way", () => {
  assert.equal(isItemShown(base), true);
});

test("the item is off when there is nothing to show or the face is hidden", () => {
  assert.equal(isItemShown({ ...base, itemReady: false }), false);
  assert.equal(isItemShown({ ...base, faceShown: false }), false);
});

test("the item is hidden while dragging and while the panel is open", () => {
  assert.equal(isItemShown({ ...base, dragging: true }), false);
  assert.equal(isItemShown({ ...base, panelOpen: true }), false);
});

test("the item never shows on a brand logo or under the maintenance hold", () => {
  assert.equal(isItemShown({ ...base, brandLogo: true }), false);
  assert.equal(isItemShown({ ...base, maintenance: true }), false);
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

test("the layer box follows the tile it dresses (the panel's 52px hero)", () => {
  const unit = 52 / 56;
  const box = wornLayerBox(52);
  assert.equal(box.left, `${-16 * unit}px`);
  assert.equal(box.width, `${88 * unit}px`);
});
