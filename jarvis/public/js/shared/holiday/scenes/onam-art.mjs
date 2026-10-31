// The drawing of the Onam scene: brass nilavilakku lamps, a pookalam laid on the floor,
// falling petals and Maveli's olakkuda umbrella. Coordinates are stage units
// (scene-geometry.mjs: 800 wide, x centred on 400); onam.mjs scales and places the group.
import { lin, rad, r2, ring, rng } from "../stage.mjs";
import { boxRule, flameSVG, floorRow, styleSVG } from "./scene-art.mjs";

// ---- geometry (plain numbers; the page needs them before the art loads) -------------------

// The floor group as drawn (the art's own y, group scale g about x = 400): it stands on `base`; the
// lamps' finials are at `top`, the pookalam's shadow at `bottom`.
export const FLOOR_ROW = floorRow({
  base: 394,
  g: 1.15,
  top: 268.6,
  bottom: 394,
  // The lamps stand at the ends; the pookalam in the middle is laid flat, its top at y = 315.
  core: 315,
});

// The olakkuda floats down onto the mark. In the mark's 56-unit tile space it starts `dx`
// right of and `rise` above where it ends, and its top is `top` above the mark's top once
// there (the finial). The start height is cut back when the mark is near the top of the
// stage, but never below `minRise`, and `gap` tile units are always kept above it.
export const UMBRELLA = { dx: 60, rise: 80, minRise: 20, top: -13, gap: 4 };

/** How high the umbrella starts above the mark, for a mark whose top is `mt` units down. */
export const riseFor = (mt, k) =>
  Math.max(
    UMBRELLA.minRise,
    Math.min(UMBRELLA.rise, mt / k + UMBRELLA.top - UMBRELLA.gap)
  );

// How long each glow takes to come up. None goes back down: ramps, not flashes.
export const RAMP_MS = { lamp: 600 };

// ---- the drawing ------------------------------------------------------------------------------

export const onamStyle = (p) =>
  styleSVG(
    p,
    `${boxRule(p, "50% 50%", "pet")}
${boxRule(p, "50% 100%", "nlf")}`
  );

export const defsSVG = (p) => `<defs>
  ${lin(`${p}brass`, [
    [0, "#fde68a"],
    [0.5, "#f59e0b"],
    [1, "#92400e"],
  ])}
  ${lin(
    `${p}brassV`,
    [
      [0, "#92400e"],
      [0.5, "#fde68a"],
      [1, "#b45309"],
    ],
    1,
    0
  )}
  ${rad(`${p}nglow`, [
    [0, "#fde68a", 0.8],
    [0.5, "#fbbf24", 0.3],
    [1, "#fbbf24", 0],
  ])}
  ${lin(
    `${p}nfl`,
    [
      [0, "#fbbf24"],
      [0.55, "#f97316"],
      [1, "#ea580c"],
    ],
    0,
    1
  )}
</defs>`;

/** A brass nilavilakku standing on the floor at `x`, with four small flames. */
export function lampSVG(p, x) {
  const flames = [-18, -9, 9, 18]
    .map(
      (fx) =>
        `<g transform="translate(${fx} -77) scale(.55) translate(-16.5 2)"><g class="${p}nlf"><g class="${p}flick">${flameSVG(
          `${p}nfl`
        )}</g></g></g>`
    )
    .join("");
  const bands = [-20, -38, -56]
    .map((y) => `<ellipse cx="0" cy="${y}" rx="5.5" ry="2.2" fill="#d97706"/>`)
    .join("");
  return `<g transform="translate(${x} 372)">
    <circle class="${p}nlg" cx="0" cy="-86" r="38" fill="url(#${p}nglow)" opacity="0"/>
    <ellipse cx="0" cy="2" rx="24" ry="3" fill="#000" opacity=".12"/>
    <path d="M-20 0 Q-20 -7 -8 -9 H8 Q20 -7 20 0 Z" fill="url(#${p}brass)"/>
    <rect x="-2.6" y="-74" width="5.2" height="66" fill="url(#${p}brassV)"/>
    ${bands}
    <path d="M-24 -74 Q0 -64 24 -74 Q17 -82 0 -82 Q-17 -82 -24 -74 Z" fill="url(#${p}brass)"/>
    <path d="M-3 -82 L0 -100 L3 -82 Z" fill="#d97706"/><circle cx="0" cy="-101" r="2.4" fill="#fbbf24"/>
    ${flames}</g>`;
}

// Nine rings of flowers, outermost first. The pookalam is drawn as a circle and laid flat on
// the floor (squashed).
const RINGS = [
  [150, "#ea580c"],
  [134, "#facc15"],
  [118, "#16a34a"],
  [100, "#fef3c7"],
  [82, "#be123c"],
  [62, "#f97316"],
  [42, "#facc15"],
  [22, "#fef3c7"],
  [10, "#be123c"],
];

export function pookalamSVG(p) {
  const rings = RINGS.map(
    (
      [r, c],
      i
    ) => `<g class="${p}pring" opacity="0"><circle r="${r}" fill="${c}"/>
    ${
      i === 0
        ? ring(
            36,
            0,
            0,
            r,
            (x, y) => `<circle cx="${x}" cy="${y}" r="9" fill="${c}"/>`
          )
        : ""
    }
    ${
      i === 3
        ? ring(
            16,
            0,
            0,
            r - 9,
            (x, y, k) =>
              `<ellipse cx="${x}" cy="${y}" rx="6.5" ry="12" fill="#be123c" transform="rotate(${
                k * 22.5 + 90
              } ${x} ${y})"/>`
          )
        : ""
    }
    ${
      i === 5
        ? ring(
            12,
            0,
            0,
            r - 11,
            (x, y, k) =>
              `<ellipse cx="${x}" cy="${y}" rx="5" ry="9" fill="#fde047" transform="rotate(${
                k * 30 + 90
              } ${x} ${y})"/>`
          )
        : ""
    }</g>`
  ).join("");
  return `<ellipse cx="400" cy="356" rx="160" ry="38" fill="#000" opacity=".06"/>
    <g transform="translate(400 350) scale(1 .22)">${rings}</g>`;
}

/**
 * The floor props: the pookalam between two lamps (`middle` false: the lamps alone).
 * `hold(x, lamp)` wraps a lamp so it keeps its place at its end of the stage while the row
 * shrinks (Scene.edge): the lamps are the tall props that must stay out from under the
 * sub-line, which the shrinking row would otherwise draw them in to.
 */
export const floorProps = (p, middle = true, hold = (_x, lamp) => lamp) =>
  `${middle ? pookalamSVG(p) : ""}${[150, 650]
    .map((x) => hold(x, lampSVG(p, x)))
    .join("")}`;

/** Thirty-six petals above the top edge, spread over the stage's width [from, to]. */
export function petalsSVG(p, from, to) {
  const rand = rng(61);
  const colours = ["#f97316", "#facc15", "#ffffff", "#be123c", "#fb923c"];
  return Array.from({ length: 36 }, (_, i) => {
    const white = i % 5 === 2;
    return `<ellipse class="${p}pet" cx="${r2(
      from + rand() * (to - from)
    )}" cy="${r2(-10 - rand() * 60)}" rx="1.7" ry="3.2" fill="${
      colours[i % 5]
    }" stroke="${white ? "#d6d3d1" : "none"}" stroke-width=".5" opacity="0"/>`;
  }).join("");
}
