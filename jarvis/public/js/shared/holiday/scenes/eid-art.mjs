// The drawing of the Eid scene: moonrise, twinkling stars and floating lanterns. Coordinates
// are stage units (scene-geometry.mjs: 800 wide, x centred on 400); eid.mjs scales and
// places the group. The crescent that lowers onto Jarvis is the worn item
// (scene-art.mjs itemSVG). The two low outer lanterns are the floor row.
import { lin, rad, rng } from "../stage.mjs";
import { CLEAR } from "../scene-geometry.mjs";
import { sparkleSVG, styleSVG } from "./scene-art.mjs";

export const MOON = { x: 652, y: 86, r: 34, glow: 80, rise: 134 };
export const LANTERN = { string: 60, glowR: 20, drop: 32 };

// [x, y] of each lantern, y either the height listed here (moved with the mark) or measured from
// the ground. `low` ones are in the floor row: they hang down into the lower band.
const SPOTS = [
  { x: 110, low: 135 },
  { x: 215, y: 172 },
  { x: 592, y: 196 },
  { x: 700, low: 78 },
  { x: 760, y: 150 },
];
export const LOW = [0, 3];

// The floor row: the string's top (faded in) down to the lowest lantern's glow.
// The two low lanterns hang at the ends of the stage: nothing stands under the hero (mid: null).
export const FLOOR_ROW = {
  up: 135 + LANTERN.string,
  rise: 78 - LANTERN.drop,
  mid: null,
};

/** The scene's style element: rules for its own classes, so nothing leaks into the page. */
export const eidStyle = (p) =>
  styleSVG(p, `.${p}lsw{transform-box:fill-box;transform-origin:50% 0%}`);

export const defsSVG = (p) => `<defs>
  ${lin(`${p}moonG`, [
    [0, "#fef3c7"],
    [0.5, "#fcd34d"],
    [1, "#d97706"],
  ])}
  ${rad(`${p}moonGlow`, [
    [0, "#fde68a", 0.55],
    [1, "#fde68a", 0],
  ])}
  ${rad(`${p}lglow`, [
    [0, "#fde68a", 0.7],
    [1, "#fde68a", 0],
  ])}
  <linearGradient id="${p}lstr" gradientUnits="userSpaceOnUse" x1="0" y1="-60" x2="0" y2="-2"><stop offset="0" stop-color="#d97706" stop-opacity="0"/><stop offset="1" stop-color="#d97706"/></linearGradient>
  <mask id="${p}moonCut"><rect x="-60" y="-60" width="120" height="120" fill="#fff"/><circle cx="15" cy="-10" r="29" fill="#000"/></mask>
</defs>`;

/** Where the moon stops: under the clearance line, and as high as the mark allows. */
export const moonY = ({ shift }) => Math.max(MOON.y + shift, CLEAR + MOON.r);

export const moonSVG = (p) =>
  `<g class="${p}moon" opacity="0"><circle r="${MOON.glow}" fill="url(#${p}moonGlow)"/><circle r="${MOON.r}" fill="url(#${p}moonG)" mask="url(#${p}moonCut)"/></g>`;

/** Sixteen stars in the sky, kept out of the column over the mark and inside the air. */
export function starsSVG(p, { shift }) {
  const rand = rng(41);
  const squeeze = Math.max(0.3, (170 + shift) / 170);
  const stars = Array.from({ length: 16 }, () => {
    let x;
    let y;
    do {
      x = 40 + rand() * 720;
      y = 20 + rand() * 170;
    } while (x > 300 && x < 500 && y > 40);
    return sparkleSVG(
      p,
      x,
      20 + (y - 20) * squeeze,
      0.3 + rand() * 0.35,
      rand() > 0.3 ? "#fbbf24" : "#a5b4fc"
    );
  });
  return `<g class="${p}stars">${stars.join("")}</g>`;
}

/** Where lantern i hangs (its origin): the low pair from the ground, the rest by the mark. */
export function lanternSpot(i, { shift, ground }) {
  const s = SPOTS[i];
  if (s.low) return [s.x, ground - s.low];
  return [s.x, Math.max(s.y + shift, CLEAR + LANTERN.string)];
}

const lanternSVG = (
  p,
  i
) => `<g class="${p}lan ${p}lan${i}" opacity="0"><path d="M0 -60 V-2" stroke="url(#${p}lstr)" stroke-width="1"/><g class="${p}lsw">
  <circle cx="0" cy="12" r="${LANTERN.glowR}" fill="url(#${p}lglow)"/>
  <circle cx="0" cy="-2" r="1.8" fill="none" stroke="#d97706" stroke-width="1.2"/>
  <path d="M-6.5 5 Q0 -3 6.5 5 Z" fill="#d97706"/>
  <path d="M-7 5 H7 L9 15 L5 24 H-5 L-9 15 Z" fill="#92400e"/>
  <path d="M-4.6 7.4 H4.6 L6.4 15 L3.3 22 H-3.3 L-6.4 15 Z" fill="#fde68a"/>
  <path d="M0 7.4 V22 M-6.4 15 H6.4" stroke="#b45309" stroke-width="1"/>
  <path d="M-5 24 H5 L0 30 Z" fill="#d97706"/></g></g>`;

/** The three lanterns by the mark (`air`) and the two low ones (`low`) that make the floor row. */
export const lanternsSVG = (p) => ({
  air: [1, 2, 4].map((i) => lanternSVG(p, i)).join(""),
  low: LOW.map((i) => lanternSVG(p, i)).join(""),
});
