// A regression net for all nine day scenes: each scene's mounted markup, the animations it
// starts (what, keyframes, options, and the absolute time each begins) and its beats, taken
// at a few moments of a run, are hashed field by field and compared with the table below, so a
// failure names the design, the run and the field (markup at one moment, animations, beats)
// that changed. A refactor that keeps this table passing draws and times every scene exactly
// as before. When a scene is changed ON PURPOSE, run with SCENE_SNAPSHOT_DIR=<dir> to dump each
// run as JSON (diff it against the dump from before the change), then paste the row the
// failure message prints.
//
// The scenes do not read the theme: the dark theme is CSS inside the markup, so the markup
// hashes already cover it, and the second half of this file reads those dark rules out by name.
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
import { phoneBoxes } from "./phone-boxes.test-support.mjs";

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

// A roomy stage, a short one (the floor row shrinks), a replay (no floor, no swap), and the
// stage widths either side of 800 units: 768 px (s < 1, the host narrower than the stage) and
// 2560 px (s capped at 1.25, the host wider). The phone path must leave all five as recorded.
const RUNS = {
  roomy: () => ({}),
  short: () => ({ boxes: boxes({ h: 470, cardsH: 120, bottomGap: -40 }) }),
  replay: () => ({ replay: true }),
  narrow: () => ({ boxes: boxes({ w: 768 }) }),
  wide: () => ({ boxes: boxes({ w: 2560 }) }),
  // The phone layout at three phone sizes (the last has no starter cards). The theme is CSS
  // inside the markup, so there is no light / dark split.
  "phone-390x844": () => ({ layout: "phone", boxes: phoneBoxes() }),
  "phone-375x667": () => ({
    layout: "phone",
    boxes: phoneBoxes({ w: 375, h: 667 }),
  }),
  "phone-360x640": () => ({
    layout: "phone",
    boxes: phoneBoxes({ w: 360, h: 640, starters: false }),
  }),
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
const hashOf = (value) =>
  createHash("sha256").update(JSON.stringify(value)).digest("hex").slice(0, 8);

// A run's parts, each hashed on its own: the markup (at the first moment, and at each later
// moment where it has changed since the one before), the animations, the beats.
const fieldsOf = ({ moments, animations, beats }) => ({
  ...Object.fromEntries(
    moments
      .filter(([, markup], i) => i === 0 || markup !== moments[i - 1][1])
      .map(([t, markup]) => [`markup at ${t} ms`, markup])
  ),
  animations,
  beats,
});

// One row per run: the hashes of its fields, in the order of fieldsOf.
const EXPECTED = {
  "birthday/narrow": "52f91131 c2afe321 e0f40c7e",
  "birthday/phone-360x640": "53069af1 e9bccdb2 e0f40c7e",
  "birthday/phone-375x667": "9e49ff49 32d7f991 e0f40c7e",
  "birthday/phone-390x844": "08494043 1f7afe8b e0f40c7e",
  "birthday/replay": "f63991a6 6003e96e fa727a46",
  "birthday/roomy": "f63991a6 6003e96e e0f40c7e",
  "birthday/short": "f63991a6 c0112047 e0f40c7e",
  "birthday/wide": "c969428d 6003e96e e0f40c7e",
  "christmas/narrow": "b5d84a10 2eaeb61c e947be32 da298c33",
  "christmas/phone-360x640": "36a00ecf 8714b322 f6b870f7 da298c33",
  "christmas/phone-375x667": "9228fe81 29bbb4bf 2ba25959 da298c33",
  "christmas/phone-390x844": "7db3e063 de139c49 9327bfb7 da298c33",
  "christmas/replay": "3413110b 869009ee 44c3ceb6 d72f8430",
  "christmas/roomy": "91301ba4 fc276ab4 e2ba2750 da298c33",
  "christmas/short": "c04d4461 d9cb11f1 4c62bc72 da298c33",
  "christmas/wide": "240fab82 b983e2f4 6a3852d5 da298c33",
  "diwali/narrow": "71f1fc2c 6ff2b242 30a4d641 1edb8737",
  "diwali/phone-360x640": "4a083fe7 a310d6be 1b2b805c 1edb8737",
  "diwali/phone-375x667": "c0a9acf7 acc2b3d0 e084bfd2 1edb8737",
  "diwali/phone-390x844": "a13db1a4 e7549030 12f28517 1edb8737",
  "diwali/replay": "d75555e8 f24ff791 b8b16b58 2d5ca61e",
  "diwali/roomy": "d6b5a5d5 a49074be 2a83e870 1edb8737",
  "diwali/short": "87c7f4cb 17ae24d7 ba3b4f94 1edb8737",
  "diwali/wide": "108108a6 d33d94d0 2a83e870 1edb8737",
  "eid/narrow": "a8a19966 c7a37e9f 4240175c",
  "eid/phone-360x640": "00b3fd11 3ec8cce6 4240175c",
  "eid/phone-375x667": "17af7b91 24d4a6c8 4240175c",
  "eid/phone-390x844": "612d5ce4 6ed897ef 4240175c",
  "eid/replay": "d3d5e4e3 32c05b87 7e9d52c7",
  "eid/roomy": "1d5f2d09 0c0c7688 4240175c",
  "eid/short": "d626b78f 457944e2 4240175c",
  "eid/wide": "bafa9b86 0c0c7688 4240175c",
  "holi/narrow": "424cdfae f3c30525 b8e1561c 9b86db1d",
  "holi/phone-360x640": "8ef504ce 13c3d010 bd1d576b 9b86db1d",
  "holi/phone-375x667": "e5ce7833 89d21609 21c6d749 9b86db1d",
  "holi/phone-390x844": "f5790ac8 2d0e1337 cb6261b3 9b86db1d",
  "holi/replay": "5ef3d821 1114e533 cbe79e2b d7005a01",
  "holi/roomy": "fafb4610 8be04f13 6dedc4e6 9b86db1d",
  "holi/short": "63a3aa22 ea04ccad dc3c3601 9b86db1d",
  "holi/wide": "1fb7da1d 8657987f ee98487c 9b86db1d",
  "independence/narrow": "0c269e5e e5e5f039 b3e7b8ac 7a2d15e0",
  "independence/phone-360x640": "9cc0d56c 802c6923 29e66368 7a2d15e0",
  "independence/phone-375x667": "10dae3b0 9db3b30e c2c2c94e 7a2d15e0",
  "independence/phone-390x844": "61726aad 585be82d c69d8980 7a2d15e0",
  "independence/replay": "c56dfaa7 68ae810c 385ef482 571065cf",
  "independence/roomy": "c56dfaa7 68ae810c 385ef482 7a2d15e0",
  "independence/short": "f08bd65f 3293dffe 3c4e1ec2 7a2d15e0",
  "independence/wide": "cf2d49e6 b6f69dd9 502eb64f 7a2d15e0",
  "newyear/narrow": "f880c730 64a24d89 2aea645f",
  "newyear/phone-360x640": "9297c70e 5f07b4eb 2aea645f",
  "newyear/phone-375x667": "96558a8a 707454bb 2aea645f",
  "newyear/phone-390x844": "7bd71925 770dfe35 2aea645f",
  "newyear/replay": "5c4fb432 6910fc9d 53a8f53c",
  "newyear/roomy": "87a86316 7bd942d3 2aea645f",
  "newyear/short": "023ff55b 14452d90 2aea645f",
  "newyear/wide": "0dc15620 7bd942d3 2aea645f",
  "onam/narrow": "939f5853 b82f0efb d65e7a10 e2348697",
  "onam/phone-360x640": "0693c9b9 5b8a19cf 361b09c5 e2348697",
  "onam/phone-375x667": "65adec48 4b26bbed ab6edb35 e2348697",
  "onam/phone-390x844": "dab64bb0 287ab9ac 08217c0c e2348697",
  "onam/replay": "84e044ba 5d9144b2 e6e2cb15 b2ee7c82",
  "onam/roomy": "bc934b95 d32162d9 d17c720f e2348697",
  "onam/short": "5ead1cb5 57235fce 6b83a5c8 e2348697",
  "onam/wide": "f374ba9a 164862df d17c720f e2348697",
  "pongal/narrow": "3312d6b4 a832fe56 a2c327f0 eab288f9",
  "pongal/phone-360x640": "e8cfb262 73a48ebc dc268e92 eab288f9",
  "pongal/phone-375x667": "326206fd 4f9f76f2 6702fbaa eab288f9",
  "pongal/phone-390x844": "10212189 78e8c786 d4877819 eab288f9",
  "pongal/replay": "c23ad67d 93d83f04 6b3f5ef5 b3c549d2",
  "pongal/roomy": "9df76d48 71b13b41 4d0784a0 eab288f9",
  "pongal/short": "c0ca7506 9b7ecde3 4d0784a0 eab288f9",
  "pongal/wide": "2c3ec6e5 1a183b54 4d0784a0 eab288f9",
};

for (const [design, mod] of Object.entries(SCENES)) {
  for (const [name, over] of Object.entries(RUNS)) {
    const key = `${design}/${name}`;
    test(`${key}: markup, animations and beats are as recorded`, () => {
      const run = capture(mod, over());
      if (dir) {
        mkdirSync(dir, { recursive: true });
        writeFileSync(
          `${dir}/${design}-${name}.json`,
          JSON.stringify(run, null, 1)
        );
      }
      const fields = fieldsOf(run);
      const names = Object.keys(fields);
      const actual = Object.values(fields).map(hashOf);
      const expected = (EXPECTED[key] || "").split(" ");
      const differs = names.filter((_, i) => actual[i] !== expected[i]);
      assert.ok(
        differs.length === 0 && actual.length === expected.length,
        `${key}: ${
          differs.join("; ") || "the field list"
        } differ(s). New row: "${key}": "${actual.join(" ")}",`
      );
    });
  }
}

// ---- the dark theme's overrides -----------------------------------------------------------

// Every `[data-theme="dark"]` rule in a run's markup, each followed by the gradient it
// points at (its id and stops), so a changed colour reads as itself, not as a hash.
function darkRules(markup) {
  const stops = (id) => {
    const gradient = new RegExp(
      `<linearGradient id="${id}"[^>]*>(.*?)</linearGradient>`
    ).exec(markup);
    const parts = [
      ...gradient[1].matchAll(
        /offset="([^"]+)" stop-color="([^"]+)" stop-opacity="([^"]+)"/g
      ),
    ].map(([, offset, colour, alpha]) => `${offset} ${colour} @${alpha}`);
    return `${id}: ${parts.join(", ")}`;
  };
  return [...markup.matchAll(/\[data-theme="dark"\][^{]*\{[^}]*\}/g)].flatMap(
    ([rule]) => {
      const id = /url\(#([^)]+)\)/.exec(rule);
      return id ? [rule, stops(id[1])] : [rule];
    }
  );
}

const SKY = '[data-theme="dark"] .t-sky{fill:url(#t-skyd)}';
// What each design's dark theme changes (the designs not listed change nothing). The sky
// washes and the stalk's end stop swap in a gradient; the rest recolour or dim a class.
const DARK_EXPECTED = {
  birthday: [SKY, "t-skyd: 0 #f472b6 @0.34, 0.3 #8b5cf6 @0.2, 0.52 #8b5cf6 @0"],
  christmas: ['[data-theme="dark"] .t-flake{stroke:#e0e7ff}'],
  diwali: [],
  eid: [],
  holi: [SKY, "t-skyd: 0 #ec4899 @0.34, 0.3 #7c3aed @0.22, 0.52 #7c3aed @0"],
  independence: [
    '[data-theme="dark"] .t-tbw{fill-opacity:.55;stroke-opacity:.55}',
    SKY,
    "t-skyd: 0 #ea7a17 @0.4, 0.3 #f97316 @0.2, 0.52 #f97316 @0",
  ],
  newyear: [],
  onam: [],
  pongal: [
    '[data-theme="dark"] .t-stalk{stroke:url(#t-scaned)}',
    "t-scaned: 0 #9d4b73 @1, 0.5 #5b1a3a @1, 1 #7a2f57 @1",
    SKY,
    "t-skyd: 0 #c2410c @0.62, 0.28 #f59e0b @0.3, 0.52 #f59e0b @0",
  ],
};

for (const [design, mod] of Object.entries(SCENES)) {
  test(`${design}: the dark theme's overrides are as recorded`, () => {
    const r = run(mod);
    const rules = darkRules(r.markup());
    r.api.settle("instant");
    r.end();
    assert.deepEqual(rules, DARK_EXPECTED[design], `${design} dark rules`);
  });
}
