// The Pongal scene (desktop stage layout): dawn sky, the sun rising, a mango-leaf thoranam,
// sugarcane, a self-drawing kolam, and the pot boiling over on its fire. Contract and beats:
// scene-runtime.mjs.
import { ballistic, rng, tr } from "../stage.mjs";
import { Scene, playScene } from "./scene-runtime.mjs";
import { edgeX } from "./scene-place.mjs";
import {
  FLOOR_ROW,
  RAMP_MS,
  SKY_STOPS,
  SKY_STOPS_DARK,
  SUN,
  caneSVG,
  defsSVG,
  frothSVG,
  hearthSVG,
  kolamSVG,
  pongalStyle,
  sunMinY,
  sunSVG,
  toranSVG,
} from "./pongal-art.mjs";
import { skyWashSVG } from "./scene-art.mjs";

const CFG = {
  end: 7600,
  land: 3050, // Jarvis's own pot boils over
  prep: 3050 - 780, // the froth is read from the pot where it now is
  swap: 3350,
  face: 4800,
  // [dx, dy, offset] of the eyes over the first 3050 ms: up at the sun, down at the kolam,
  // to the pot on the fire.
  look: [
    [7, -6, 0],
    [7, -6, 0.2],
    [-8, 5, 0.34],
    [8, 5, 0.6],
    [0, 6, 0.8],
    [0, 5, 1],
  ],
  row: FLOOR_ROW,
};

export const END_MS = CFG.end;

// Nothing in this scene flashes: the sun and the hearth glow come up slowly and stay (see
// RAMP_MS in pongal-art.mjs), the froth and bubbles are a few small dots.

export function play(host, opts) {
  return playScene(PongalScene, host, opts);
}

class PongalScene extends Scene {
  constructor(host, opts) {
    super(host, opts, CFG);
  }

  // The sun stays beside the mark and its rays stay below the thoranam, wherever the mark is.
  extra(_b, geo) {
    return { sunY: Math.max(geo.mt + SUN.dy, sunMinY()) };
  }

  px(geo) {
    const { p } = this;
    return `${pongalStyle(p)}${defsSVG(p)}${skyWashSVG(
      p,
      SKY_STOPS,
      geo.W,
      geo.skyPx,
      SKY_STOPS_DARK
    )}`;
  }

  inner(geo) {
    const { p } = this;
    return `${sunSVG(p)}${toranSVG(p, geo.from, geo.to)}${
      this.opts.replay
        ? ""
        : this.floorGroup(this.onGround(this.floorProps(geo)))
    }`;
  }

  // Sugarcane at the stage's edges, the kolam and the hearth in the middle (left out when the
  // hero leaves them no room even at the row's smallest).
  floorProps(geo) {
    const { p } = this;
    const cane = (x, h, lean) => {
      const at = edgeX(x, geo);
      return this.edge(at, caneSVG(p, at, h, lean));
    };
    const middle = geo.floor.tight ? "" : `${kolamSVG(p)}${hearthSVG(p)}`;
    return `${cane(58, 150, 8)}${cane(88, 118, -6)}${cane(712, 122, 6)}${cane(
      742,
      154,
      -8
    )}${middle}`;
  }

  playArt() {
    this.playSky();
    this.dropToran();
    this.riseSun();
    this.playCanes();
    this.playKolam();
    this.playHearth();
  }

  riseSun() {
    const { stage, p, geo } = this;
    const x = 400 + SUN.dx;
    stage.animate(
      stage.q(`.${p}sun`),
      [
        { transform: tr(x, geo.sunY + SUN.rise), opacity: 0 },
        { opacity: 1, offset: 0.25 },
        { transform: tr(x, geo.sunY), opacity: 1 },
      ],
      { duration: RAMP_MS.sun, delay: 100, easing: "cubic-bezier(.2,.7,.2,1)" }
    );
    stage.animate(
      stage.q(`.${p}rays`),
      [{ transform: "rotate(0deg)" }, { transform: "rotate(60deg)" }],
      { duration: 9000, iterations: Infinity, fill: "none" }
    );
  }

  playCanes() {
    const { stage, p } = this;
    stage
      .qa(`.${p}grow`)
      .forEach((g, i) =>
        stage.animate(
          g,
          [
            { transform: "scaleY(0)" },
            { transform: "scaleY(1.05)", offset: 0.8 },
            { transform: "scaleY(1)" },
          ],
          { duration: 700, delay: 300 + i * 120, easing: "ease-out" }
        )
      );
  }

  // The kolam: dots first, then the three rings of lines draw on, then colour.
  playKolam() {
    const { stage, p } = this;
    stage.qa(`.${p}kdot`).forEach((d, i) =>
      stage.animate(
        d,
        [
          { opacity: 0, transform: "scale(0)" },
          { opacity: 1, transform: "scale(1)" },
        ],
        { duration: 240, delay: 300 + i * 14, easing: "ease-out" }
      )
    );
    stage.qa(`.${p}kl`).forEach((l, i) => {
      if (typeof l.getTotalLength !== "function") return;
      const len = l.getTotalLength();
      l.style.strokeDasharray = `${len}`;
      stage.animate(l, [{ strokeDashoffset: len }, { strokeDashoffset: 0 }], {
        duration: 1500,
        delay: 800 + i * 350,
        easing: "cubic-bezier(.4,0,.3,1)",
      });
    });
    stage.qa(`.${p}kpet`).forEach((k, i) =>
      stage.animate(
        k,
        [
          { opacity: 0, transform: "scale(.4)" },
          { opacity: 0.9, transform: "scale(1)" },
        ],
        { duration: 320, delay: 2300 + i * 22, easing: "ease-out" }
      )
    );
  }

  // The fire catches, the milk rises, and the pot boils over: Pongalo Pongal.
  playHearth() {
    const { stage, p } = this;
    this.pop("hflame", { peak: 1.15, ms: 420, delay: 700, step: 140 });
    stage.animate(stage.q(`.${p}hglow`), [{ opacity: 0 }, { opacity: 1 }], {
      duration: RAMP_MS.hearth,
      delay: 800,
    });
    stage.animate(
      stage.q(`.${p}rise`),
      [
        { transform: "scaleY(0)" },
        { transform: "scaleY(.6)", offset: 0.6 },
        { transform: "scaleY(1.25)", offset: 0.93 },
        { transform: "scaleY(1)" },
      ],
      { duration: 1600, delay: 1400, easing: "ease-in" }
    );
    stage.animate(
      stage.q(`.${p}drip`),
      [
        { opacity: 0, transform: "scaleY(0)" },
        { opacity: 1, transform: "scaleY(1)" },
      ],
      { duration: 600, delay: 3000, easing: "ease-out" }
    );
    this.bubbles();
  }

  bubbles() {
    const rb = rng(44);
    this.stage.qa(`.${this.p}bub`).forEach((b, i) => {
      const a = -Math.PI / 2 + (rb() - 0.5) * 2.2;
      const v = 60 + rb() * 70;
      const d = 900 + rb() * 400;
      const frames = ballistic(
        Math.cos(a) * v,
        Math.sin(a) * v * 0.7,
        160,
        d
      ).map((f, k) => ({ ...f, opacity: k === 0 ? 0 : f.opacity }));
      this.stage.animate(b, frames, {
        duration: d,
        delay: 2950 + i * 25,
        easing: "cubic-bezier(.2,.7,.4,1)",
        fill: "forwards",
      });
    });
  }

  // Jarvis's own pot boils over at the same moment: froth from its spout. The mark is read
  // again just before, so the froth rises from the pot where it now is.
  prepare() {
    this.append(this.pin(44.5, 39, frothSVG(this.p)));
    const rand = rng(17);
    this.stage.qa(`.${this.p}fr`).forEach((f, i) => {
      const a = -Math.PI / 2 + (rand() - 0.5) * 1.7;
      const v = 18 + rand() * 28;
      this.stage.animate(
        f,
        [
          { transform: tr(0, 0, " scale(.4)"), opacity: 0 },
          { opacity: 1, offset: 0.15 },
          {
            transform: tr(Math.cos(a) * v, Math.sin(a) * v, " scale(1.15)"),
            opacity: 0,
          },
        ],
        {
          duration: 900 + rand() * 400,
          delay: this.untilLand(50 + i * 40),
          easing: "cubic-bezier(.2,.7,.3,1)",
          fill: "forwards",
        }
      );
    });
  }
}
