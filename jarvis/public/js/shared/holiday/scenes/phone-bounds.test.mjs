// The phone layout holds every piece of all nine scenes inside the phone window (units 130..670
// of the stage) at three phone sizes, keeps the props that stand beside the hero clear of it, and
// keeps the props that carry the meaning of a day (the cannons and the hat's launch, the moon,
// the tree, the lamps, ...). What may be left out is listed with the reason.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as birthday from "./birthday.mjs";
import * as christmas from "./christmas.mjs";
import * as diwali from "./diwali.mjs";
import * as eid from "./eid.mjs";
import * as holi from "./holi.mjs";
import * as independence from "./independence.mjs";
import * as newyear from "./newyear.mjs";
import * as onam from "./onam.mjs";
import * as pongal from "./pongal.mjs";
import { PHONE } from "../scene-phone.mjs";
import { run } from "./day-scene-fakes.test-support.mjs";
import { phoneBoxes } from "./phone-boxes.test-support.mjs";
import { shapesUnder } from "./scene-bounds.test-support.mjs";

const SCENES = {
  birthday,
  christmas,
  diwali,
  eid,
  holi,
  independence,
  newyear,
  onam,
  pongal,
};

const PHONES = {
  "390x844": phoneBoxes({ w: 390, h: 844 }),
  "375x667": phoneBoxes({ w: 375, h: 667 }),
  "360x640": phoneBoxes({ w: 360, h: 640, starters: false }),
};

// Pieces that are not a place: they fly, fall, burst or span the layer on purpose, or they are
// pinned to the mark. Strings run off the bottom of the stage on purpose (kites, balloons: the
// stroke colour in isTransient). Each class has its reason here, so a new one cannot slip in
// unexplained.
const TRANSIENT = new Map([
  ["sleigh", "Santa's sleigh crosses the sky"],
  ["trail", "the sleigh's glitter trail, behind it"],
  ["drop", "the hat or rosette falling onto the mark"],
  ["pin", "sparkles pinned to the mark"],
  ["flake", "snow, falling"],
  ["bconf", "birthday confetti, falling"],
  ["pet", "Onam petals, falling"],
  ["tb", "Independence confetti, falling"],
  ["fount", "Diwali fountain sparks, rising and falling"],
  ["carry", "the Diwali spark that drifts down to the mark"],
  ["ball", "the Holi colour ball, thrown across"],
  ["toran", "the toran spans the layer on purpose"],
  ["b1", "Diwali firework burst 1"],
  ["b2", "Diwali firework burst 2"],
  ["b3", "Diwali firework burst 3"],
  ["nb0", "New Year burst 0"],
  ["nb1", "New Year burst 1"],
  ["nb2", "New Year burst 2"],
  ["nb3", "New Year burst 3"],
  ["rk1", "Diwali rocket 1, flying up"],
  ["rk2", "Diwali rocket 2, flying up"],
  ["rk3", "Diwali rocket 3, flying up"],
  ["nrk0", "New Year rocket 0, flying up"],
  ["nrk1", "New Year rocket 1, flying up"],
  ["nrk2", "New Year rocket 2, flying up"],
  ["nrk3", "New Year rocket 3, flying up"],
  ["cl", "the left cannon's spray"],
  ["cr", "the right cannon's spray"],
]);
const isTransient = (n) =>
  n.classes.some((c) => TRANSIENT.has(c.slice(2))) ||
  n.attrs.stroke === "#94a3b8";

// What stands beside the hero (and so has to be clear of its box), by class. The sun, the moon
// and the fireworks are sky behind the greeting by design; the floor row's middle stands under
// the hero, kept off it by the floor fit, so it is not listed.
const BESIDE_HERO = {
  birthday: ["bal"],
  christmas: ["edge"],
  diwali: [],
  eid: ["lan"],
  holi: ["splat"],
  independence: ["kite"],
  newyear: ["edge"],
  onam: ["edge"],
  pongal: ["edge"],
};

const TOL = 0.5;

function play(mod, page) {
  const r = run(mod, { layout: "phone", boxes: page });
  r.tick(mod.END_MS - 1);
  const main = r.host.svg.querySelector(".t-main");
  const s = page.host.w / PHONE.units;
  const cx = page.mark.x + page.mark.w / 2;
  const hero = {
    l: 400 + (page.hero.x - cx) / s,
    r: 400 + (page.hero.x + page.hero.w - cx) / s,
    t: page.hero.y / s,
    b: (page.hero.y + page.hero.h) / s,
  };
  return { r, main, hero, shapes: shapesUnder(main, isTransient) };
}

const overlaps = (b, h) =>
  b[0] < h.r - TOL && b[2] > h.l + TOL && b[1] < h.b - TOL && b[3] > h.t + TOL;

for (const [design, mod] of Object.entries(SCENES))
  for (const [name, page] of Object.entries(PHONES)) {
    test(`${design} at ${name}: every piece is inside the phone window, and what stands beside the hero is clear of it`, () => {
      const { r, hero, shapes } = play(mod, page);
      assert.ok(shapes.length > 0, "the scene drew something");
      for (const { box, chain, node } of shapes)
        assert.ok(
          box[0] >= PHONE.from - TOL && box[2] <= PHONE.to + TOL,
          `${node.tag}.${chain.join(".")} spans x ${box[0].toFixed(
            1
          )}..${box[2].toFixed(1)}`
        );
      const beside = BESIDE_HERO[design];
      for (const { box, chain, node } of shapes)
        if (chain.some((c) => beside.includes(c.slice(2))))
          assert.ok(
            !overlaps(box, hero),
            `${node.tag}.${chain.join(".")} is over the hero`
          );
      r.api.settle("instant");
      r.end();
    });
  }

// ---- what a day cannot do without is moved, never dropped ------------------------------------

const count = (r, sel) => r.host.svg.querySelectorAll(sel).length;

// What each scene must still show on a phone, whatever else is left out for want of room.
const MEANING = {
  christmas: (r) =>
    assert.equal(count(r, ".t-edge"), 2, "the tree and the gifts"),
  eid: (r) => {
    assert.equal(count(r, ".t-moon"), 1, "the crescent moon");
    assert.equal(
      count(r, ".t-lan0") + count(r, ".t-lan3"),
      2,
      "the two low lanterns"
    );
  },
  newyear: (r) => assert.equal(count(r, ".t-edge"), 2, "both cannons"),
  onam: (r) => assert.equal(count(r, ".t-edge"), 2, "both lamps"),
  pongal: (r) => assert.equal(count(r, ".t-edge"), 4, "the four canes"),
  diwali: (r) =>
    assert.equal(
      count(r, ".t-b2"),
      40,
      "the second burst, the spark leaves it"
    ),
  independence: (r) => assert.equal(count(r, ".t-drop"), 1, "the rosette"),
  birthday: (r) =>
    assert.ok(count(r, ".t-bal") >= 2, "at least the outer pair of balloons"),
  holi: (r) =>
    assert.ok(count(r, ".t-splat") >= 4, "splats still colour the edges"),
};

for (const [design, mod] of Object.entries(SCENES))
  for (const [name, page] of Object.entries(PHONES))
    test(`${design} at ${name}: the props that carry the day are all there`, () => {
      const { r } = play(mod, page);
      MEANING[design](r);
      r.api.settle("instant");
      r.end();
    });

test("the New Year hat leaves the right cannon, and the cannon is inside the window", () => {
  for (const page of Object.values(PHONES)) {
    const { r, main } = play(newyear, page);
    const s = page.host.w / PHONE.units;
    const k = page.mark.w / s / 56;
    const hat = r.host.svg.animations.find((a) =>
      a.node.classes.includes("t-drop")
    );
    const [tx, ty] = hat.frames[0].transform
      .match(/translate\(([\d.-]+)px, ([\d.-]+)px\)/)
      .slice(1)
      .map(Number);
    const [x, y] = [400 + (tx - 28) * k, page.mark.y / s + ty * k];
    const right = shapesUnder(main, isTransient)
      .filter(({ chain }) => chain.includes("t-edge"))
      .at(-1);
    assert.ok(x >= PHONE.from && x <= PHONE.to, `the muzzle x ${x}`);
    assert.ok(
      Math.abs(x - right.box[2]) < 40 && y > page.hero.y / s,
      "from the right cannon"
    );
    r.api.settle("instant");
    r.end();
  }
});
