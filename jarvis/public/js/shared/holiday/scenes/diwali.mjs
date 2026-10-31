// The Diwali scene (desktop stage layout): a marigold toran, dusk sky, diyas and anar
// fountains along the floor, rockets and three firework bursts, and a spark that drifts down
// and lights the flame on Jarvis's item. Contract and beats: scene-runtime.mjs; the shared
// motion is scene-motion.mjs.
import { ballistic, quadPath, rng, tr } from "../stage.mjs";
import { BURSTS, FLOOR_ROW, fireworksMinDy } from "../scene-geometry.mjs";
import { Scene, playScene } from "./scene-runtime.mjs";
import {
  FLAME_TILE,
  FLASHES,
  LAND_MS,
  PEAK,
  defsSVG,
  diwaliStyle,
  floorSVG,
  lightSVG,
  skyShowSVG,
  skySVG,
  toranSVG,
} from "./diwali-art.mjs";

// The flash table (FLASHES, PEAK) lives with the drawing in diwali-art.mjs, so the radius
// drawn and the timing used here come from one place.

const CFG = {
  end: 7600,
  land: LAND_MS,
  prep: LAND_MS - 780, // the spark leaves the second burst for the flame
  swap: 4350,
  face: 5800,
  // [dx, dy, offset] of the eyes over the first 4080 ms: down at the diyas, up at the sky,
  // then to the flame.
  look: [
    [-9, 3, 0],
    [-9, 3, 0.06],
    [6, 3, 0.36],
    [-6, -7, 0.6],
    [8, -7, 0.75],
    [9, -6, 0.82],
    [3, -1, 1],
  ],
  row: FLOOR_ROW,
};

export const END_MS = CFG.end;

export function play(host, opts) {
  return playScene(DiwaliScene, host, opts);
}

class DiwaliScene extends Scene {
  constructor(host, opts) {
    super(host, opts, CFG);
  }

  // The fireworks follow the mark, but never up into the toran.
  extra(_b, geo) {
    return { dy: Math.max(geo.shift, fireworksMinDy()) };
  }

  px(geo) {
    const { p } = this;
    return `${diwaliStyle(p)}${defsSVG(p)}${skySVG(p, geo.W, geo.skyPx)}`;
  }

  inner(geo) {
    const { p } = this;
    return `${skyShowSVG(p, geo.dy)}${toranSVG(p, geo.from, geo.to)}${
      this.opts.replay ? "" : this.floorGroup(floorSVG(p, geo.ground))
    }`;
  }

  // The art's own timeline: toran, flames, fountains, rockets and bursts.
  playArt() {
    const { stage, p } = this;
    this.playSky();
    this.dropToran();
    this.pop("dflame", { peak: 1.18, ms: 380, delay: 250, step: 170 });
    stage.qa(`.${p}dglow`).forEach((g, i) =>
      stage.animate(g, [{ opacity: 0 }, { opacity: 1 }], {
        duration: 500,
        delay: 300 + i * 170,
      })
    );
    const rand = rng(21);
    stage.qa(`.${p}fount`).forEach((f) => {
      const d = 900 + rand() * 500;
      stage.animate(
        f,
        ballistic((rand() - 0.5) * 70, -(150 + rand() * 110), 320, d),
        { duration: d, delay: 1450 + rand() * 2400, fill: "forwards" }
      );
    });
    this.rockets();
  }

  rockets() {
    const { geo, stage } = this;
    // The rockets leave from just above the floor row, wherever the row now sits.
    const launchAt = (x) => this.fitted([x, geo.ground - 30]);
    const launch = (n, x, to, delay) =>
      stage.animate(
        stage.q(`.${this.p}rk${n}`),
        [
          { transform: tr(...launchAt(x)), opacity: 1 },
          { transform: tr(...to), opacity: 1, offset: 0.95 },
          { transform: tr(to[0], to[1] - 3), opacity: 0 },
        ],
        {
          duration: 650,
          delay,
          easing: "cubic-bezier(.3,.6,.4,1)",
          fill: "forwards",
        }
      );
    const dy = geo.dy;
    const [b1, b2, b3] = BURSTS;
    launch(1, 234, [b1.x, b1.y + dy], 1800);
    launch(2, 552, [b2.x, b2.y + dy], 2400);
    launch(3, 672, [b3.x, b3.y + dy], 3250);
    this.flashes();
    this.burst("b1", b1, 2450, 3);
    this.burst("b2", b2, 3050, 6);
    this.burst("b3", b3, 3900, 9);
  }

  flashes() {
    FLASHES.filter((f) => f.cls !== "lit").forEach((f) => this.flash(f, f.at));
  }

  flash(f, delay) {
    const el = this.stage.q(`.${this.p}${f.cls}`);
    this.stage.animate(
      el,
      [
        { opacity: 0, transform: "scale(.2)" },
        { opacity: 1, transform: "scale(1)", offset: 0.2 },
        { opacity: 0, transform: `scale(${PEAK})` },
      ],
      { duration: f.ms, delay, fill: "forwards" }
    );
  }

  // The last spark of the second burst drifts down to the flame on the mark. The mark's
  // position is read again before this runs, because the page may have moved since the
  // scene began.
  prepare() {
    const { dy } = this.geo;
    const [fx, fy] = this.tile(...FLAME_TILE);
    this.append(lightSVG(this.p, fx, fy));
    const burst = BURSTS[1];
    this.stage.animate(
      this.stage.q(`.${this.p}carry`),
      quadPath([burst.x, burst.y + dy], [492, 36 + dy], [fx, fy], 12),
      { duration: this.untilLand(), easing: "ease-in", fill: "forwards" }
    );
    this.flash(
      FLASHES.find((f) => f.cls === "lit"),
      this.untilLand()
    );
  }
}
