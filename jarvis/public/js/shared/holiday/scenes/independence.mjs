// The Independence Day scene (desktop stage layout): a saffron morning wash, four kites
// rise, tricolour paper bits drift down, and the rosette spins in and pins itself on. The
// same scene serves Republic Day: the design is the Indian tricolour either way, only the
// words differ (copy.mjs). Contract and beats: scene-runtime.mjs.
import { tr } from "../stage.mjs";
import { Scene, playScene } from "./scene-runtime.mjs";
import { landSparklesSVG, skyWashSVG } from "./scene-art.mjs";
import {
  SKY_STOPS,
  SKY_STOPS_DARK,
  bitsSVG,
  defsSVG,
  independenceStyle,
  kiteSVG,
  kiteSpots,
} from "./independence-art.mjs";

const CFG = {
  end: 7200,
  land: 3500, // the rosette pins on, Jarvis smiles
  prep: 2500, // the rosette leaves; its flight takes 1000 ms
  swap: 3750,
  face: 5100,
  // [dx, dy, offset] of the eyes over the first 3500 ms: up at the kites, across, then down
  // and left for the rosette.
  look: [
    [-8, -6, 0],
    [-6, -5, 0.2],
    [8, -6, 0.4],
    [9, -4, 0.55],
    [-9, 2, 0.75],
    [-9, 3, 1],
  ],
};

export const END_MS = CFG.end;

// The only bright event is the burst of sparkles where the rosette lands. There are no floor
// props in this scene, so nothing to fit to a short stage; the kites are kept above the floor.

export function play(host, opts) {
  return playScene(IndependenceScene, host, opts);
}

class IndependenceScene extends Scene {
  constructor(host, opts) {
    super(host, opts, CFG);
  }

  // Where the kites fly follows the page as it stands; they are drawn once, at the start
  // (inner), so a later relayout does not move them.
  extra(_b, geo) {
    return { kites: kiteSpots(geo) };
  }

  px(geo) {
    const { p } = this;
    return `${independenceStyle(p)}${defsSVG(p)}${skyWashSVG(
      p,
      SKY_STOPS,
      geo.W,
      geo.skyPx,
      SKY_STOPS_DARK
    )}`;
  }

  inner(geo) {
    const { p } = this;
    const kites = geo.kites.map((k) => kiteSVG(p, k.i, geo.hu)).join("");
    return `${kites}${bitsSVG(p, geo.from, geo.to)}${this.item(
      "independence"
    )}`;
  }

  playArt() {
    this.kites();
    this.fall("tb", 93, 2400);
    this.playSky();
  }

  kites() {
    const { stage, p, geo } = this;
    const spot = (el) => geo.kites.find((k) => k.i === Number(el.dataset.i));
    stage.qa(`.${p}kite`).forEach((el) => {
      const { i, x, y } = spot(el);
      stage.animate(
        el,
        [
          { transform: tr(x + (i < 2 ? -40 : 40), geo.hu + 70), opacity: 1 },
          { transform: tr(x, y), opacity: 1 },
        ],
        {
          duration: 2400 + i * 150,
          delay: 150 + i * 220,
          easing: "cubic-bezier(.2,.7,.3,1)",
        }
      );
    });
    stage
      .qa(`.${p}ksw`)
      .forEach((el) =>
        this.sway(
          el,
          "rotate(-8deg) translateY(0)",
          "rotate(7deg) translateY(-4px)",
          2200 + Number(el.dataset.i) * 260
        )
      );
  }

  // The rosette spins in from the left and pins itself on (a copy pinned to the mark).
  rosette() {
    const at = (x, y, deg) => tr(x, y, ` rotate(${deg}deg)`);
    this.fly(
      [
        { opacity: 0, transform: at(-240, 110, -540) },
        { opacity: 1, offset: 0.1 },
        { opacity: 1, transform: at(0, 0, 0), offset: 0.97 },
        { opacity: 0, transform: at(0, 0, 0) },
      ],
      {
        duration: this.untilLand(),
        easing: "cubic-bezier(.2,.7,.3,1)",
      }
    );
  }

  // The rosette leaves and the sparkles wait where it will land, on the mark where it now is.
  prepare() {
    this.rosette();
    this.append(this.pin(10, 48, landSparklesSVG(this.p)));
    this.sparkle(this.untilLand());
  }
}
