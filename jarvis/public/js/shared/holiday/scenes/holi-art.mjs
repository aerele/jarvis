// The drawing of the Holi scene: pastel dawn wash, Google-style paint splats with drips
// round the edges, and the handful of gulal that lands on Jarvis's cheeks. Coordinates are
// stage units (scene-geometry.mjs: 800 wide, x centred on 400); holi.mjs scales and places
// the group.
import { rad, r2, rng } from "../stage.mjs";
import { clearOfHero, edgeX } from "./scene-place.mjs";
import { boxRule, floorRow, styleSVG } from "./scene-art.mjs";

// ---- geometry (plain numbers) ------------------------------------------------------------

// The floor group: the low splats sit on the stage bottom. They are drawn at the art's
// own y (base 398, scale 1); the splat bodies bleed off the bottom edge on purpose, so the
// group's extent for fitting stops at the baseline.
export const FLOOR_ROW = floorRow({
  base: 398,
  g: 1,
  top: 270,
  bottom: 398,
  core: null, // the splats are round the edges, clear of the greeting
});

// [x, y, radius, colour, seed, drip length]: round the edges, clear of the greeting.
export const SPLATS = [
  [64, 72, 46, "#ec4899", 1, 40],
  [196, 40, 32, "#facc15", 2, 0],
  [330, 26, 24, "#22c55e", 3, 0],
  [516, 34, 28, "#06b6d4", 4, 26],
  [680, 62, 44, "#a855f7", 5, 46],
  [762, 196, 36, "#f97316", 6, 0],
  [706, 334, 46, "#ec4899", 7, 0],
  [546, 372, 34, "#facc15", 8, 0],
  [296, 372, 38, "#3b82f6", 9, 0],
  [108, 336, 46, "#22c55e", 10, 0],
  [40, 200, 34, "#06b6d4", 11, 30],
  [150, 214, 20, "#f97316", 12, 0],
  [664, 262, 20, "#22c55e", 13, 0],
];

// How far a splat reaches from its centre, in radii: the body (up to 1.34), the flecks
// round it (to 2.0 plus their own size) and the squash overshoot on landing.
export const REACH = 2.1;

// The splats build up one after another and stay until the scene's own fade: they are never
// a flash. Splat i starts at `first + i * every` and takes `ms` to land (scale .1 to 1.16
// to 1, opacity straight to .92 and held).
export const BUILD = {
  first: 150,
  every: 165,
  ms: 280,
  held: 0.92,
  fadeFrom: 0.8,
};

/** Which band of the stage a splat belongs to, by the y it is listed at. */
export const bandOf = (y) => (y < 120 ? "top" : y >= 300 ? "low" : "side");

// A side splat keeps its place relative to the greeting: above it, it moves with the mark;
// below the greeting the band is stretched down to the stage bottom.
const sideY = (y, dyM, shift) =>
  y <= 230 ? y + dyM : 230 + dyM + ((y - 230) * (170 + shift - dyM)) / 170;

/**
 * Where every splat goes on this stage. geo: {from, to, mt, ground, keep}. Each splat keeps
 * its distance from its own edge of the stage and is pushed sideways out of the hero's way
 * (heroKeepOut); a splat with no room left is left out. `y` is where it lands; `drawY` is
 * the y to draw it at (the low band is drawn at the art's own y inside the floor group).
 */
export function placeSplats(geo) {
  const shift = geo.ground - FLOOR_ROW.base;
  const dyM = geo.mt - 84;
  return SPLATS.flatMap((def, i) => {
    const [x0, y0, r] = def;
    const band = bandOf(y0);
    const y =
      band === "top" ? y0 : band === "low" ? y0 + shift : sideY(y0, dyM, shift);
    const hw = r * REACH;
    const x = clearOfHero(
      { x: edgeX(x0, geo, hw), y, hw, hh: hw },
      geo.keep,
      geo.from,
      geo.to
    );
    if (x === null) return [];
    return [{ i, def, band, x, y, drawY: band === "low" ? y0 : y }];
  });
}

// ---- the drawing ------------------------------------------------------------------------------

export const holiStyle = (p) =>
  styleSVG(
    p,
    `${boxRule(p, "50% 50%", "splat")}\n${boxRule(p, "50% 0%", "hdrip")}`
  );

export const SKY_STOPS = [
  [0, "#f9a8d4", 0.35],
  [0.3, "#fde68a", 0.2],
  [0.52, "#a5f3fc", 0],
];

// The same wash in the dark theme: the pastels would go grey over the dark page, so it is
// the holiday's own colours (a pink and violet tint) at a lower alpha.
export const SKY_STOPS_DARK = [
  [0, "#ec4899", 0.34],
  [0.3, "#7c3aed", 0.22],
  [0.52, "#7c3aed", 0],
];

export const defsSVG = (p) => `<defs>
  <filter id="${p}soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2"/></filter>
  ${rad(`${p}ballG`, [
    [0, "#f9a8d4", 0.9],
    [1, "#ec4899", 0],
  ])}
</defs>`;

/** One paint splat (a lumpy blob, flecks, streaks) and, when it has one, its drip. */
function splatSVG(p, i, [cx, cy, r, c, seed, drip]) {
  const rand = rng(seed);
  const n = 18;
  const pts = [];
  for (let k = 0; k < n; k++) {
    const a = (k / n) * Math.PI * 2;
    const rr = r * (k % 2 ? 0.74 + 0.22 * rand() : 0.96 + 0.34 * rand());
    pts.push([cx + Math.cos(a) * rr, cy + Math.sin(a) * rr]);
  }
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  let d = `M${mid(pts[n - 1], pts[0])
    .map(r2)
    .join(" ")}`;
  for (let k = 0; k < n; k++) {
    d += ` Q${pts[k].map(r2).join(" ")} ${mid(pts[k], pts[(k + 1) % n])
      .map(r2)
      .join(" ")}`;
  }
  const drops = Array.from({ length: 9 }, () => {
    const a = rand() * Math.PI * 2;
    const dist = r * (1.25 + 0.75 * rand());
    return `<circle cx="${r2(cx + Math.cos(a) * dist)}" cy="${r2(
      cy + Math.sin(a) * dist
    )}" r="${r2(r * (0.05 + 0.11 * rand()))}"/>`;
  }).join("");
  const streaks = Array.from({ length: 3 }, () => {
    const a = rand() * Math.PI * 2;
    const x = r2(cx + Math.cos(a) * r * 1.08);
    const y = r2(cy + Math.sin(a) * r * 1.08);
    return `<ellipse cx="${x}" cy="${y}" rx="${r2(r * 0.08)}" ry="${r2(
      r * 0.3
    )}" transform="rotate(${r2((a * 180) / Math.PI + 90)} ${x} ${y})"/>`;
  }).join("");
  const dx = r2(cx + (rand() - 0.5) * r * 0.6);
  const dy = r2(cy + r * 0.55);
  const w = r2(Math.max(2.4, r * 0.1));
  const hang = drip
    ? `<path class="${p}hdrip" data-i="${i}" d="M${r2(dx - w)} ${dy} V${r2(
        dy + drip
      )} a${w} ${w} 0 0 0 ${r2(w * 2)} 0 V${dy} Z" fill="${c}" opacity="0"/>`
    : "";
  return `${hang}<g class="${p}splat" data-i="${i}" fill="${c}" opacity="0"><path d="${d}"/>${drops}${streaks}</g>`;
}

/**
 * The splats of `placed`, in a group the scene fades out at its end. `wrap(x, markup)` may
 * wrap each splat (the floor row's holds its edge splats in place as the row shrinks).
 */
export const splatsSVG = (p, placed, wrap = (_x, splat) => splat) =>
  `<g class="${p}splats">${placed
    .map((s) => wrap(s.x, splatSVG(p, s.i, [s.x, s.drawY, ...s.def.slice(2)])))
    .join("")}</g>`;

/** The handful of gulal, thrown from the lower left. Moved by holi.mjs. */
export const ballSVG = (p) =>
  `<g class="${p}ball" opacity="0"><circle r="10" fill="url(#${p}ballG)" filter="url(#${p}soft)"/><circle r="5" fill="#ec4899"/><circle cx="-1.5" cy="-1.6" r="1.6" fill="#fbcfe8"/></g>`;

export const SPARK_COLOURS = ["#ec4899", "#facc15", "#f472b6", "#22c55e"];
