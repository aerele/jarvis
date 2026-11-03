// The drawing of the Birthday scene: a soft pink wash, curly streamers from the top edge,
// six balloons and falling confetti. Coordinates are stage units (scene-geometry.mjs: 800
// wide, x centred on 400); birthday.mjs scales and places the group.
// The cake is the worn item (scene-art.mjs itemSVG); there is no floor row.
import { r2, rng } from "../stage.mjs";
import { CLEAR } from "../scene-geometry.mjs";
import { styleSVG } from "./scene-art.mjs";
import { clearOfHero, squeezeX } from "./scene-place.mjs";

export const SKY_STOPS = [
  [0, "#fbcfe8", 0.45],
  [0.3, "#e0e7ff", 0.25],
  [0.52, "#fdf2f8", 0],
];

// The same wash in the dark theme: the pastels would go grey over the dark page, so it is
// the holiday's own colours (a pink and violet tint) at a lower alpha.
export const SKY_STOPS_DARK = [
  [0, "#f472b6", 0.34],
  [0.3, "#8b5cf6", 0.2],
  [0.52, "#8b5cf6", 0],
];

/** The scene's style element: rules for its own classes, so nothing leaks into the page. */
export const birthdayStyle = (p) =>
  styleSVG(
    p,
    `.${p}bsw{transform-box:fill-box;transform-origin:50% 0%}
.${p}bconf{transform-box:fill-box;transform-origin:50% 50%}`
  );

// ---- streamers -------------------------------------------------------------------------

export const STREAMER = {
  xs: [40, 120, 200, 280, 520, 600, 680, 760],
  min: 28,
  curls: 6,
};
const STREAMER_COLOURS = [
  "#ec4899",
  "#38bdf8",
  "#facc15",
  "#a855f7",
  "#22c55e",
];

// The length of one curl, a quadratic from (0, 0) through (c, h / 2) to (0, h).
function curlLength(c, h) {
  let sum = 0;
  let [px, py] = [0, 0];
  for (let i = 1; i <= 24; i++) {
    const t = i / 24;
    const [x, y] = [2 * (1 - t) * t * c, 2 * (1 - t) * t * (h / 2) + t * t * h];
    sum += Math.hypot(x - px, y - py);
    [px, py] = [x, y];
  }
  return sum;
}

/**
 * The streamers: each hangs from the top edge and, however long it is listed, stops above the
 * hero (`airBottom`, never shorter than STREAMER.min). `ends` is the lowest y reached. `at(x)`
 * is where a streamer listed at x hangs on this stage (the phone squeezes them in).
 */
export function streamerPlan(airBottom, at = (x) => x) {
  const rand = rng(83);
  const cap = Math.max(STREAMER.min, airBottom);
  return STREAMER.xs.map((x0, i) => {
    const x = r2(at(x0) + (rand() - 0.5) * 16);
    const len = r2(Math.min(46 + rand() * 40, cap));
    const seg = len / STREAMER.curls;
    let d = `M${x} 0`;
    for (let k = 1; k <= STREAMER.curls; k++)
      d += ` q${k % 2 ? 7 : -7} ${r2(seg / 2)} 0 ${r2(seg)}`;
    const dash = r2(
      Array.from({ length: STREAMER.curls }, (_, k) =>
        curlLength(k % 2 ? -7 : 7, seg)
      ).reduce((a, b) => a + b, 0)
    );
    return { x, ends: len, d, dash, colour: STREAMER_COLOURS[i % 5] };
  });
}

export const streamersSVG = (p, plan) =>
  `<g class="${p}strs">${plan
    .map(
      (s) =>
        `<path class="${p}str" d="${s.d}" stroke="${s.colour}" stroke-width="3" fill="none" stroke-linecap="round" stroke-dasharray="${s.dash}" stroke-dashoffset="${s.dash}" data-len="${s.dash}"/>`
    )
    .join("")}</g>`;

// ---- balloons --------------------------------------------------------------------------

export const BALLOONS = [
  [64, "#ec4899", "#fbcfe8"],
  [138, "#38bdf8", "#e0f2fe"],
  [202, "#facc15", "#fef9c3"],
  [598, "#a855f7", "#ede9fe"],
  [664, "#22c55e", "#dcfce7"],
  [736, "#f97316", "#ffedd5"],
];
export const BALLOON = { rx: 16, ry: 20, string: 86 };
const TOPS = [150, 110, 132, 120, 160, 104];
// [balloon, share of the way down to the low line]: the outer pair and half of the middle pair
// settle lower, so the balloons fill the sides of the taller area.
const SETTLE_LOW = [
  [0, 1],
  [5, 1],
  [1, 0.5],
  [4, 0.5],
];

/**
 * Where each balloon settles (the y of its centre): the heights listed here, moved with the mark,
 * some of them down to `ground - 140`; none so high that its top passes the clearance line.
 */
export function balloonTops({ shift, ground }) {
  const tops = TOPS.map((y) => y + shift);
  const low = ground - 140;
  SETTLE_LOW.forEach(([i, share]) => {
    tops[i] += Math.max(0, low - tops[i]) * share;
  });
  return tops.map((y) => Math.max(y, CLEAR + BALLOON.ry));
}

const balloonSVG = (
  p,
  [x, c, hi]
) => `<g transform="translate(${x} 0)"><g class="${p}bal" opacity="0"><g class="${p}bsw">
    <path d="M0 22 q-4 12 0 24 q4 12 0 24 q-3 8 0 16" stroke="#94a3b8" stroke-width=".8" fill="none"/>
    <path d="M-2.6 23.4 L0 19.4 L2.6 23.4 Z" fill="${c}"/>
    <ellipse cx="0" cy="0" rx="${BALLOON.rx}" ry="${BALLOON.ry}" fill="${c}"/>
    <ellipse cx="-5.6" cy="-7" rx="4.2" ry="6.4" fill="${hi}" opacity=".55" transform="rotate(-22 -5.6 -7)"/>
  </g></g></g>`;

/** The balloons drawn: `xs` is each one's x (null: left out), from balloonXs. */
export const balloonsSVG = (p, xs = BALLOONS.map((b) => b[0])) =>
  BALLOONS.flatMap((b, i) =>
    xs[i] === null ? [] : [balloonSVG(p, [xs[i], ...b.slice(1)])]
  ).join("");

/**
 * Where each balloon hangs across the stage, or null for one left out. On a phone they are
 * squeezed in, pushed to the strip beside the hero and left out when there is no room there or
 * another balloon already hangs in it (outer ones first, so the outer pair stays); the
 * desktop's are at their listed x.
 */
export function balloonXs(geo) {
  const listed = BALLOONS.map((b) => b[0]);
  if (!(geo.squeeze < 1)) return listed;
  const tops = balloonTops(geo);
  const placed = [];
  const xs = listed.map(() => null);
  const outerFirst = listed
    .map((x, i) => i)
    .sort((i, j) => Math.abs(listed[j] - 400) - Math.abs(listed[i] - 400));
  outerFirst.forEach((i) => {
    const hw = BALLOON.rx;
    const hh = (BALLOON.ry + BALLOON.string) / 2;
    const box = {
      x: squeezeX(listed[i], geo, { hw }),
      y: tops[i] + hh - BALLOON.ry,
      hw,
      hh,
    };
    const x = clearOfHero(box, geo.keep, geo.from, geo.to);
    if (x === null || placed.some((q) => Math.abs(q - x) < 2 * hw + 2)) return;
    placed.push(x);
    xs[i] = x;
  });
  return xs;
}

// ---- confetti --------------------------------------------------------------------------

const CONFETTI = ["#f472b6", "#facc15", "#38bdf8", "#a855f7", "#22c55e"];

/** Forty paper bits above the top edge, spread over the whole stage width. */
export function confettiSVG(p, from, to) {
  const rand = rng(84);
  return Array.from(
    { length: 40 },
    (_, i) =>
      `<rect class="${p}bconf" x="${r2(from + rand() * (to - from))}" y="${r2(
        -14 - rand() * 50
      )}" width="3.4" height="7" rx="1" fill="${CONFETTI[i % 5]}" opacity="0"/>`
  ).join("");
}
