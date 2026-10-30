// The worn art is ported, not designed here, so this only guards the plumbing:
// every design draws, ids are unique per mark, and every reference resolves.
import { test } from "node:test";
import assert from "node:assert/strict";
import { DESIGNS as ANSWER_DESIGNS } from "./answer.mjs";
import {
  DESIGNS,
  LOOK_AT,
  installWornCss,
  wornCss,
  wornMarkup,
} from "./worn.mjs";

const idsOf = (svg) => [...svg.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
const refsOf = (svg) => [...svg.matchAll(/url\(#([^)]+)\)/g)].map((m) => m[1]);

// Built from the code point so this file holds no literal em-dash itself.
const EM_DASH = String.fromCharCode(0x2014);

test("worn.mjs draws exactly the designs the answer reader accepts", () => {
  // DESIGNS here is the art's own key list, so this is what keeps the two lists in step.
  assert.deepEqual([...DESIGNS].sort(), [...ANSWER_DESIGNS].sort());
  for (const design of ANSWER_DESIGNS) {
    assert.notEqual(wornMarkup(design, { idPrefix: "jvh1" }), "", design);
  }
});

test("every design draws something with an item group", () => {
  for (const design of DESIGNS) {
    const svg = wornMarkup(design, { idPrefix: "jvh1" });
    assert.ok(svg.includes('class="acc"'), design);
    assert.ok(!svg.includes("undefined") && !svg.includes("NaN"), design);
  }
});

test("an unknown design, or an inherited key, draws nothing", () => {
  for (const design of ["ramadan", "", "toString", "__proto__", undefined]) {
    assert.equal(wornMarkup(design, { idPrefix: "jvh1" }), "", String(design));
  }
});

test("every url(#id) reference resolves to an id in the same markup", () => {
  for (const design of DESIGNS) {
    const svg = wornMarkup(design, { idPrefix: "jvh7" });
    const ids = new Set(idsOf(svg));
    for (const ref of refsOf(svg)) assert.ok(ids.has(ref), `${design}: ${ref}`);
  }
});

test("two marks never share an id: the idPrefix makes them unique", () => {
  for (const design of DESIGNS) {
    const a = idsOf(wornMarkup(design, { idPrefix: "jvh1" }));
    const b = idsOf(wornMarkup(design, { idPrefix: "jvh2" }));
    assert.ok(a.length > 0, design);
    assert.equal(a.filter((id) => b.includes(id)).length, 0, design);
    assert.equal(
      new Set(a).size,
      a.length,
      `${design}: duplicate id in one mark`
    );
  }
});

test("small marks drop fine detail and still draw", () => {
  for (const design of DESIGNS) {
    const full = wornMarkup(design, { idPrefix: "a" });
    const small = wornMarkup(design, { idPrefix: "a", small: true });
    assert.ok(small.length > 0 && small.length <= full.length, design);
  }
});

test("motion false leaves out the SMIL sway", () => {
  assert.ok(
    wornMarkup("christmas", { idPrefix: "a" }).includes("animateTransform")
  );
  assert.ok(
    !wornMarkup("christmas", { idPrefix: "a", motion: false }).includes(
      "animateTransform"
    )
  );
});

test("the tile clip follows the mark's corner radius", () => {
  assert.match(wornMarkup("holi", { idPrefix: "a", radius: 7 }), /rx="7"/);
});

test("holi draws its cheeks; no other design does", () => {
  for (const design of DESIGNS) {
    assert.equal(
      wornMarkup(design, { idPrefix: "a" }).includes('class="cheeks"'),
      design === "holi"
    );
  }
});

test("the stylesheet has a glance for each LOOK_AT design and a reaction set", () => {
  const css = wornCss();
  for (const design of Object.keys(LOOK_AT)) {
    assert.ok(css.includes(`@keyframes jvh-look-${design} `), design);
    assert.ok(DESIGNS.includes(design), design);
  }
  for (const name of [
    "tip",
    "hop",
    "flare",
    "swing",
    "twirl",
    "wiggle",
    "puff",
    "blowout",
    "smoke",
  ]) {
    assert.ok(css.includes(`@keyframes jvh-${name} `), name);
  }
  assert.ok(css.includes("prefers-reduced-motion"));
});

test("installWornCss adds one style element, however often it is called", () => {
  const added = [];
  const doc = {
    getElementById: (id) => added.find((el) => el.id === id) || null,
    createElement: () => ({}),
    head: { appendChild: (el) => added.push(el) },
  };
  installWornCss(doc);
  installWornCss(doc);
  assert.equal(added.length, 1);
  assert.ok(added[0].textContent.includes("jvh-layer"));
});

test("no em-dash in the module's output", () => {
  const all =
    DESIGNS.map((d) => wornMarkup(d, { idPrefix: "a" })).join("") + wornCss();
  assert.ok(!all.includes(EM_DASH));
});
