// The New Year scene (desktop stage layout): four firework bursts, the coming year in gold
// just above the party hat, two confetti cannons, and the right cannon fires the party hat
// onto Jarvis. Contract and beats: scene-runtime.mjs. The year is the `year` option, drawn as
// text. The year and the fireworks stay between the clearance line and the top of the hero.
import { quadPath, r2, rng, tr } from "../stage.mjs";
import { Scene, playScene } from "./scene-runtime.mjs";
import { toTile } from "./scene-place.mjs";
import {
  BURSTS,
  CANNON,
  FIREWORKS_MIN_SHIFT,
  FLASHES,
  FLOOR_ROW,
  PEAK,
  ROCKET_MS,
  defsSVG,
  floorSVG,
  muzzle,
  newyearStyle,
  rocketAt,
  skySVG,
  yearPlan,
  yearSVG,
  yearText,
} from "./newyear-art.mjs";

const HAT_AT = 2300; // the right cannon fires
const HAT_MS = 1150;

const CFG = {
  end: 7400,
  land: 3450,
  swap: 3700,
  face: 5100,
  look: [
    [-9, -5, 0],
    [-6, -7, 0.12],
    [-2, -7, 0.24],
    [5, -7, 0.36],
    [9, -5, 0.48],
    [9, 3, 0.62],
    [6, -7, 0.84],
    [-7, -6, 1],
  ],
  row: FLOOR_ROW,
  prep: HAT_AT,
};
const SPRAY_REACH = 186; // how high the confetti rises from a cannon at the stage's height, in units

export const END_MS = CFG.end;

export function play(host, opts) {
  return playScene(NewYearScene, host, opts);
}

class NewYearScene extends Scene {
  constructor(host, opts) {
    super(host, opts, CFG);
  }

  // The fireworks follow the mark, but never up past the clearance line.
  extra(_b, geo) {
    return { dy: Math.max(geo.shift, FIREWORKS_MIN_SHIFT) };
  }

  px() {
    return `${newyearStyle(this.p)}${defsSVG(this.p)}`;
  }

  inner(geo) {
    const { p } = this;
    const floor = this.opts.replay
      ? ""
      : this.floorGroup(floorSVG(p, geo.ground));
    return `${skySVG(p, geo.dy)}${yearSVG(
      p,
      yearText(this.opts.year),
      yearPlan(geo)
    )}${floor}${this.item("newyear")}`;
  }

  playArt() {
    const { stage, p } = this;
    BURSTS.forEach((b) => this.firework(b));
    this.confetti();
    stage.animate(
      stage.q(`.${p}yrwrap`),
      [
        { opacity: 0, transform: "scale(.6)" },
        { opacity: 1, transform: "scale(1.08)", offset: 0.35 },
        { opacity: 1, transform: "scale(1)", offset: 0.5 },
        { opacity: 1, transform: "scale(1)" },
      ],
      { duration: 2600, delay: 1900, easing: "ease-out", fill: "forwards" }
    );
  }

  // A rocket from the bottom edge, a flash, then the burst.
  firework(b) {
    const { stage, p, geo } = this;
    const t = rocketAt(b.n);
    const y = b.y + geo.dy;
    stage.animate(
      stage.q(`.${p}nrk${b.n}`),
      [
        { transform: tr(b.x + (b.n % 2 ? 14 : -14), geo.hu), opacity: 1 },
        { transform: tr(b.x, y), opacity: 1, offset: 0.95 },
        { transform: tr(b.x, y - 3), opacity: 0 },
      ],
      {
        duration: ROCKET_MS,
        delay: t,
        easing: "cubic-bezier(.3,.6,.4,1)",
        fill: "forwards",
      }
    );
    const flash = FLASHES.find((f) => f.cls === `nflash${b.n}`);
    stage.animate(
      stage.q(`.${p}${flash.cls}`),
      [
        { opacity: 0, transform: "scale(.2)" },
        { opacity: 1, transform: "scale(1)", offset: 0.2 },
        { opacity: 0, transform: `scale(${PEAK})` },
      ],
      { duration: flash.ms, delay: flash.at, fill: "forwards" }
    );
    this.burst(`nb${b.n}`, b, flash.at, 70 + b.n);
  }

  // The cannons stand low, so the spray is scaled up to reach the same height by the mark.
  confetti() {
    const { stage, p, geo } = this;
    if (this.opts.replay) return; // no cannons on a replay
    const rand = rng(57);
    const muzzleY = this.fitted([
      CANNON.x[1],
      this.floorBottom + muzzle(CANNON.x[1], -1)[1],
    ])[1];
    const target = geo.mt + 70 * geo.k;
    const fz = Math.max(1, (muzzleY - target) / SPRAY_REACH) / geo.floor.scale;
    const spray = (cls, dir, delay) =>
      stage.qa(`.${p}${cls}`).forEach((c) => {
        const ang = ((-90 + dir * (20 + rand() * 40)) * Math.PI) / 180;
        const v = (260 + rand() * 160) * fz;
        const d = 1400 + rand() * 600;
        const spin = (rand() - 0.5) * 1440;
        const frames = Array.from({ length: 11 }, (_, k) => {
          const t = (d / 1000) * (k / 10);
          return {
            transform: tr(
              Math.cos(ang) * v * t,
              Math.sin(ang) * v * t + 0.5 * 420 * fz * t * t,
              ` rotate(${r2((spin * k) / 10)}deg)`
            ),
            opacity: k > 8 ? 0 : 1,
          };
        });
        stage.animate(c, frames, {
          duration: d,
          delay: delay + rand() * 250,
          easing: "cubic-bezier(.2,.6,.4,1)",
          fill: "forwards",
        });
      });
    spray("cl", 1, 2150);
    spray("cr", -1, 2250);
  }

  // The right cannon fires the party hat in a high arc onto Jarvis's head. On a replay the
  // cannons are not drawn, so the hat comes in from the bottom right corner instead.
  prepare() {
    const arc = (g) => {
      const [mx, my] = muzzle(CANNON.x[1], -1);
      const mouth = this.opts.replay
        ? [g.to - 20, g.hu + 20]
        : this.fitted([mx, this.floorBottom + my]);
      return quadPath(toTile(g, mouth), [140, -150], [0, 0], 14, -720);
    };
    this.fly(arc, {
      duration: HAT_MS,
      easing: "cubic-bezier(.35,.1,.5,1)",
    });
  }
}
