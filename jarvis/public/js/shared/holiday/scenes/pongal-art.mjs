// The drawing of the Pongal scene: dawn sky, rising sun, mango-leaf thoranam, purple
// sugarcane, a self-drawing kolam and the pot on its fire. Coordinates are stage units
// (scene-geometry.mjs: 800 wide, x centred on 400); pongal.mjs scales and places the group.
// What every scene draws alike lives in scene-art.mjs.
import { lin, rad, r2, ring, rng } from "../stage.mjs";
import { GAP, toranBottom } from "../scene-geometry.mjs";
import {
  boxRule,
  floorRow,
  flameSVG,
  styleSVG,
  toranSVG as toran,
} from "./scene-art.mjs";

// ---- Pongal geometry (plain numbers; the page needs them before the art loads) -----------

// The thoranam hangs a mango leaf at every point of a swag but each fourth, which has a bloom.
const TORAN_P = { leaf: 17, bloom: 4.6 };

/** The lowest y the thoranam ever reaches (deepest leaf or bloom, plus the settle overshoot). */
export const TORAN_BOTTOM = toranBottom((k) =>
  k % 4 === 0 ? TORAN_P.bloom : TORAN_P.leaf
);

// The sun's centre sits `dx` right of the mark's centre and `dy` below the mark's top; its
// ray tips reach `r`. It starts `rise` lower and climbs. It stays beside the mark, above the
// greeting, and never above the thoranam.
export const SUN = { dx: 148, dy: 28, r: 62, rise: 218 };

/** The least y the sun's centre may have: its ray tips just below the thoranam. */
export const sunMinY = () => TORAN_BOTTOM + GAP + SUN.r;

/**
 * The floor group as drawn (the art's own y, group scale g about x = 400): it stands on `base`; the
 * tallest sugarcane leaf's control point is at `top`, the kolam's front edge at `bottom`. `core`
 * is the highest point of what stands in the middle (see floorRow).
 */
export const FLOOR_ROW = floorRow({
  base: 398,
  g: 1,
  top: 176,
  bottom: 398,
  // The canes stand at the ends; under the hero are the kolam and the hearth. The pot's froth
  // is at its highest, y = 256, as it boils over (the bubbles it throws stay below that).
  core: 256,
});

// How long each glow takes to come up. None of them goes back down: they are ramps, not
// flashes (see pongal.test.mjs).
export const RAMP_MS = { sun: 2300, hearth: 700 };

// ---- the drawing ---------------------------------------------------------------------------

// The stalk's dark end stop (#2e0a1e in the light theme) all but vanishes on the dark page, so
// the dark theme (html[data-theme="dark"]) draws the stalk from this lighter one.
const CANE_END_DARK = "#7a2f57";

export const pongalStyle = (p) =>
  styleSVG(
    p,
    `${boxRule(p, "50% 100%", "grow", "rise", "hflame")}
${boxRule(p, "50% 50%", "kdot", "kpet")}
${boxRule(p, "50% 0%", "drip")}
.${p}kdot{fill:var(--text-3,#7e7e8a)}
[data-theme="dark"] .${p}stalk{stroke:url(#${p}scaned)}`
  );

export const defsSVG = (p) => `<defs>
  ${rad(`${p}sglow`, [
    [0, "#fde68a", 0.75],
    [1, "#fde68a", 0],
  ])}
  ${rad(`${p}sdisc`, [
    [0, "#fffbeb"],
    [0.45, "#fde047"],
    [1, "#f59e0b"],
  ])}
  ${lin(
    `${p}scane`,
    [
      [0, "#9d4b73"],
      [0.5, "#5b1a3a"],
      [1, "#2e0a1e"],
    ],
    1,
    0
  )}
  ${lin(
    `${p}scaned`,
    [
      [0, "#9d4b73"],
      [0.5, "#5b1a3a"],
      [1, CANE_END_DARK],
    ],
    1,
    0
  )}
  ${lin(`${p}hpot`, [
    [0, "#f97316"],
    [0.5, "#c2410c"],
    [1, "#7c2d12"],
  ])}
  ${lin(
    `${p}hfl`,
    [
      [0, "#fbbf24"],
      [0.55, "#f97316"],
      [1, "#ea580c"],
    ],
    0,
    1
  )}
  ${rad(`${p}hglow`, [
    [0, "#fde68a", 0.85],
    [0.5, "#fb923c", 0.3],
    [1, "#fb923c", 0],
  ])}
</defs>`;

export const SKY_STOPS = [
  [0, "#fb923c", 0.5],
  [0.28, "#fde68a", 0.38],
  [0.52, "#fef3c7", 0],
];

// The same wash in the dark theme: the pastels would go grey over the dark page, so it is
// the holiday's own colours (a burnt orange and amber tint) at a lower alpha.
export const SKY_STOPS_DARK = [
  [0, "#c2410c", 0.62],
  [0.28, "#f59e0b", 0.3],
  [0.52, "#f59e0b", 0],
];

/** The rising sun: glow, twelve slow-turning rays, disc. Animated into place by pongal.mjs. */
export function sunSVG(p) {
  const rays = Array.from(
    { length: 12 },
    (_, i) =>
      `<rect x="-2.4" y="-62" width="4.8" height="16" rx="2.4" fill="#fbbf24" opacity=".85" transform="rotate(${
        i * 30
      })"/>`
  ).join("");
  return `<g class="${p}sun" opacity="0"><circle r="104" fill="url(#${p}sglow)"/><g class="${p}rays ${p}fb">${rays}</g><circle r="36" fill="url(#${p}sdisc)"/></g>`;
}

/** The mango-leaf thoranam across units [from, to]: leaves with a bloom at every fourth. */
export const toranSVG = (p, from, to) =>
  toran(p, from, to, (x, y, n, k) => {
    const odd = Math.abs(n) % 2;
    return k % 4 === 0 ? bloom(x, y, odd) : leaf(x, y, odd);
  });

const leaf = (x, y, odd) => {
  const { leaf: len } = TORAN_P;
  const w = 4.4;
  return `<path d="M${x} ${y} q${-w} ${len * 0.5} 0 ${len} q${w} ${
    -len * 0.48
  } 0 ${-len} Z" fill="${odd ? "#15803d" : "#22c55e"}"/><path d="M${x} ${
    y + 1
  } V${r2(
    y + len - 1.5
  )}" stroke="#14532d" stroke-opacity=".5" stroke-width=".6"/>`;
};

const bloom = (x, y, odd) => {
  const r = TORAN_P.bloom;
  return `<circle cx="${x}" cy="${y}" r="${r}" fill="${
    odd ? "#facc15" : "#f97316"
  }"/>${ring(
    8,
    x,
    y,
    r * 0.62,
    (cx, cy) =>
      `<circle cx="${cx}" cy="${cy}" r="${r2(r * 0.3)}" fill="${
        odd ? "#fde047" : "#fb923c"
      }"/>`
  )}<circle cx="${x}" cy="${y}" r="${r2(r * 0.3)}" fill="#c2410c"/>`;
};

// The kolam is round but laid flat on the floor, so every point is squashed vertically
// (KY). Lines are drawn in screen space so their stroke stays even and the draw-on length
// is right.
const KC = [400, 346];
const KY = 0.25;
const kpt = (r, deg) => {
  const a = (deg * Math.PI) / 180;
  return [r2(KC[0] + Math.cos(a) * r), r2(KC[1] + Math.sin(a) * r * KY)];
};

function petalPath(r0, r1, deg, half) {
  const [ax, ay] = kpt(r0, deg);
  const [bx, by] = kpt(r1, deg);
  const mid = (r0 + r1) / 2;
  const [l1x, l1y] = kpt(mid, deg - half);
  const [l2x, l2y] = kpt(mid, deg + half);
  return `M${ax} ${ay} Q${l1x} ${l1y} ${bx} ${by} Q${l2x} ${l2y} ${ax} ${ay} Z`;
}

/** Dots first, then three rings of line (class `kl`, drawn on by pongal.mjs), then colour. */
export function kolamSVG(p) {
  const dots = (n, r, off = 0) =>
    Array.from({ length: n }, (_, i) => {
      const [x, y] = kpt(r, (i / n) * 360 + off);
      return `<circle class="${p}kdot" cx="${x}" cy="${y}" r="2.4" opacity="0"/>`;
    }).join("");
  const petals = (n, r0, r1, off, half) =>
    Array.from({ length: n }, (_, i) =>
      petalPath(r0, r1, i * (360 / n) + off, half)
    );
  const inner = petals(8, 48, 100, 0, 18).join(" ");
  const outer = petals(16, 158, 205, 11.25, 9).join(" ");
  const wave =
    Array.from({ length: 145 }, (_, i) => {
      const deg = (i / 144) * 360;
      const r = 129 + 13 * Math.sin((deg * 12 * Math.PI) / 180);
      const [x, y] = kpt(r, deg);
      return `${i ? "L" : "M"}${x} ${y}`;
    }).join(" ") + " Z";
  const fill = (d, c) =>
    `<path class="${p}kpet" d="${d}" fill="${c}" opacity="0"/>`;
  const fills =
    petals(8, 52, 96, 0, 15)
      .map((d, i) => fill(d, i % 2 ? "#facc15" : "#f472b6"))
      .join("") +
    petals(16, 162, 201, 11.25, 7)
      .map((d, i) => fill(d, i % 2 ? "#22c55e" : "#fb923c"))
      .join("");
  const line = (d, c) =>
    `<path class="${p}kl" d="${d}" stroke="${c}" stroke-width="2.2" fill="none" stroke-linejoin="round"/>`;
  return `<g>${dots(8, 74, 22.5)}${dots(24, 129)}${dots(16, 181)}${fills}${line(
    inner,
    "#e11d74"
  )}${line(wave, "#f59e0b")}${line(outer, "#e11d74")}</g>`;
}

/** The pot on a brick hearth over crossed firewood, at the kolam's centre. */
export function hearthSVG(p) {
  const P = (tx, ty) => [r2(285.6 + tx * 2.6), r2(170.2 + ty * 2.6)];
  const flames = [
    [386, 1],
    [400, 1.25],
    [414, 1],
  ]
    .map(
      ([x, s]) =>
        `<g transform="translate(${x} 344) scale(${s}) translate(-16.5 2)"><g class="${p}hflame"><g class="${p}flick">${flameSVG(
          `${p}hfl`
        )}</g></g></g>`
    )
    .join("");
  const rb = rng(52);
  const bubbles = Array.from(
    { length: 16 },
    () =>
      `<circle class="${p}bub" r="${r2(
        2 + rb() * 2.6
      )}" fill="#fff" stroke="#e7e5e4" stroke-width=".8" opacity="0"/>`
  ).join("");
  const [rx, ry] = P(44, 39);
  const brick = (x) =>
    `<rect x="${x}" y="322" width="17" height="24" rx="2.5" fill="#b45309"/><rect x="${x}" y="322" width="17" height="5" rx="2" fill="#d97706"/>`;
  return `<circle class="${p}hglow" cx="400" cy="334" r="52" fill="url(#${p}hglow)" opacity="0"/>
    <ellipse cx="400" cy="349" rx="46" ry="6" fill="#000" opacity=".14"/>
    <path d="M370 346 L432 334 M370 334 L432 346" stroke="#78350f" stroke-width="4.5" stroke-linecap="round"/>
    ${brick(358)}${brick(425)}
    ${potSVG(p)}${flames}
    <g transform="translate(${rx} ${ry})">${bubbles}</g>`;
}

// The pot reuses the worn item's shape (tile units) at 2.6x.
const potSVG = (p) => `<g transform="translate(285.6 170.2) scale(2.6)">
    <path d="M33.5 47 C30 52 31 59.5 37 61.5 Q44 63.5 51 61.5 C57 59.5 58 52 54.5 47 Z" fill="url(#${p}hpot)"/>
    <path d="M33.2 52 Q36.6 50 40 52 T47 52 T54.6 52" stroke="#fff" stroke-width=".5" fill="none" opacity=".9"/>
    ${[37.5, 41, 44.5, 48, 51.5]
      .map(
        (x) => `<circle cx="${x}" cy="55.6" r=".55" fill="#fff" opacity=".9"/>`
      )
      .join("")}
    <path d="M36.5 47.5 L37.4 44 H50.6 L51.5 47.5 Z" fill="#9a3412"/>
    <path d="M37.5 45.6 C33 46.5 29.5 50 29 54 C33 52.5 36 50 38 47.2 Z" fill="#15803d"/>
    <path d="M50.5 45.6 C55 46 58.5 49 59.5 53 C55.5 52 52.5 49.5 50 47.2 Z" fill="#16a34a"/>
    <path d="M37 45.7 H51" stroke="#facc15" stroke-width=".6"/>
    <g class="${p}drip" opacity="0"><path d="M36.4 42.6 Q35.8 49.5 37.4 50.4 Q39 49.5 38.4 42.6 Z M51.2 42.6 Q51.4 47.2 52.6 47.8 Q53.8 47.2 53.2 42.6 Z M44 43 Q43.6 46 44.6 46.6 Q45.6 46 45.2 43 Z" fill="#fff" stroke="#e7e5e4" stroke-width=".3"/></g>
    <rect x="34.5" y="41.5" width="19" height="3.4" rx="1.7" fill="#ea580c"/>
    <rect x="35.5" y="41.8" width="17" height="1" rx=".5" fill="#fdba74" opacity=".8"/>
    <g class="${p}rise" fill="#fff" stroke="#d6d3d1" stroke-width=".35"><circle cx="38" cy="41.2" r="3"/><circle cx="51" cy="41.4" r="2.6"/><circle cx="47.2" cy="39.9" r="3.3"/><circle cx="42.5" cy="39.6" r="3.7"/></g>
  </g>`;

/** The froth that boils over from Jarvis's own pot (drawn at a pin on the mark). */
export function frothSVG(p) {
  const rand = rng(31);
  return Array.from(
    { length: 14 },
    () =>
      `<circle class="${p}fr ${p}fb" r="${r2(
        1.8 + rand() * 2.4
      )}" fill="#ffffff" stroke="#e7e5e4" stroke-width=".8" opacity="0"/>`
  ).join("");
}

/**
 * Sugarcane (karumbu): a purple-maroon stalk with pale cream joints and a burst of long
 * narrow leaves that arch out and droop. `x` is where it stands, `h` its height, `lean` how
 * far its tip leans.
 */
export function caneSVG(p, x, h, lean) {
  const step = 22;
  const n = Math.floor((h - 8) / step);
  const at = (t) => [r2(lean * t), r2(-h * t)];
  const joints = Array.from({ length: n }, (_, k) => {
    const [px, py] = at(((k + 1) * step) / h);
    return `<ellipse cx="${px}" cy="${py}" rx="4.8" ry="1.5" fill="#ead7c8"/><path d="M${r2(
      px - 4.4
    )} ${r2(py + 1.4)} H${r2(
      px + 4.4
    )}" stroke="#2e0a1e" stroke-opacity=".45" stroke-width=".8"/>`;
  }).join("");
  const [lx, ly] = at(1);
  const blade = ([dx, dy, lift, c]) =>
    `<path d="M${lx} ${ly} Q${r2(lx + dx * 0.45)} ${r2(ly - lift)} ${r2(
      lx + dx
    )} ${r2(ly + dy)} Q${r2(lx + dx * 0.5)} ${r2(ly - lift + 8)} ${lx} ${r2(
      ly + 3
    )} Z" fill="${c}"/>`;
  const leaves = [
    [-46, 10, 30, "#4d7c0f"],
    [-30, 26, 22, "#15803d"],
    [42, 12, 28, "#15803d"],
    [26, 30, 20, "#4d7c0f"],
    [-8, -6, 34, "#22c55e"],
    [10, -4, 30, "#16a34a"],
  ]
    .map(blade)
    .join("");
  return `<g transform="translate(${x} 364)"><g class="${p}grow">
    <path class="${p}stalk" d="M0 0 L${lx} ${ly}" stroke="url(#${p}scane)" stroke-width="8" stroke-linecap="round"/>
    <path d="M-2 -4 L${r2(lx - 2)} ${r2(
    ly + 6
  )}" stroke="#c084a8" stroke-opacity=".45" stroke-width="1.4" stroke-linecap="round"/>
    ${joints}${leaves}
  </g></g>`;
}
