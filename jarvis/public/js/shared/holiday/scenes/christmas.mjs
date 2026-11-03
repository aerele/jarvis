// The Christmas scene (desktop stage layout): snow, Santa's sleigh dropping the hat, a lit
// tree and gifts. Contract and beats: scene-runtime.mjs. The sleigh flies between the
// clearance line and the top of the hero, at the size that fits.
import { r2, rng, tr } from "../stage.mjs";
import { Scene, playScene } from "./scene-runtime.mjs";
import { squeezeX, toTile } from "./scene-place.mjs";
import { landSparklesSVG, sparkleSVG } from "./scene-art.mjs";
import {
  FLOOR_GROW,
  FLOOR_ROW,
  LIGHTS,
  SLEIGH,
  christmasStyle,
  defsSVG,
  flakeSVG,
  floorSVG,
  sleighAt,
  sleighPlan,
  sleighSVG,
} from "./christmas-art.mjs";

const FLIGHT_MS = 3600;
const FLIGHT_AT = 200;
const DROP_AT = 1950; // Santa lets the hat go as the sleigh passes over Jarvis
const DROP_MS = 950;

const CFG = {
  end: 7000,
  land: 2900,
  swap: 3150,
  face: 4500,
  look: [
    [-9, -5, 0],
    [-9, -6, 0.12],
    [-4, -7, 0.42],
    [0, -7, 0.66],
    [2, -5, 0.86],
    [0, -3, 1],
  ],
  row: FLOOR_ROW,
  prep: DROP_AT, // the hat is let go: the mark is read again for it
};

export const END_MS = CFG.end;

export function play(host, opts) {
  return playScene(ChristmasScene, host, opts);
}

class ChristmasScene extends Scene {
  constructor(host, opts) {
    super(host, opts, CFG);
  }

  px() {
    return `${christmasStyle(this.p)}${defsSVG(this.p)}`;
  }

  inner(geo) {
    const { p } = this;
    this.plan = sleighPlan(geo);
    const rand = rng(5);
    const width = geo.to - geo.from;
    const flakes = Array.from({ length: 34 }, () => {
      const [x, y] = [geo.from + rand() * width, -14 - rand() * 120];
      return `<g transform="translate(${r2(x)} ${r2(
        y
      )})"><g class="${p}flake" opacity="0">${flakeSVG(
        0.45 + rand() * 0.7
      )}</g></g>`;
    }).join("");
    const trail = Array.from({ length: 36 }, (_, i) => {
      const [x, y] = sleighAt(this.plan, i / 36);
      const [dx, dy] = [(rand() - 0.5) * 12, (rand() - 0.5) * 16];
      const colour = ["#fbbf24", "#f59e0b", "#fde047"][i % 3];
      return sparkleSVG(
        p,
        x - 52 * this.plan.k + dx,
        y - 6 + dy,
        0.22 + rand() * 0.28,
        colour
      );
    }).join("");
    const floor = this.opts.replay
      ? ""
      : this.floorGroup(
          this.onGround(
            floorSVG(
              p,
              (x, prop) => this.edge(x, prop),
              (x, hw) => squeezeX(x, geo, { hw, g: FLOOR_GROW })
            )
          )
        );
    return `${floor}<g>${flakes}</g><g class="${p}trail">${trail}</g>
        ${this.item("christmas")}
        <g class="${p}sleigh"><g transform="scale(${r2(
      this.plan.k
    )})">${sleighSVG(p)}</g></g>`;
  }

  playArt() {
    const { stage, p, plan } = this;
    const q = (n) => stage.qa(`.${p}${n}`);
    const rand = rng(9);
    q("flake").forEach((f) => {
      const d = 5600 + rand() * 2600;
      const spin = (rand() - 0.5) * 360;
      const sway = r2((rand() - 0.5) * 70);
      const frames = [
        { transform: "translate(0px, 0px) rotate(0deg)", opacity: 0 },
        { opacity: 0.95, offset: 0.12 },
        { opacity: 0.95, offset: 0.82 },
        {
          transform: `translate(${sway}px, ${r2(
            this.geo.hu + 80
          )}px) rotate(${r2(spin)}deg)`,
          opacity: 0,
        },
      ];
      stage.animate(f, frames, {
        duration: d,
        delay: rand() * 1600,
        fill: "forwards",
      });
    });
    this.lights();
    stage.animate(
      stage.q(`.${p}tstar`),
      [
        { opacity: 0, transform: "scale(.4)" },
        { opacity: 1, transform: "scale(1.2)", offset: 0.6 },
        { opacity: 1, transform: "scale(1)" },
      ],
      { duration: 600, delay: 1700, easing: "ease-out" }
    );
    q("gift").forEach((g, i) =>
      stage.animate(
        g,
        [
          { opacity: 0, transform: "scale(.2)" },
          { opacity: 1, transform: "scale(1.08)", offset: 0.7 },
          { opacity: 1, transform: "scale(1)" },
        ],
        { duration: 420, delay: 600 + i * 170, easing: "ease-out" }
      )
    );
    const flight = Array.from({ length: 25 }, (_, k) => {
      const [x, y, ang] = sleighAt(plan, k / 24);
      return {
        transform: `translate(${r2(x)}px, ${r2(y)}px) rotate(${r2(ang)}deg)`,
      };
    });
    stage.animate(stage.q(`.${p}sleigh`), flight, {
      duration: FLIGHT_MS,
      delay: FLIGHT_AT,
    });
    q("deer").forEach((d, i) =>
      stage.animate(
        d,
        [
          { transform: tr(0, 0) },
          { transform: tr(0, -2.6) },
          { transform: tr(0, 0) },
        ],
        {
          duration: 520,
          iterations: Infinity,
          iterationStart: i * 0.35,
          easing: "ease-in-out",
          fill: "none",
        }
      )
    );
    stage.qa(`.${p}trail .${p}sp`).forEach((s, i) =>
      stage.animate(
        s,
        [
          { opacity: 0, transform: "scale(.3)" },
          { opacity: 1, transform: "scale(1)", offset: 0.25 },
          { opacity: 0, transform: "translateY(6px) scale(.2)" },
        ],
        { duration: 1000, delay: 320 + FLIGHT_MS * (i / 36), fill: "forwards" }
      )
    );
  }

  // The tree's fairy lights switch on one by one, then twinkle.
  lights() {
    const { stage, p } = this;
    stage.qa(`.${p}tl`).forEach((l, i) => {
      stage.animate(l, [{ opacity: 0 }, { opacity: 1 }], {
        duration: 200,
        delay: 300 + i * 110,
      });
      stage.animate(
        l.lastElementChild,
        [{ opacity: 1 }, { opacity: 0.35 }, { opacity: 1 }],
        {
          duration: LIGHTS.periodMs + (i % 4) * 230,
          delay: 1700 + i * 60,
          iterations: Infinity,
          fill: "none",
        }
      );
    });
  }

  // Santa lets the hat go right over Jarvis; it tumbles down onto the tile. It starts where
  // the sleigh is, so it is read from the mark's place now.
  prepare() {
    const { plan } = this;
    const frames = (g) => {
      const [sx, sy] = sleighAt(plan, (DROP_AT - FLIGHT_AT) / FLIGHT_MS);
      const [x0, y0] = toTile(g, [
        sx + SLEIGH.santaX * plan.k,
        sy + SLEIGH.santaY * plan.k,
      ]);
      const up = Math.min(-20, y0);
      const f = up / -52;
      return [
        { opacity: 0, transform: tr(x0, up, " rotate(-40deg)"), offset: 0 },
        { opacity: 1, offset: 0.08 },
        {
          transform: tr(-9, -30 * f, " rotate(12deg)"),
          offset: 0.45,
          easing: "ease-in",
        },
        { transform: tr(-3, -10 * f, " rotate(-8deg)"), offset: 0.75 },
        { opacity: 1, transform: tr(0, 0, " rotate(0deg)"), offset: 0.97 },
        { opacity: 0, transform: tr(0, 0, " rotate(0deg)"), offset: 1 },
      ];
    };
    this.fly(frames, { duration: DROP_MS });
    this.append(this.pin(39, 3, landSparklesSVG(this.p)));
    this.sparkle(DROP_MS);
  }
}
