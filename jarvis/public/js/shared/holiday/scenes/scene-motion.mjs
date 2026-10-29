// The motion several scenes share, as methods on the scene: the sky wash, things falling,
// the landing sparkles, a toran dropping in, things popping in, a burst of sparks and a
// sway. Scene (scene-runtime.mjs) extends this; the methods use the scene's own stage, id
// prefix (`p`), geometry (`geo`) and timing (`cfg`).
import { ballistic, r2, rng, tr } from "../stage.mjs";
import { TORAN } from "../scene-geometry.mjs";
import { SPARKLE } from "./scene-art.mjs";

export class SceneMotion {
  /** The sky wash fades in at the start and out before the end. */
  playSky() {
    const { stage, p, cfg } = this;
    stage.animate(
      stage.q(`.${p}sky`),
      [
        { opacity: 0 },
        { opacity: 1, offset: 0.1 },
        { opacity: 1, offset: 0.78 },
        { opacity: 0 },
      ],
      { duration: cfg.end }
    );
  }

  /** The toran (scene-art.mjs toranSVG) drops in from above and settles with a little overshoot. */
  dropToran() {
    const { stage, p } = this;
    stage.animate(
      stage.q(`.${p}toran`),
      [
        { transform: tr(0, -52), opacity: 1 },
        { transform: tr(0, TORAN.overshoot), opacity: 1, offset: 0.7 },
        { transform: tr(0, 0), opacity: 1 },
      ],
      { duration: 850, delay: 100, easing: "ease-out" }
    );
  }

  /**
   * Things of class `cls` pop in one after another: from nothing to `peak` times their size,
   * then to their size. The first starts at `delay` ms, the next `step` ms after it.
   */
  pop(cls, { peak, ms, delay, step }) {
    const { stage, p } = this;
    stage
      .qa(`.${p}${cls}`)
      .forEach((el, i) =>
        stage.animate(
          el,
          [
            { transform: "scale(0)" },
            { transform: `scale(${peak})`, offset: 0.7 },
            { transform: "scale(1)" },
          ],
          { duration: ms, delay: delay + i * step, easing: "ease-out" }
        )
      );
  }

  /**
   * Sparks (burstSVG of class `cls`, each aimed along its data-a) thrown out and falling:
   * `speed` px per second (scaled 0.6 to 1.1 at random from `seed`), `gravity` px per second
   * squared, for `ms`, starting after `delay`.
   */
  burst(cls, { speed, gravity, ms }, delay, seed) {
    const rand = rng(seed);
    this.stage.qa(`.${this.p}${cls}`).forEach((el) => {
      const a = (Number(el.dataset.a) * Math.PI) / 180;
      const v = speed * (0.6 + rand() * 0.5);
      this.stage.animate(
        el,
        ballistic(Math.cos(a) * v, Math.sin(a) * v, gravity, ms),
        {
          duration: ms,
          delay,
          easing: "cubic-bezier(.15,.7,.3,1)",
          fill: "forwards",
        }
      );
    });
  }

  /** Sways `el` for ever: from the `rest` transform to `peak` and back, over `ms`. */
  sway(el, rest, peak, ms) {
    this.stage.animate(
      el,
      [{ transform: rest }, { transform: peak }, { transform: rest }],
      {
        duration: ms,
        iterations: Infinity,
        easing: "ease-in-out",
        fill: "none",
      }
    );
  }

  /** Light things drifting down (petals, paper bits, confetti): fall, sway and spin. */
  fall(cls, seed, window) {
    const rand = rng(seed);
    const drop = this.geo.hu + 80;
    this.stage.qa(`.${this.p}${cls}`).forEach((el) => {
      const duration = 3800 + rand() * 2000;
      const end = {
        transform: tr(
          (rand() - 0.5) * 90,
          drop,
          ` rotate(${r2((rand() - 0.5) * 900)}deg)`
        ),
        opacity: 0,
      };
      this.stage.animate(
        el,
        [
          { transform: tr(0, 0, " rotate(0deg)"), opacity: 0 },
          { opacity: 1, offset: 0.1 },
          { opacity: 1, offset: 0.85 },
          end,
        ],
        { duration, delay: rand() * window, fill: "forwards" }
      );
    });
  }

  /** The sparkles (landSparklesSVG) burst from where they are pinned. */
  sparkle(delay) {
    this.stage.qa(`.${this.p}pin .${this.p}sp`).forEach((el, i) => {
      const a = (i / SPARKLE.n) * Math.PI * 2;
      const { spread } = SPARKLE;
      this.stage.animate(
        el,
        [
          { opacity: 0, transform: tr(0, 0, " scale(.3)") },
          { opacity: 1, offset: 0.3 },
          {
            opacity: 0,
            transform: tr(
              Math.cos(a) * spread,
              Math.sin(a) * spread,
              " scale(1)"
            ),
          },
        ],
        { duration: SPARKLE.ms, delay, easing: "ease-out", fill: "forwards" }
      );
    });
  }
}
