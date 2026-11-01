// The runtime's failure handling: a scene that throws while starting leaves nothing behind, a
// timer callback or a beat that throws is reported and the scene still reaches rest, and a
// browser that refuses one animation does not stop the rest.
import { mock, test } from "node:test";
import assert from "node:assert/strict";
import { Stage } from "../stage.mjs";
import { Scene, playScene } from "./scene-runtime.mjs";
import { heroKeepOut } from "./scene-place.mjs";
import {
  boxes,
  fakeHost,
  floorTransformOf,
  run,
} from "./day-scene-fakes.test-support.mjs";

const floorScaleOf = (r) => floorTransformOf(r).scale;

const CFG = {
  end: 1000,
  land: 400,
  swap: 450,
  face: 700,
  prep: 300,
  look: [[1, 1, 0.5]],
};

// A scene that draws one group, starts an endless animation and then fails.
class FailsInPlayArt extends Scene {
  constructor(host, opts) {
    super(host, opts, CFG);
  }

  inner() {
    return `<g class="${this.p}x"></g>`;
  }

  playArt() {
    this.stage.animate(this.stage.q(`.${this.p}x`), [{ opacity: 0 }], {
      iterations: Infinity,
    });
    throw new Error("playArt failed");
  }
}

class FailsInPrepare extends FailsInPlayArt {
  playArt() {}

  prepare() {
    throw new Error("prepare failed");
  }
}

// A scene whose flying copy has left and whose animation refuses to be cancelled.
class FlightWontCancel extends FailsInPlayArt {
  inner() {
    return `<g class="${this.p}drop"></g>`;
  }

  playArt() {}

  prepare() {
    this.fly([{ opacity: 0 }, { opacity: 1 }], { duration: 100 });
    this.flight.cancel = () => {
      throw new Error("cancel failed");
    };
  }
}

const mod = (SceneClass) => ({
  END_MS: CFG.end,
  play: (host, opts) => playScene(SceneClass, host, opts),
});

test("a scene that throws after mounting leaves no svg and no running animation", () => {
  mock.timers.reset();
  mock.timers.enable({ apis: ["setTimeout"] });
  const host = fakeHost();
  const opts = { layout: "desktop", getBoxes: () => boxes(), onBeat() {} };
  assert.throws(() => playScene(FailsInPlayArt, host, opts), /playArt failed/);
  assert.equal(host.svgs.length, 1, "it had mounted");
  assert.ok(host.svg.removed, "the svg is gone");
  assert.ok(host.svg.animations.length > 0);
  assert.ok(host.svg.animations.every((a) => a.cancelled));
  mock.timers.reset();
});

test("a throwing timer callback is reported and the scene still reaches rest", async () => {
  const errors = [];
  const r = run(mod(FailsInPrepare), {
    onError: (e) => errors.push(e.message),
  });
  r.tick(CFG.end);
  assert.deepEqual(errors, ["prepare failed"]);
  assert.deepEqual(
    r.beats.find((b) => b[0] === "settle"),
    ["settle", "end"]
  );
  r.tick(700);
  await r.done();
  assert.ok(r.host.svg.removed, "the art is gone");
  r.end();
});

test("an early settle still lands the item and fades out when the flying copy cannot be cancelled", async () => {
  const r = run(mod(FlightWontCancel));
  r.tick(CFG.prep + 10); // the item is in the air
  r.api.settle("early");
  assert.equal(r.count("deliver"), 1, "the item lands");
  r.tick(400);
  await r.done();
  assert.ok(r.host.svg.removed, "the art is gone");
  r.end();
});

test("a page that throws on a beat is reported and the scene still ends", async () => {
  const errors = [];
  const r = run(mod(FailsInPrepare), {
    onError: (e) => errors.push(e.message),
    onBeat: (name) => {
      if (name === "look") throw new Error("beat failed");
    },
  });
  r.tick(CFG.end);
  r.tick(700);
  await r.done();
  assert.ok(errors.includes("beat failed"));
  assert.ok(r.host.svg.removed);
  r.end();
});

test("an onError that throws is ignored", async () => {
  const r = run(mod(FailsInPrepare), {
    onError: () => {
      throw new Error("reporter failed");
    },
  });
  r.tick(CFG.end);
  r.tick(700);
  await r.done();
  assert.ok(r.host.svg.removed);
  r.end();
});

test("Stage.animate gives null when the browser refuses the keyframes, and fadeOut still disposes", async () => {
  const stage = new Stage({ appendChild() {} });
  const refusing = {
    animate() {
      throw new TypeError("bad keyframes");
    },
  };
  assert.equal(stage.animate(refusing, []), null);
  assert.deepEqual(stage.anims, []);
  stage.svg = { style: {}, remove() {}, animate: refusing.animate };
  stage.fadeOut(0);
  await stage.done;
  assert.equal(stage.svg, null);
});

test("the keep-out box follows the hero's own box when the page gives it, and a guess when not", () => {
  const page = boxes();
  const guess = heroKeepOut(page, 1);
  assert.equal(guess.b, page.heading.y + page.heading.h + 44 + 8);
  const hero = { x: 210, y: 100, w: 680, h: 300 };
  const own = heroKeepOut({ ...page, hero }, 1);
  assert.equal(
    own.b,
    400 + 8,
    "the hero's bottom, not a guess below the heading"
  );
  assert.equal(own.l, 400 - 340 - 8);
  assert.equal(own.r, 400 + 340 + 8);
  const scaled = heroKeepOut({ ...page, hero }, 2);
  assert.equal(scaled.b, 200 + 8);
});

// A scene with a floor row and one prop held at its edge (Scene.edge).
class HoldsAProp extends Scene {
  constructor(host, opts) {
    super(host, opts, {
      ...CFG,
      row: { up: 52, rise: 0, mid: null, base: 0, g: 1 },
    });
  }

  inner() {
    return this.floorGroup(this.edge(150, `<g class="${this.p}prop"></g>`));
  }
}

const heldTransform = (r) =>
  r.host.svg.querySelector(".t-edge").getAttribute("transform");

test("a held prop on a roomy stage has exactly the identity wrapper, translate(0 0)", () => {
  const r = run(mod(HoldsAProp));
  assert.equal(floorScaleOf(r), 1, "roomy: the row is at full size");
  assert.equal(heldTransform(r), "translate(0 0)", "not -0, not a stray shift");
  r.api.settle("instant");
  r.end();
});

test("a held prop moves back out as the floor row shrinks, and returns to the identity", () => {
  const r = run(mod(HoldsAProp), {
    boxes: boxes({ h: 480, cardsH: 100, bottomGap: -40 }),
  });
  const scale = floorScaleOf(r);
  assert.ok(scale < 1, `the row shrank to ${scale}`);
  const [, shift] = /translate\(([-\d.]+) 0\)/.exec(heldTransform(r));
  // The prop is at x = 150, 250 units left of the row's centre: held at x, so moved out.
  assert.equal(Number(shift), Math.round(-250 * (1 / scale - 1) * 100) / 100);
  r.api.relayout(boxes());
  assert.equal(heldTransform(r), "translate(0 0)");
  r.api.settle("instant");
  r.end();
});
