// The Onam scene (desktop stage layout): brass nilavilakku lamps light, a pookalam blooms,
// petals fall, and Maveli's olakkuda umbrella drifts down into Jarvis's hold. Contract and
// beats: scene-runtime.mjs.
import { tr } from "../stage.mjs";
import { Scene, playScene } from "./scene-runtime.mjs";
import { landSparklesSVG } from "./scene-art.mjs";
import {
  FLOOR_ROW,
  RAMP_MS,
  UMBRELLA,
  defsSVG,
  floorProps,
  onamStyle,
  petalsSVG,
  riseFor,
} from "./onam-art.mjs";

const CFG = {
  end: 7200,
  land: 3400, // the umbrella settles, Jarvis smiles
  prep: 2100, // the umbrella leaves; its flight takes 1300 ms
  swap: 3650,
  face: 5000,
  // [dx, dy, offset] of the eyes over the first 3400 ms: down at the lamps, along the
  // pookalam, then up to catch the umbrella.
  look: [
    [0, 4, 0],
    [-8, 4, 0.15],
    [8, 4, 0.35],
    [-9, 0, 0.5],
    [0, -6, 0.7],
    [0, -7, 1],
  ],
  row: FLOOR_ROW,
  foldMid: true, // the row's middle group goes if a relayout leaves the hero no room for it
};

export const END_MS = CFG.end;

// The only bright event is the burst of sparkles where the umbrella lands. The lamp glows
// come up over RAMP_MS.lamp (onam-art.mjs) and stay.

export function play(host, opts) {
  return playScene(OnamScene, host, opts);
}

class OnamScene extends Scene {
  constructor(host, opts) {
    super(host, opts, CFG);
  }

  // The umbrella starts lower when the mark is near the top of the stage.
  extra(_b, geo) {
    return { rise: riseFor(geo.mt, geo.k) };
  }

  px() {
    return `${onamStyle(this.p)}${defsSVG(this.p)}`;
  }

  inner(geo) {
    const { p } = this;
    // The pookalam is left out when the hero leaves it no room even at the row's smallest.
    const floor = this.opts.replay
      ? ""
      : this.floorGroup(
          this.onGround(
            floorProps(p, !geo.floor.tight, (x, lamp) => this.edge(x, lamp))
          )
        );
    return `${floor}${this.item("onam", { crownOnly: true })}${petalsSVG(
      p,
      geo.from,
      geo.to
    )}`;
  }

  playArt() {
    this.lamps();
    this.pookalam();
    this.fall("pet", 67, 2800);
  }

  lamps() {
    const { stage, p } = this;
    this.pop("nlf", { peak: 1.15, ms: 380, delay: 200, step: 130 });
    stage.qa(`.${p}nlg`).forEach((g, i) =>
      stage.animate(g, [{ opacity: 0 }, { opacity: 1 }], {
        duration: RAMP_MS.lamp,
        delay: 400 + i * 500,
      })
    );
  }

  // The rings bloom from the middle outward.
  pookalam() {
    const { stage, p } = this;
    const rings = stage.qa(`.${p}pring`);
    rings.forEach((g, i) =>
      stage.animate(
        g,
        [
          { opacity: 0, transform: "scale(.2)" },
          { opacity: 1, transform: "scale(1.06)", offset: 0.75 },
          { opacity: 1, transform: "scale(1)" },
        ],
        {
          duration: 520,
          delay: 600 + (rings.length - 1 - i) * 150,
          easing: "ease-out",
        }
      )
    );
  }

  // Maveli's umbrella drifts down like a leaf, swaying, and settles in Jarvis's hold. The
  // flying copy is pinned to the mark, so it ends on it wherever the page puts it.
  umbrella() {
    const f = this.geo.rise / UMBRELLA.rise;
    const at = (x, y, deg) => tr(x, y * f, ` rotate(${deg}deg)`);
    this.fly(
      [
        {
          opacity: 0,
          transform: at(UMBRELLA.dx, -UMBRELLA.rise, -18),
          offset: 0,
        },
        { opacity: 1, offset: 0.12 },
        { transform: at(30, -48, 12), offset: 0.4, easing: "ease-in-out" },
        { transform: at(8, -18, -6), offset: 0.7, easing: "ease-in-out" },
        { opacity: 1, transform: at(0, 0, 0), offset: 0.97 },
        { opacity: 0, transform: at(0, 0, 0) },
      ],
      { duration: this.untilLand() }
    );
  }

  // The umbrella leaves and the sparkles wait where it will land, on the mark where it now is.
  prepare() {
    this.umbrella();
    this.append(this.pin(28, 3, landSparklesSVG(this.p)));
    this.sparkle(this.untilLand());
  }
}
