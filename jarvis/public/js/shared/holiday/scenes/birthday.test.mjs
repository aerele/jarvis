// The Birthday scene: the shared contract, the streamers and balloons keeping clear of the
// top and of the hero, no floor row, the cake landing on the beat, and photosensitivity (the
// scene has no flash at all; only slow sways repeat).
import { test } from "node:test";
import assert from "node:assert/strict";
import * as scene from "./birthday.mjs";
import {
  BALLOON,
  BALLOONS,
  STREAMER,
  balloonTops,
  streamerPlan,
} from "./birthday-art.mjs";
import { CLEAR, GAP, sceneHeroTop } from "../scene-geometry.mjs";
import {
  boxes,
  contractSuite,
  loops,
  run,
} from "./day-scene-fakes.test-support.mjs";

contractSuite("birthday", scene, { launch: 2400 });

test("a streamer hangs from the top edge and stops above the hero", () => {
  for (const airBottom of [20, 40, 60, 90, 140]) {
    for (const s of streamerPlan(airBottom)) {
      assert.match(s.d, /^M[\d.-]+ 0 /, "starts on the top edge");
      assert.ok(
        s.ends <= Math.max(STREAMER.min, airBottom) + 1e-6,
        `${airBottom}`
      );
      assert.ok(s.ends >= STREAMER.min - 1e-6);
    }
  }
});

test("with room the streamers are their listed lengths, 46 to 86 units", () => {
  const lens = streamerPlan(400).map((s) => s.ends);
  assert.ok(
    lens.every((l) => l >= 46 && l <= 86),
    lens.join()
  );
  assert.equal(lens.length, 8);
});

test("no streamer is over the hero's column", () => {
  for (const s of streamerPlan(400))
    assert.ok(Math.abs(s.x - 400) > 36 + 40, `${s.x}`);
});

test("a streamer's dash length is its drawn length, so it draws on exactly", () => {
  const r = run(scene);
  const strs = r.host.svg.querySelectorAll(".t-str");
  assert.equal(strs.length, 8);
  for (const st of strs) {
    assert.equal(st.getAttribute("stroke-dasharray"), st.dataset.len);
    assert.ok(Number(st.dataset.len) > 0);
    const a = r.host.svg.animations.find((x) => x.node === st);
    assert.equal(a.frames[0].strokeDashoffset, Number(st.dataset.len));
    assert.equal(a.frames[1].strokeDashoffset, 0);
  }
  r.api.settle("instant");
  r.end();
});

test("the streamers on the page stop above the mark's top", () => {
  const r = run(scene, { boxes: boxes({ w: 1000, markY: 70 }) });
  const oy = 70 / 1.25;
  const plan = streamerPlan(oy - GAP);
  assert.ok(
    plan.every((s) => s.ends <= Math.max(STREAMER.min, oy - GAP) + 1e-6)
  );
  assert.equal(r.host.svg.querySelectorAll(".t-str").length, plan.length);
  r.api.settle("instant");
  r.end();
});

test("the page keeps the mark low enough for the streamers: the art needs no more than the table", () => {
  assert.ok(sceneHeroTop("birthday", 1) >= CLEAR + STREAMER.min + GAP - 1e-9);
});

test("balloons settle under the clearance line, follow the mark, and only move down to the low line", () => {
  for (const shift of [-50, 0, 40, 200]) {
    const ground = 380;
    const tops = balloonTops({ shift, ground });
    assert.equal(tops.length, BALLOONS.length);
    for (const y of tops) assert.ok(y - BALLOON.ry >= CLEAR - 1e-6, `${y}`);
  }
  const listed = [150, 110, 132, 120, 160, 104];
  const tops = balloonTops({ shift: 0, ground: 372 });
  const low = 372 - 140;
  assert.equal(tops[0], low, "the outer pair rests on the low line");
  assert.equal(tops[5], low);
  assert.equal(tops[2], listed[2], "the inner pair stays by the mark");
  assert.equal(tops[3], listed[3]);
  assert.equal(tops[1], listed[1] + (low - listed[1]) * 0.5);
  // A very short stage must never lift a balloon back up from its listed height.
  for (const ground of [100, 200, 372]) {
    balloonTops({ shift: 0, ground }).forEach((y, i) =>
      assert.ok(
        y >= Math.max(listed[i], CLEAR + BALLOON.ry) - 1e-6,
        `${ground} ${i}`
      )
    );
  }
});

test("the balloons rise from below the stage and the confetti spans its width", () => {
  const r = run(scene, { boxes: boxes({ w: 1800, h: 700 }) });
  const rise = r.host.svg.animations.filter((a) =>
    a.node.classes.includes("t-bal")
  );
  assert.equal(rise.length, 6);
  rise.forEach((a) =>
    assert.ok(
      Number(a.frames[0].transform.match(/, ([\d.-]+)px/)[1]) > 700 / 1.25
    )
  );
  const xs = r.host.svg
    .querySelectorAll(".t-bconf")
    .map((n) => Number(n.getAttribute("x")));
  assert.equal(xs.length, 40);
  assert.ok(Math.min(...xs) < 100 && Math.max(...xs) > 700);
  r.api.settle("instant");
  r.end();
});

test("there is no floor row: the cards sit where the lower balloons settle", () => {
  const r = run(scene);
  assert.equal(r.host.svg.querySelector(".t-floor"), null);
  r.api.settle("instant");
  r.end();
});

test("the cake slides in and lands on the beat that delivers it", () => {
  const r = run(scene);
  r.tick(2401);
  const a = r.host.svg.animations.find((x) =>
    x.node.classes.includes("t-drop")
  );
  assert.equal(a.options.duration, 900);
  assert.equal(
    a.frames.at(-1).opacity,
    0,
    "it hands over to the mark's own cake"
  );
  assert.equal(r.count("deliver"), 0);
  r.tick(899); // 3300 ms: the item lands as the beat fires
  assert.equal(r.count("deliver"), 1);
  assert.equal(2400 + a.options.duration, 3300);
  r.api.settle("instant");
  r.end();
});

// ---- photosensitivity --------------------------------------------------------------------
// No flash is drawn at all. The sky fades once over the whole scene; only the balloons sway,
// slowly (over 2 s a swing), far under 3 changes a second.

test("the scene has no flash, and what repeats is a slow sway", () => {
  const r = run(scene);
  assert.ok(!/flash|glow|strobe/i.test(r.markup()));
  const repeating = loops(r);
  assert.equal(repeating.length, 6);
  for (const [cls, ms] of repeating) assert.ok(ms >= 2000, `${cls} ${ms}`);
  const sky = r.host.svg.animations.find((x) =>
    x.node.classes.includes("t-sky")
  );
  assert.equal(sky.options.duration, scene.END_MS);
  r.api.settle("instant");
  r.end();
});
