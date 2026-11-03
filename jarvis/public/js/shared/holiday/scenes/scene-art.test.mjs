// The sky wash every day scene shares: its light gradient, and the dark-theme one the four
// pastel scenes swap in (pastel colours at a low alpha over the dark page go grey and muddy).
import { test } from "node:test";
import assert from "node:assert/strict";
import { skyWashSVG } from "./scene-art.mjs";
import * as birthday from "./birthday-art.mjs";
import * as holi from "./holi-art.mjs";
import * as independence from "./independence-art.mjs";
import * as pongal from "./pongal-art.mjs";

const LIGHT = [
  [0, "#fbcfe8", 0.45],
  [0.52, "#fdf2f8", 0],
];
const DARK = [
  [0, "#f472b6", 0.34],
  [0.52, "#8b5cf6", 0],
];

test("a wash with no dark stops draws one gradient and no dark rule", () => {
  const svg = skyWashSVG("t-", LIGHT, 800, 300);
  assert.equal(svg.match(/<linearGradient id="t-sky"/g).length, 1);
  assert.ok(!svg.includes("t-skyd"));
  assert.ok(!svg.includes("data-theme"));
});

test("a wash with dark stops adds a second gradient that the dark theme fills the sky with", () => {
  const svg = skyWashSVG("t-", LIGHT, 800, 300, DARK);
  assert.ok(svg.includes('<linearGradient id="t-skyd"'));
  assert.ok(svg.includes("#f472b6") && svg.includes("#fbcfe8"));
  assert.ok(
    svg.includes('[data-theme="dark"] .t-sky{fill:url(#t-skyd)}'),
    "the dark rule"
  );
  // The light gradient is still the rect's own fill.
  assert.ok(svg.includes('fill="url(#t-sky)"'));
});

const hsl = (hex) => {
  const [r, g, b] = [1, 3, 5].map(
    (i) => parseInt(hex.slice(i, i + 2), 16) / 255
  );
  const [hi, lo] = [Math.max(r, g, b), Math.min(r, g, b)];
  const l = (hi + lo) / 2;
  const s = hi === lo ? 0 : (hi - lo) / (1 - Math.abs(2 * l - 1));
  return { s, l };
};

for (const [name, art] of Object.entries({
  birthday,
  holi,
  independence,
  pongal,
})) {
  test(`${name}: the dark sky has the light sky's offsets, fades out, and is saturated, not pastel`, () => {
    const { SKY_STOPS: light, SKY_STOPS_DARK: dark } = art;
    assert.ok(Array.isArray(dark), "has dark stops");
    assert.deepEqual(
      dark.map((s) => s[0]),
      light.map((s) => s[0])
    );
    assert.equal(dark[dark.length - 1][2], 0, "ends transparent");
    for (const [, colour, alpha = 1] of dark) {
      assert.ok(alpha <= 0.65, `${colour} alpha ${alpha}`);
      const { s, l } = hsl(colour);
      assert.ok(s >= 0.55, `${colour} saturation ${s}`);
      assert.ok(l <= 0.72, `${colour} lightness ${l}`);
    }
    // The light sky's pastel stops are exactly what goes grey on the dark page.
    assert.ok(light.some(([, c]) => hsl(c).l > 0.8));
  });
}
