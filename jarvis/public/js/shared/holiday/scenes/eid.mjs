// The Eid scene (desktop stage layout): moonrise, twinkling stars, floating lanterns, and
// the crescent lowering onto Jarvis. Contract and beats: scene-runtime.mjs. The moon and the
// lanterns stay under the clearance line.
import { tr } from "../stage.mjs";
import { Scene, playScene } from "./scene-runtime.mjs";
import { squeezeX } from "./scene-place.mjs";
import {
  FLOOR_ROW,
  LANTERN,
  LOW,
  MOON,
  defsSVG,
  eidStyle,
  lanternSpot,
  lanternXs,
  lanternsSVG,
  moonSVG,
  moonY,
  starsSVG,
} from "./eid-art.mjs";

const CRESCENT_AT = 2500;
const CRESCENT_MS = 1200;

const CFG = {
  end: 7400,
  land: 3700,
  swap: 3950,
  face: 5300,
  look: [
    [8, -6, 0],
    [8, -6, 0.2],
    [-8, -6, 0.4],
    [-9, 2, 0.55],
    [9, 2, 0.68],
    [6, -7, 1],
  ],
  row: FLOOR_ROW,
  prep: CRESCENT_AT,
};

export const END_MS = CFG.end;

export function play(host, opts) {
  return playScene(EidScene, host, opts);
}

class EidScene extends Scene {
  constructor(host, opts) {
    super(host, opts, CFG);
  }

  // Where the lanterns hang follows the page as it stands, like the moon.
  extra(_b, geo) {
    return { lanternX: lanternXs(geo) };
  }

  px() {
    return `${eidStyle(this.p)}${defsSVG(this.p)}`;
  }

  inner(geo) {
    const { p } = this;
    const { low, air } = lanternsSVG(p, geo.lanternX);
    const floor = this.opts.replay ? "" : this.floorGroup(low);
    return `${moonSVG(p)}${starsSVG(p, geo)}${floor}${air}${this.item("eid")}`;
  }

  playArt() {
    const { stage, p, geo } = this;
    const my = moonY(geo);
    const moonX = squeezeX(MOON.x, geo);
    stage.animate(
      stage.q(`.${p}moon`),
      [
        { transform: tr(moonX, my + MOON.rise), opacity: 0 },
        { opacity: 1, offset: 0.3 },
        { transform: tr(moonX, my), opacity: 1 },
      ],
      { duration: 2200, delay: 100, easing: "cubic-bezier(.2,.7,.2,1)" }
    );
    stage.qa(`.${p}stars .${p}sp`).forEach((s, i) =>
      stage.animate(
        s,
        [
          { opacity: 0, transform: "scale(.2)" },
          { opacity: 1, transform: "scale(1.15)", offset: 0.4 },
          { opacity: 0.75, transform: "scale(.85)", offset: 0.7 },
          { opacity: 1, transform: "scale(1)" },
        ],
        { duration: 900, delay: 500 + i * 110, fill: "forwards" }
      )
    );
    [0, 1, 2, 3, 4].forEach((i) => this.lantern(i));
  }

  // A lantern floats up from below the stage to its place and sways there. The two low
  // ones live in the floor row, which shrinks about its bottom: they start further down by
  // the string's length at the row's scale, so no string shows at the stage's bottom edge.
  lantern(i) {
    const { stage, p, geo } = this;
    const x = geo.lanternX[i];
    if (x === null) return; // no room beside the hero: not drawn
    const [, y] = lanternSpot(i, geo);
    const stub = LOW.includes(i) ? LANTERN.string / geo.floor.scale : 0;
    const from = geo.hu + 90 + stub;
    stage.animate(
      stage.q(`.${p}lan${i}`),
      [
        { transform: tr(x, from), opacity: 1 },
        { transform: tr(x, y), opacity: 1 },
      ],
      {
        duration: 2600 + i * 200,
        delay: 300 + i * 180,
        easing: "cubic-bezier(.25,.7,.3,1)",
      }
    );
    this.sway(
      stage.q(`.${p}lan${i} .${p}lsw`),
      "rotate(-5deg)",
      "rotate(5deg)",
      2600 + i * 300
    );
  }

  // The crescent lowers on its string from the sky onto Jarvis.
  prepare() {
    this.fly(
      [
        { opacity: 0, transform: tr(0, -70) },
        { opacity: 1, offset: 0.1 },
        { transform: tr(0, 3), offset: 0.8, easing: "ease-in-out" },
        { opacity: 1, transform: tr(0, 0), offset: 0.97 },
        { opacity: 0, transform: tr(0, 0) },
      ],
      { duration: CRESCENT_MS, easing: "cubic-bezier(.3,.6,.3,1)" }
    );
  }
}
