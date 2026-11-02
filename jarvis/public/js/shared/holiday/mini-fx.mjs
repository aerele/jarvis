// The mini chat's celebration: a few seconds of the holiday's particles drift down behind
// the welcome. Ported verbatim from the approved design (MINI_PARTICLE, playMiniFx); lazy art,
// it loads with the Desk art bundle and never with the widget. The target svg has viewBox
// "0 0 350 260", which is the space the numbers below are in.
import { flakeSVG } from "./scenes/christmas-art.mjs";
import { r2, rng, STAR } from "./stage.mjs";

export const PARTICLE_COUNT = 24;

const confetti = (colours, i) =>
  `<rect x="-1.6" y="-3.4" width="3.2" height="6.8" rx="1" fill="${
    colours[i % colours.length]
  }"/>`;
const petal = (colours, i) =>
  `<ellipse rx="1.8" ry="3.4" fill="${colours[i % colours.length]}"/>`;

export const MINI_PARTICLE = {
  birthday: (i) =>
    i % 3
      ? confetti(["#f472b6", "#facc15", "#38bdf8", "#a855f7"], i)
      : `<circle r="2.4" fill="${["#f472b6", "#38bdf8", "#facc15"][i % 3]}"/>`,
  christmas: () => `<g class="mfx-snow">${flakeSVG(0.5)}</g>`,
  diwali: (i) =>
    `<circle r="2.2" fill="${
      ["#fbbf24", "#f97316", "#fde047"][i % 3]
    }"/><circle r="5" fill="#fde68a" opacity=".35"/>`,
  pongal: (i) => petal(["#f97316", "#facc15", "#be123c"], i),
  eid: () =>
    `<path d="${STAR}" fill="#fbbf24" transform="scale(.42) translate(-12 -12)"/>`,
  newyear: (i) =>
    confetti(["#f472b6", "#facc15", "#38bdf8", "#4ade80", "#a855f7"], i),
  onam: (i) =>
    `<ellipse rx="1.8" ry="3.4" fill="${
      ["#f97316", "#facc15", "#ffffff", "#be123c"][i % 4]
    }" stroke="${i % 4 === 2 ? "#d6d3d1" : "none"}" stroke-width=".5"/>`,
  holi: (i) =>
    `<circle r="${2 + (i % 3)}" fill="${
      ["#ec4899", "#facc15", "#22c55e", "#06b6d4", "#a855f7"][i % 5]
    }"/>`,
  independence: (i) =>
    `<rect x="-2" y="-3" width="4" height="6" rx="1" fill="${
      ["#ff9933", "#ffffff", "#138808"][i % 3]
    }" stroke="${i % 3 === 1 ? "#cbd5e1" : "none"}" stroke-width=".6"/>`,
};

const has = (design) => Object.hasOwn(MINI_PARTICLE, design);

/** The svg children for one design's particles, "" for a design with none. */
export function particleMarkup(design, rand = rng(7)) {
  if (!has(design)) return "";
  return Array.from(
    { length: PARTICLE_COUNT },
    (_, i) =>
      `<g transform="translate(${r2(12 + rand() * 326)} ${r2(
        -12 - rand() * 40
      )})"><g class="mfx" opacity="0">${MINI_PARTICLE[design](i)}</g></g>`
  ).join("");
}

/**
 * Draw the particles into `svg` and let them fall. Returns { cancel }: stops every
 * animation and empties the svg. A design with no particle draws nothing.
 */
export function playParticles(svg, design) {
  const rand = rng(7);
  svg.innerHTML = particleMarkup(design, rand);
  const animations = [...svg.querySelectorAll(".mfx")].map((el) =>
    fall(el, rand)
  );
  return {
    cancel() {
      animations.forEach((a) => a.cancel());
      svg.innerHTML = "";
    },
  };
}

// The draw order of rand() is the approved design's: duration, drift, spin, delay.
function fall(el, rand) {
  const duration = 2600 + rand() * 1500;
  return el.animate(
    [
      { transform: "translate(0px, 0px) rotate(0deg)", opacity: 0 },
      { opacity: 0.95, offset: 0.15 },
      { opacity: 0.95, offset: 0.75 },
      {
        transform: `translate(${r2((rand() - 0.5) * 40)}px, 280px) rotate(${r2(
          (rand() - 0.5) * 400
        )}deg)`,
        opacity: 0,
      },
    ],
    { duration, delay: 200 + rand() * 900, fill: "both" }
  );
}
