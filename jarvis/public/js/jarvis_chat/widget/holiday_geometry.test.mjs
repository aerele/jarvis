// The numbers the item's layout is computed from are written twice: here in JS (so the room
// above the hero and the art's corner radius are known before any art loads) and in the
// components' CSS. This test reads the CSS and holds the two to each other.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { FAB_SIZE } from "./fab_position.mjs";
import {
  FAB_PX,
  FAB_RADIUS_PX,
  MARGIN_UNITS,
  wornLayerBox,
} from "./fab_worn.mjs";
import {
  HERO_PX,
  HERO_RADIUS_PX,
  WELCOME_PAD_TOP_PX,
} from "./panel_welcome.mjs";
import { TILE_UNITS, tileUnits } from "../../shared/holiday/worn-room.mjs";

const read = (name) =>
  readFileSync(new URL(`./${name}`, import.meta.url), "utf8");

// The declarations of the first rule that starts with exactly this selector.
function ruleBody(text, selector) {
  const start = text.search(
    new RegExp(`^${selector.replace(/\./g, "\\.")}\\s*[,{]`, "m")
  );
  assert.notEqual(start, -1, `no rule for ${selector}`);
  return text.slice(text.indexOf("{", start), text.indexOf("}", start));
}

const pxOf = (body, property) => {
  const found = new RegExp(`(?:^|[\\s;{])${property}:\\s*(\\d+)px`).exec(body);
  assert.ok(found, `no ${property} in ${body}`);
  return Number(found[1]);
};

test("the hero's size, radius and top padding match Panel.vue's CSS", () => {
  const panel = read("Panel.vue");
  const hero = ruleBody(panel, ".jvp-hero");
  assert.equal(pxOf(hero, "width"), HERO_PX);
  assert.equal(pxOf(hero, "height"), HERO_PX);
  assert.equal(pxOf(hero, "border-radius"), HERO_RADIUS_PX);
  assert.equal(pxOf(hero, "--sz"), HERO_PX);
  const welcome = ruleBody(panel, ".jvp-welcome");
  assert.equal(pxOf(welcome, "padding"), WELCOME_PAD_TOP_PX);
});

test("the launcher's size and radius match the CSS of Widget.vue and FabWorn.vue", () => {
  const fab = ruleBody(read("Widget.vue"), ".jvw-fab");
  assert.equal(pxOf(fab, "width"), FAB_PX);
  assert.equal(pxOf(fab, "height"), FAB_PX);
  assert.equal(pxOf(fab, "border-radius"), FAB_RADIUS_PX);
  const layer = ruleBody(read("FabWorn.vue"), ".jvw-worn");
  assert.equal(pxOf(layer, "width"), FAB_PX);
  assert.equal(pxOf(layer, "height"), FAB_PX);
  assert.equal(FAB_SIZE, FAB_PX);
});

test("the item's svg viewBox is the tile plus its margin on every side", () => {
  const side = TILE_UNITS + 2 * MARGIN_UNITS;
  const viewBox = `viewBox="${-MARGIN_UNITS} ${-MARGIN_UNITS} ${side} ${side}"`;
  assert.ok(read("FabWorn.vue").includes(viewBox));
  assert.ok(read("WelcomeHero.vue").includes(viewBox));
});

test("the layer box is the viewBox scaled to the tile", () => {
  const box = wornLayerBox(FAB_PX);
  assert.equal(
    box.width,
    `${((TILE_UNITS + 2 * MARGIN_UNITS) * FAB_PX) / TILE_UNITS}px`
  );
  assert.equal(box.left, `${(-MARGIN_UNITS * FAB_PX) / TILE_UNITS}px`);
});

test("a radius in px becomes the same radius in the art's 56 unit tile", () => {
  assert.equal(TILE_UNITS, 56);
  assert.equal(tileUnits(HERO_RADIUS_PX, HERO_PX), (14 / 52) * 56);
  assert.equal(tileUnits(FAB_RADIUS_PX, FAB_PX), (16 / 54) * 56);
  assert.equal(tileUnits(28, 56), 28);
});
