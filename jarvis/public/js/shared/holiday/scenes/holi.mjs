// The Holi scene (desktop stage layout): paint splats land one after another round the
// edges, some with drips, and a handful of gulal is thrown onto Jarvis's cheeks. Contract and
// beats: scene-runtime.mjs.
import { burstSVG, quadPath } from "../stage.mjs";
import { Scene, playScene } from "./scene-runtime.mjs";
import {
  BUILD,
  FLOOR_ROW,
  SKY_STOPS,
  SKY_STOPS_DARK,
  SPARK_COLOURS,
  ballSVG,
  defsSVG,
  holiStyle,
  placeSplats,
  splatsSVG,
} from "./holi-art.mjs";
import { skyWashSVG } from "./scene-art.mjs";

const CFG = {
  end: 7000,
  land: 3100, // the gulal reaches the cheeks
  swap: 3350,
  face: 4800,
  prep: 3100 - 800, // the gulal leaves; the mark is read again as it does
  // [dx, dy, offset] of the eyes over the first 3100 ms: up at the splats, round the
  // edges, then after the gulal.
  look: [
    [-9, -6, 0],
    [-4, -7, 0.15],
    [8, -6, 0.32],
    [9, 4, 0.5],
    [-9, 4, 0.7],
    [-7, 0, 0.88],
    [-4, 2, 1],
  ],
  row: FLOOR_ROW,
};

export const END_MS = CFG.end;

// Nothing in this scene flashes. The splats build up (BUILD in holi-art.mjs) and stay until
// the whole layer fades over the last fifth of the scene.

export function play(host, opts) {
  return playScene(HoliScene, host, opts);
}

class HoliScene extends Scene {
  constructor(host, opts) {
    super(host, opts, CFG);
  }

  // Where the splats go follows the page as it stands; they are drawn once, at the start
  // (inner), so a later relayout does not move them.
  extra(_b, geo) {
    return { splats: placeSplats(geo) };
  }

  px(geo) {
    const { p } = this;
    return `${holiStyle(p)}${defsSVG(p)}${skyWashSVG(
      p,
      SKY_STOPS,
      geo.W,
      geo.skyPx,
      SKY_STOPS_DARK
    )}`;
  }

  inner(geo) {
    const { p } = this;
    const onFloor = (s) => s.band === "low";
    const low = this.opts.replay
      ? ""
      : this.floorGroup(
          this.onGround(
            splatsSVG(p, geo.splats.filter(onFloor), (x, splat) =>
              this.edge(x, splat)
            )
          )
        );
    const air = splatsSVG(
      p,
      geo.splats.filter((s) => !onFloor(s))
    );
    return `${air}${low}${ballSVG(p)}`;
  }

  playArt() {
    this.playSky();
    this.build();
    this.fade();
  }

  // Splat after splat hits the screen, each with a little squash; some drip.
  build() {
    const { stage, p } = this;
    const start = (el) => BUILD.first + Number(el.dataset.i) * BUILD.every;
    stage.qa(`.${p}splat`).forEach((sp) =>
      stage.animate(
        sp,
        [
          { opacity: 0, transform: "scale(.1)" },
          { opacity: BUILD.held, transform: "scale(1.16)", offset: 0.55 },
          { transform: "scale(.95)", offset: 0.8 },
          { opacity: BUILD.held, transform: "scale(1)" },
        ],
        { duration: BUILD.ms, delay: start(sp), easing: "ease-out" }
      )
    );
    stage.qa(`.${p}hdrip`).forEach((d) =>
      stage.animate(
        d,
        [
          { opacity: BUILD.held, transform: "scaleY(0)" },
          { opacity: BUILD.held, transform: "scaleY(1)" },
        ],
        { duration: 1500, delay: start(d) + 220, easing: "ease-in" }
      )
    );
  }

  fade() {
    const { stage, p, cfg } = this;
    stage
      .qa(`.${p}splats`)
      .forEach((g) =>
        stage.animate(
          g,
          [
            { opacity: 1 },
            { opacity: 1, offset: BUILD.fadeFrom },
            { opacity: 0 },
          ],
          { duration: cfg.end, fill: "forwards" }
        )
      );
  }

  // A handful of gulal is thrown from the lower left and lands on Jarvis's cheeks. The
  // mark is read again as it leaves, so it lands on the cheek where the cheek now is.
  prepare() {
    const { geo, p, stage } = this;
    const [hx, hy] = this.tile(14.5, 38);
    const from = [geo.from + 110, geo.ground - 58];
    const ctrl = [geo.from + 200, geo.mt - 24];
    stage.animate(stage.q(`.${p}ball`), quadPath(from, ctrl, [hx, hy], 12), {
      duration: this.untilLand(),
      easing: "cubic-bezier(.3,.1,.6,1)",
      fill: "forwards",
    });
    this.append(this.pin(14.5, 38, burstSVG(`${p}hsp`, 22, SPARK_COLOURS, 81)));
    this.burst(
      "hsp",
      { speed: 60, gravity: 40, ms: 900 },
      this.untilLand(),
      82
    );
  }
}
