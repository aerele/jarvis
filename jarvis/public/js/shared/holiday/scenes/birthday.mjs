// The Birthday scene (desktop stage layout): a pink wash, streamers, balloons, the cake
// sliding in to Jarvis, confetti. Contract and beats: scene-runtime.mjs. The streamers stay
// above the hero; there is no floor row.
import { tr } from "../stage.mjs";
import { Scene, playScene } from "./scene-runtime.mjs";
import { skyWashSVG } from "./scene-art.mjs";
import { squeezeX } from "./scene-place.mjs";
import {
  SKY_STOPS,
  SKY_STOPS_DARK,
  balloonTops,
  balloonXs,
  balloonsSVG,
  birthdayStyle,
  confettiSVG,
  streamerPlan,
  streamersSVG,
} from "./birthday-art.mjs";

const CAKE_AT = 2400; // the cake slides in and lands as Jarvis smiles
const CAKE_MS = 900;

const CFG = {
  end: 7200,
  land: 3300,
  swap: 3550,
  face: 5000,
  look: [
    [-9, 4, 0],
    [-9, -6, 0.3],
    [9, -6, 0.55],
    [9, 2, 0.72],
    [9, 3, 1],
  ],
  prep: CAKE_AT,
};

export const END_MS = CFG.end;

export function play(host, opts) {
  return playScene(BirthdayScene, host, opts);
}

class BirthdayScene extends Scene {
  constructor(host, opts) {
    super(host, opts, CFG);
  }

  // Which balloons hang, and where, follows the page as it stands (a phone leaves some out).
  extra(_b, geo) {
    return { balloonX: balloonXs(geo) };
  }

  px(geo) {
    const { p } = this;
    return `${birthdayStyle(p)}${skyWashSVG(
      p,
      SKY_STOPS,
      geo.W,
      geo.skyPx,
      SKY_STOPS_DARK
    )}`;
  }

  inner(geo) {
    const { p } = this;
    const plan = streamerPlan(geo.airBottom, (x) => squeezeX(x, geo));
    return `${streamersSVG(p, plan)}${balloonsSVG(p, geo.balloonX)}${this.item(
      "birthday"
    )}${confettiSVG(p, geo.from, geo.to)}`;
  }

  playArt() {
    const { stage, p, geo } = this;
    const q = (n) => stage.qa(`.${p}${n}`);
    this.playSky();
    q("str").forEach((st, i) => {
      const len = Number(st.dataset.len);
      stage.animate(st, [{ strokeDashoffset: len }, { strokeDashoffset: 0 }], {
        duration: 700,
        delay: 100 + i * 70,
        easing: "ease-out",
      });
    });
    // Balloons float up from below and settle at the sides, bobbing. `live` are the listed
    // balloons that were drawn, in order (all of them on desktop).
    const tops = balloonTops(geo);
    const live = geo.balloonX.flatMap((x, i) => (x === null ? [] : [i]));
    q("bal").forEach((b, n) =>
      stage.animate(
        b,
        [
          { transform: tr(0, geo.hu + 70), opacity: 1 },
          { transform: tr(0, tops[live[n]]), opacity: 1 },
        ],
        {
          duration: 2300 + live[n] * 120,
          delay: 150 + live[n] * 160,
          easing: "cubic-bezier(.2,.7,.3,1)",
        }
      )
    );
    q("bsw").forEach((b, n) =>
      this.sway(
        b,
        "rotate(-4deg) translateY(0)",
        "rotate(4deg) translateY(-4px)",
        2400 + live[n] * 230
      )
    );
    this.fall("bconf", 87, 1200);
  }

  // The cake slides in from the right and lands in Jarvis's hold.
  prepare() {
    this.fly(
      [
        { opacity: 0, transform: tr(150, 10) },
        { opacity: 1, offset: 0.15 },
        { transform: tr(-3, 0), offset: 0.8, easing: "ease-out" },
        { opacity: 1, transform: tr(0, 0), offset: 0.97 },
        { opacity: 0, transform: tr(0, 0) },
      ],
      { duration: CAKE_MS, easing: "cubic-bezier(.2,.7,.3,1)" }
    );
  }
}
