// The drawing of the Christmas scene: ice-crystal snowflakes, Santa's sleigh, a decorated
// tree and a pile of gifts. Coordinates are stage units (scene-geometry.mjs: 800 wide, x
// centred on 400); christmas.mjs scales and places the group.
import { lin, rad, r2 } from "../stage.mjs";
import { floorRow, styleSVG } from "./scene-art.mjs";

// The floor row (tree and gifts, drawn at FLOOR_GROW times their own size) stands on y = 378;
// the tree's star-glow top is 160 units above that.
export const FLOOR_GROW = 1.2;
// The tree and the gifts stand at the two ends: nothing stands under the hero (core: null).
export const FLOOR_ROW = floorRow({
  base: 378,
  g: FLOOR_GROW,
  top: 218,
  core: null,
});

// How far each prop reaches sideways from its x (its shadow), before FLOOR_GROW.
const TREE_REACH = 50;
const GIFTS_REACH = 62;

// Twinkling fairy lights: how many, the shortest twinkle, and the radius of the glow drawn.
export const LIGHTS = { n: 12, periodMs: 900, glowR: 4.6 };

/** The scene's style element: rules for its own classes, so nothing leaks into the page. */
export const christmasStyle = (p) =>
  styleSVG(
    p,
    `.${p}flake{stroke:#a5b4fc;transform-box:fill-box;transform-origin:50% 50%}
[data-theme="dark"] .${p}flake{stroke:#e0e7ff}
.${p}tstar{transform-box:fill-box;transform-origin:50% 50%}
.${p}gift{transform-box:fill-box;transform-origin:50% 100%}`
  );

export const defsSVG = (p) => `<defs>
  ${lin(
    `${p}treeG`,
    [
      [0, "#16a34a"],
      [1, "#14532d"],
    ],
    0,
    1
  )}
  ${lin(`${p}starG`, [
    [0, "#fef3c7"],
    [0.5, "#fbbf24"],
    [1, "#d97706"],
  ])}
  ${rad(`${p}starGlow`, [
    [0, "#fde68a", 0.75],
    [1, "#fde68a", 0],
  ])}
  ${lin(
    `${p}giftShade`,
    [
      [0, "#fff", 0.18],
      [0.5, "#fff", 0],
      [1, "#000", 0.18],
    ],
    1,
    0
  )}
</defs>`;

// A six-armed ice crystal, each arm with two pairs of side branches.
export function flakeSVG(k) {
  const arm = (deg) =>
    `<g transform="rotate(${deg})"><path d="M0 0 V-9 M0 -4 L-2.4 -6.2 M0 -4 L2.4 -6.2 M0 -6.6 L-1.8 -8.2 M0 -6.6 L1.8 -8.2"/></g>`;
  return `<g transform="scale(${r2(
    k
  )})" fill="none" stroke-width="1.1" stroke-linecap="round">${[
    0, 60, 120, 180, 240, 300,
  ]
    .map(arm)
    .join("")}<path d="M0 -2 L1.7 -1 L1.7 1 L0 2 L-1.7 1 L-1.7 -1 Z"/></g>`;
}

const star5 = (R, r) =>
  Array.from({ length: 10 }, (_, i) => {
    const a = -Math.PI / 2 + (i * Math.PI) / 5;
    const rr = i % 2 ? r : R;
    return `${i ? "L" : "M"}${r2(Math.cos(a) * rr)} ${r2(Math.sin(a) * rr)}`;
  }).join(" ") + " Z";

// Three pine tiers, gold tinsel, baubles, fairy lights (.tl) that switch on and twinkle, a star.
function treeSVG(p, x) {
  const tiers = [
    [48, -14, -66],
    [38, -52, -100],
    [28, -88, -132],
  ]
    .map(
      ([w, base, top]) => `
    <path d="M${-w} ${base} L0 ${top} L${w} ${base} Q0 ${
        base + 7
      } ${-w} ${base} Z" fill="url(#${p}treeG)"/>
    <path d="M${-w} ${base} L0 ${top} L0 ${
        base + 5
      } Z" fill="#22c55e" opacity=".22"/>`
    )
    .join("");
  const tinsel = [
    "M-40 -24 Q-4 -30 38 -46",
    "M-30 -62 Q2 -68 30 -80",
    "M-20 -96 Q2 -102 20 -112",
  ]
    .map(
      (d) =>
        `<path d="${d}" stroke="#fbbf24" stroke-width="1.5" fill="none" stroke-linecap="round" opacity=".9"/>`
    )
    .join("");
  const baubles = [
    [-24, -24, "#dc2626"],
    [20, -32, "#f59e0b"],
    [-8, -60, "#2563eb"],
    [20, -72, "#dc2626"],
    [-14, -98, "#f59e0b"],
    [8, -110, "#2563eb"],
    [-30, -44, "#f59e0b"],
    [32, -20, "#2563eb"],
  ]
    .map(
      ([bx, by, c]) =>
        `<circle cx="${bx}" cy="${by}" r="3.8" fill="${c}"/><circle cx="${
          bx - 1.2
        }" cy="${by - 1.2}" r="1.1" fill="#fff" opacity=".7"/>`
    )
    .join("");
  const lights = [
    [-36, -18],
    [-12, -20],
    [10, -26],
    [34, -28],
    [-26, -54],
    [-2, -52],
    [24, -58],
    [-16, -80],
    [6, -86],
    [-10, -114],
    [12, -98],
    [0, -124],
  ]
    .map(
      ([lx, ly]) =>
        `<g class="${p}tl" opacity="0"><circle cx="${lx}" cy="${ly}" r="${LIGHTS.glowR}" fill="#fbbf24" opacity=".5"/><circle cx="${lx}" cy="${ly}" r="2.1" fill="#fde047"/></g>`
    )
    .join("");
  return `<g transform="translate(${x} 372)">
    <ellipse cx="0" cy="2" rx="50" ry="4" fill="#000" opacity=".12"/>
    <rect x="-6" y="-16" width="12" height="17" rx="2" fill="#78350f"/>
    ${tiers}${tinsel}${baubles}${lights}
    <g class="${p}tstar" opacity="0"><circle cx="0" cy="-138" r="16" fill="url(#${p}starGlow)"/>
      <path d="${star5(
        10,
        4.4
      )}" transform="translate(0 -138)" fill="url(#${p}starG)" stroke="#b45309" stroke-width=".6" stroke-linejoin="round"/></g>
  </g>`;
}

// A little pile of wrapped gifts; each pops in.
function giftsSVG(p, x) {
  const box = ([bx, w, h, c, lid, rib]) => `<g class="${p}gift" opacity="0">
    <rect x="${
      bx - w / 2
    }" y="${-h}" width="${w}" height="${h}" rx="2" fill="${c}"/>
    <rect x="${
      bx - w / 2
    }" y="${-h}" width="${w}" height="${h}" rx="2" fill="url(#${p}giftShade)"/>
    <rect x="${bx - w / 2 - 2}" y="${-h - 5}" width="${
    w + 4
  }" height="6.5" rx="1.6" fill="${lid}"/>
    <rect x="${bx - 2.6}" y="${-h - 5}" width="5.2" height="${
    h + 5
  }" fill="${rib}"/>
    <ellipse cx="${bx - 5.4}" cy="${
    -h - 8
  }" rx="5.4" ry="3.2" fill="none" stroke="${rib}" stroke-width="2.4" transform="rotate(-22 ${
    bx - 5.4
  } ${-h - 8})"/>
    <ellipse cx="${bx + 5.4}" cy="${
    -h - 8
  }" rx="5.4" ry="3.2" fill="none" stroke="${rib}" stroke-width="2.4" transform="rotate(22 ${
    bx + 5.4
  } ${-h - 8})"/>
    <circle cx="${bx}" cy="${-h - 6.4}" r="2.6" fill="${rib}"/></g>`;
  const boxes = [
    [-34, 30, 26, "#2563eb", "#1d4ed8", "#e5e7eb"],
    [34, 28, 22, "#16a34a", "#15803d", "#fde68a"],
    [0, 38, 38, "#dc2626", "#b91c1c", "#fbbf24"],
  ];
  return `<g transform="translate(${x} 372)">
    <ellipse cx="0" cy="2" rx="62" ry="4.4" fill="#000" opacity=".12"/>
    ${boxes.map(box).join("")}
  </g>`;
}

/**
 * The floor row's drawing: the tree bottom left, the gifts bottom right, at the art's own y.
 * `hold(x, prop)` wraps a prop so it keeps its place at its end of the stage while the row
 * shrinks (Scene.edge); a shrinking row would draw them in over the heading. `at(x, reach)`
 * is where a prop drawn for x, `reach` units wide on each side, stands on this stage (the phone
 * squeezes them into its window).
 */
export const floorSVG = (p, hold = (_x, prop) => prop, at = (x) => x) => {
  const [tree, gifts] = [at(150, TREE_REACH), at(650, GIFTS_REACH)];
  return `${hold(tree, treeSVG(p, tree))}${hold(gifts, giftsSVG(p, gifts))}`;
};

// ---- Santa's sleigh ----------------------------------------------------------------------

// The sleigh's drawn extent (its own units, before the scale), the arc it flies and the room
// it keeps. The arc dips `dip` units at the screen edges and is level over the mark.
export const SLEIGH = {
  top: -61,
  bottom: 9.6,
  left: -48,
  right: 139,
  dip: 46,
  maxK: 0.95,
  minK: 0.45,
  margin: 8, // spare room under it: the dip and the tilt near the mark stay inside this
  tilt: 3, // spare room over it: the tilted nose lifts a little near the mark
  santaX: -10, // where the hat is let go, along the sleigh
  santaY: -26,
};
SLEIGH.span = SLEIGH.bottom - SLEIGH.top;

/** The air room the sleigh needs at its smallest size (stage units). */
export const SLEIGH_MIN_ROOM =
  (SLEIGH.span + SLEIGH.tilt) * SLEIGH.minK + SLEIGH.margin;

/** Size and path of the sleigh in the room between the clearance and the top of the hero. */
export function sleighPlan({ airTop, airBottom, from, to }) {
  const room = Math.max(0, airBottom - SLEIGH.margin - airTop);
  const fit = room / (SLEIGH.span + SLEIGH.tilt);
  const k = Math.max(SLEIGH.minK, Math.min(SLEIGH.maxK, fit));
  const bottom = airBottom - SLEIGH.margin;
  return {
    k,
    dip: SLEIGH.dip * (k / SLEIGH.maxK),
    apexY: bottom - SLEIGH.bottom * k,
    half: Math.max(400 - from, to - 400) + 240,
  };
}

/** The sleigh's origin and tilt (deg) at u (0..1) along its flight, level over x = 400. */
export function sleighAt(plan, u) {
  const { half, apexY, dip } = plan;
  return [
    400 + (2 * u - 1) * half,
    apexY + dip * (1 - Math.sin(Math.PI * u)),
    ((Math.atan2(-dip * Math.PI * Math.cos(Math.PI * u), 2 * half) * 180) /
      Math.PI) *
      0.7,
  ];
}

const DEER = (
  p,
  x,
  red
) => `<g transform="translate(${x} 0)"><g class="${p}deer">
    <g stroke="#7c2d12" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" fill="none">
      <path d="M9 -17 L19 -11 L24 -13"/><path d="M6 -15 L14 -6 L18 -6"/>
      <path d="M-10 -18 L-20 -12 L-25 -13"/><path d="M-8 -15 L-16 -7 L-20 -7"/>
    </g>
    <ellipse cx="0" cy="-21" rx="15" ry="7.5" fill="url(#${p}rdb)"/>
    <ellipse cx="1" cy="-17" rx="9.5" ry="3" fill="#e7c9a0" opacity=".55"/>
    <ellipse cx="-15" cy="-24" rx="2.6" ry="1.5" fill="#fef3c7"/>
    <path d="M9 -25 Q14.5 -31 17 -36" stroke="#92400e" stroke-width="6.5" stroke-linecap="round" fill="none"/>
    <g stroke="#78350f" stroke-width="1.5" stroke-linecap="round" fill="none">
      <path d="M17 -41 L14 -49 M14 -49 L10 -52 M14 -49 L15.5 -54"/>
      <path d="M20 -41 L22 -49 M22 -49 L19 -53 M22 -49 L26 -52"/>
    </g>
    <ellipse cx="15.5" cy="-40" rx="2.6" ry="1.2" fill="#78350f" transform="rotate(-35 15.5 -40)"/>
    <ellipse cx="20" cy="-37.5" rx="5.8" ry="4.2" fill="#a16207" transform="rotate(-18 20 -37.5)"/>
    <ellipse cx="25" cy="-35.6" rx="3" ry="2.4" fill="#c2884a"/>
    ${
      red
        ? `<circle cx="27.6" cy="-35.6" r="4.6" fill="#ef4444" opacity=".28"/>`
        : ""
    }
    <circle cx="27.6" cy="-35.6" r="${red ? 2 : 1.4}" fill="${
  red ? "#ef4444" : "#292524"
}"/>
    <circle cx="19.6" cy="-39" r="1" fill="#1c1917"/>
    <path d="M-5 -28 Q-6 -21 -5 -14" stroke="#dc2626" stroke-width="2" fill="none"/>
    <circle cx="-5" cy="-13" r="1.7" fill="#fbbf24"/>
  </g></g>`;

/** The sleigh with two reindeer and Santa, at its own size (christmas.mjs scales it). */
export const sleighSVG = (p) => `<defs>
    ${lin(`${p}coat`, [
      [0, "#f87171"],
      [1, "#b91c1c"],
    ])}
    ${lin(
      `${p}sl`,
      [
        [0, "#ef4444"],
        [1, "#991b1b"],
      ],
      0,
      1
    )}
    ${lin(
      `${p}rdb`,
      [
        [0, "#b45309"],
        [1, "#78350f"],
      ],
      0,
      1
    )}
  </defs>
  <path d="M30 -22 H56 M74 -22 H100" stroke="#fbbf24" stroke-width="1.2"/>
  <path d="M12 -29 Q36 -35 60 -30" stroke="#fbbf24" stroke-width=".9" fill="none"/>
  ${DEER(p, 62, false)}${DEER(p, 106, true)}
  <path d="M-36 -24 Q-42 -42 -31 -47 Q-22 -50 -21 -40 L-21 -24 Z" fill="#a16207"/>
  <path d="M-33 -45 Q-29 -47.5 -25 -45" stroke="#713f12" stroke-width="1.4" fill="none"/>
  <rect x="-31" y="-36" width="9" height="9" rx="1" fill="#16a34a"/>
  <path d="M-26.5 -36 V-27 M-31 -31.5 H-22" stroke="#fde047" stroke-width="1.4"/>
  <ellipse cx="-11" cy="-30" rx="11.5" ry="10.5" fill="url(#${p}coat)"/>
  <path d="M-40 -26 H8 Q17 -26 19 -17 L22 -5 Q23 -1 19 0 H-35 Q-40 0 -40 -5 Z" fill="url(#${p}sl)"/>
  <path d="M-40 -23.5 H9" stroke="#fbbf24" stroke-width="2"/>
  <path d="M-28 -13 q4 -5 8 0 q4 5 8 0 q4 -5 8 0" stroke="#fde68a" stroke-width="1.3" fill="none"/>
  <path d="M18 -18 Q24 -31 32 -28 Q37 -25 34 -20 Q31 -17 28.5 -20.5" stroke="#fbbf24" stroke-width="2.8" fill="none" stroke-linecap="round"/>
  <path d="M-40 -24 Q-48 -30 -45 -36 Q-42 -40 -38.5 -36.5" stroke="#fbbf24" stroke-width="2.8" fill="none" stroke-linecap="round"/>
  <path d="M-44 8 H24 Q35 8 37 0 Q38 -5 33.5 -5.5" stroke="#fbbf24" stroke-width="2.6" fill="none" stroke-linecap="round"/>
  <path d="M-28 0 V8 M6 0 V8" stroke="#fbbf24" stroke-width="2"/>
  <path d="M-5 -33 Q4 -33 11 -29" stroke="#dc2626" stroke-width="5" stroke-linecap="round" fill="none"/>
  <circle cx="12" cy="-29" r="2.8" fill="#1f2937"/>
  <circle cx="-10" cy="-46" r="6.5" fill="#fcd9bd"/>
  <path d="M-17 -45 Q-16 -35 -9.5 -34 Q-3 -35 -3.5 -44 Q-6.5 -41.5 -10 -41.5 Q-14 -41.5 -17 -45 Z" fill="#fff"/>
  <path d="M-10 -43 q-3 -1.6 -5 .4 M-10 -43 q3 -1.6 5 .4" stroke="#e5e7eb" stroke-width="1.6" fill="none" stroke-linecap="round"/>
  <circle cx="-5.2" cy="-45.5" r="1.5" fill="#f4a3a3"/>
  <circle cx="-6.8" cy="-48.6" r=".9" fill="#1f2937"/>
  <path d="M-17 -50 Q-14 -61 -3 -59.5 Q3 -58.5 5.5 -53 L2.5 -51.5 Q0 -55.5 -4.5 -55 Q-4 -51 -4 -50 Z" fill="#dc2626"/>
  <rect x="-18" y="-52" width="15.5" height="4.6" rx="2.3" fill="#fff"/>
  <circle cx="5" cy="-52" r="2.4" fill="#fff"/>`;
