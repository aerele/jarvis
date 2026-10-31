import { test } from "node:test";
import assert from "node:assert/strict";
import { LOOK_AT, installWornCss, wornMarkup } from "./worn.mjs";
import { playParticles } from "./mini-fx.mjs";
import { ART_VERSION, installHolidayArt } from "./art.mjs";

test("the global carries the versioned art the Desk widget reads", () => {
  const win = {};
  installHolidayArt(win);
  const art = win.__jarvisHolidayArt;
  assert.equal(art.v, ART_VERSION);
  assert.equal(art.wornMarkup, wornMarkup);
  assert.equal(art.installWornCss, installWornCss);
  assert.equal(art.LOOK_AT, LOOK_AT);
  assert.equal(art.particles, playParticles);
});

test("the art version is the one the widget loader waits for", async () => {
  const { ART_VERSION: expected } = await import(
    "../../jarvis_chat/widget/holiday_art_loader.mjs"
  );
  assert.equal(ART_VERSION, expected);
});

test("the art is the only thing that surfaces; it adds nothing else to the window", () => {
  const win = {};
  installHolidayArt(win);
  assert.deepEqual(Object.keys(win), ["__jarvisHolidayArt"]);
});
