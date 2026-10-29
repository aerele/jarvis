// A regression net for all nine day scenes: each scene's mounted markup, the animations it
// starts (what, keyframes, options, and the absolute time each begins) and its beats, taken
// at a few moments of a light-theme run, are hashed and compared with the table below. A
// refactor that keeps this table passing draws and times every scene exactly as before. When
// a scene is changed ON PURPOSE, run with SCENE_SNAPSHOT_DIR=<dir> to dump each run as JSON
// (diff it against the dump from before the change), then paste the new hashes the failure
// message prints.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import * as birthday from "./birthday.mjs";
import * as christmas from "./christmas.mjs";
import * as diwali from "./diwali.mjs";
import * as eid from "./eid.mjs";
import * as holi from "./holi.mjs";
import * as independence from "./independence.mjs";
import * as newyear from "./newyear.mjs";
import * as onam from "./onam.mjs";
import * as pongal from "./pongal.mjs";
import { boxes, run } from "./day-scene-fakes.test-support.mjs";

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

// A roomy stage, a short one (the floor row shrinks) and a replay (no floor, no swap).
const RUNS = {
  roomy: () => ({}),
  short: () => ({ boxes: boxes({ h: 470, cardsH: 120, bottomGap: -40 }) }),
  replay: () => ({ replay: true }),
};

const MOMENTS = [0, 600, 1800, 2600, 3500, 4300, 5500];

const describeNode = (n) => `${n.tag}.${n.attrs.class || ""}`;

/** One run of a scene, stepped 1 ms at a time so each animation is stamped with its start. */
function capture(mod, over) {
  const r = run(mod, over);
  const moments = [];
  const stamps = new Map();
  const seen = () => {
    for (const a of r.host.svg.animations) if (!stamps.has(a)) stamps.set(a, t);
  };
  const stops = [...MOMENTS, mod.END_MS - 1];
  let t = 0;
  seen();
  for (const stop of stops) {
    for (; t < stop; ) {
      r.tick(1);
      t += 1;
      seen();
    }
    moments.push([t, r.markup()]);
  }
  const animations = r.host.svg.animations
    .map((a) =>
      JSON.stringify({
        node: describeNode(a.node),
        frames: a.frames,
        options: { ...a.options, delay: undefined },
        start: stamps.get(a) + (a.options.delay || 0),
      })
    )
    .sort();
  const out = { moments, animations, beats: r.beats };
  r.api.settle("instant");
  r.end();
  return out;
}

const dir = process.env.SCENE_SNAPSHOT_DIR;
const hashOf = (text) =>
  createHash("sha256").update(text).digest("hex").slice(0, 16);

const EXPECTED = {
  "birthday/replay": "44608047a13757c2",
  "birthday/roomy": "579c60eb0cb24d64",
  "birthday/short": "97cfbdab1ca83fb6",
  "christmas/replay": "0d359ca3e84de087",
  "christmas/roomy": "4f5efbb2b18370de",
  "christmas/short": "c66083e64ef31b28",
  "diwali/replay": "c206fdccd7ffed5a",
  "diwali/roomy": "bc8ac6c09a46fe59",
  "diwali/short": "277dc54916e65623",
  "eid/replay": "0100c076cea273b3",
  "eid/roomy": "e00b760d02cb212d",
  "eid/short": "d629bc6d638ab591",
  "holi/replay": "756910b7b46e0cf2",
  "holi/roomy": "49e33ea079b4a1b3",
  "holi/short": "4b44f2aa3fab257e",
  "independence/replay": "7fbdce855d9e9f78",
  "independence/roomy": "eaa5d9c2028cad75",
  "independence/short": "2ca59fe12ba042f9",
  "newyear/replay": "308e5a56db1637e5",
  "newyear/roomy": "727a08d65f659e1a",
  "newyear/short": "1a0a921e28293673",
  "onam/replay": "f6f659edb8e53d8b",
  "onam/roomy": "eed184c6b4e0e3a1",
  "onam/short": "7680bf5590382010",
  "pongal/replay": "409ce8ce56ad1110",
  "pongal/roomy": "90423e7a93af4bab",
  "pongal/short": "01e2d607d8a18099",
};

for (const [design, mod] of Object.entries(SCENES)) {
  for (const [name, over] of Object.entries(RUNS)) {
    const key = `${design}/${name}`;
    test(`${key}: markup, animations and beats are as recorded`, () => {
      const text = JSON.stringify(capture(mod, over()), null, 1);
      if (dir) {
        mkdirSync(dir, { recursive: true });
        writeFileSync(`${dir}/${design}-${name}.json`, text);
      }
      assert.equal(hashOf(text), EXPECTED[key], `${key} hash`);
    });
  }
}
