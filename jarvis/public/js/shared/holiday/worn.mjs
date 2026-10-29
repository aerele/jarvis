// The worn item on the Jarvis mark: one small SVG per design, ported from the
// approved holiday design (the ACC drawings, the LOOK_AT glances and the reaction
// keyframes). Do not redraw them here; a change to the art is a change to the
// approved design first.
//
// LAZY: this module carries all the art, so it is only ever loaded with a dynamic
// import() when a mark is about to wear something (frontend/src/lib/holidayWorn.js).
// The words and the answer reader live in copy.mjs and answer.mjs and stay in the
// main bundle. No DOM access at import time.
//
// Everything is drawn in the 56-unit tile space the approved design uses. The host
// puts the markup in an <svg viewBox="-16 -16 88 88" overflow:visible> over the
// mark, so items may hang past the tile. SVG ids get an idPrefix so several marks
// on one page never share a gradient or clip id.

const STAR = "M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z";
const SHADOW = "#1e1b4b";

// ---------- drawing helpers (all in the 56-unit tile space) ----------
const r2 = (n) => Math.round(n * 100) / 100;
function rng(seed) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
// An organic blob (powder burst): jittered radius, smoothed with quadratic midpoints.
function blob(cx, cy, r, seed, n = 11) {
  const rand = rng(seed),
    p = [];
  for (let i = 0; i < n; i++) {
    const a = (i / n) * Math.PI * 2,
      rr = r * (0.7 + 0.45 * rand());
    p.push([cx + Math.cos(a) * rr, cy + Math.sin(a) * rr]);
  }
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  let d = `M${mid(p[n - 1], p[0])
    .map(r2)
    .join(" ")}`;
  for (let i = 0; i < n; i++)
    d += ` Q${p[i].map(r2).join(" ")} ${mid(p[i], p[(i + 1) % n])
      .map(r2)
      .join(" ")}`;
  return d + "Z";
}
function specks(cx, cy, from, to, r0, r1, colour, seed, count) {
  const rand = rng(seed);
  let s = "";
  for (let i = 0; i < count; i++) {
    const a = ((from + (to - from) * rand()) * Math.PI) / 180,
      d = r0 + (r1 - r0) * rand();
    s += `<circle cx="${r2(cx + Math.cos(a) * d)}" cy="${r2(
      cy + Math.sin(a) * d
    )}" r="${r2(0.6 + rand() * 1.1)}" fill="${colour}"/>`;
  }
  return s;
}
const ring = (n, cx, cy, rad, fn, offset = 0) =>
  Array.from({ length: n }, (_, i) => {
    const a = (((i / n) * 360 + offset) * Math.PI) / 180;
    return fn(r2(cx + Math.cos(a) * rad), r2(cy + Math.sin(a) * rad), i);
  }).join("");
const lin = (id, stops, x2 = 1, y2 = 1, x1 = 0, y1 = 0) =>
  `<linearGradient id="${id}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}">${stops
    .map(
      ([o, c, op = 1]) =>
        `<stop offset="${o}" stop-color="${c}" stop-opacity="${op}"/>`
    )
    .join("")}</linearGradient>`;
const rad = (id, stops) =>
  `<radialGradient id="${id}">${stops
    .map(
      ([o, c, op = 1]) =>
        `<stop offset="${o}" stop-color="${c}" stop-opacity="${op}"/>`
    )
    .join("")}</radialGradient>`;
// A gentle pendulum sway about a pivot, only when motion is allowed.
const sway = (o, cx, cy, deg, dur, begin = 0) =>
  o.motion
    ? `<animateTransform attributeName="transform" type="rotate" values="${-deg} ${cx} ${cy};${deg} ${cx} ${cy};${-deg} ${cx} ${cy}" dur="${dur}s" begin="${begin}s" repeatCount="indefinite" calcMode="spline" keySplines=".45 0 .55 1;.45 0 .55 1"/>`
    : "";
// Contact shadow on the tile, clipped to it.
const contact = (o, shape) =>
  o.noShadow
    ? ""
    : `<g clip-path="url(#t${o.id})" fill="${SHADOW}" opacity=".2">${shape}</g>`;

// ---------- the art set. Each returns SVG for the .acc group. ----------
const ACC = {
  christmas: (o) => `<defs>
      ${lin(`hat${o.id}`, [
        [0, "#fb7185"],
        [0.5, "#e11d48"],
        [1, "#9f1239"],
      ])}
      ${lin(
        `fur${o.id}`,
        [
          [0, "#ffffff"],
          [1, "#dcdde8"],
        ],
        0,
        1
      )}
    </defs>
    ${contact(
      o,
      `<ellipse cx="37" cy="11.5" rx="17" ry="4.6" transform="rotate(18 37 11.5)"/>`
    )}
    <g transform="translate(39 3) rotate(18)"><g class="tip">
      <path d="M-14 2 C-13 -12 -4 -23 8 -24 C17 -24.5 23 -18 24 -8 C24.5 -4 24 0 23 3 L18.5 3 C18.5 -3 17 -8 13.5 -11 C14.5 -6 14.8 -2 14.5 2 Z" fill="url(#hat${
        o.id
      })"/>
      <path d="M13.5 -11 C17 -8 18.5 -3 18.5 3 L23 3 C24 0 24.5 -4 24 -8 C23.5 -12 21 -16 17 -17 C16 -14 15 -12 13.5 -11 Z" fill="#881337" opacity=".5"/>
      ${
        o.small
          ? ""
          : `<path d="M-10 -2 C-9 -10 -3 -17 4 -19.5" stroke="#fff" stroke-opacity=".35" stroke-width="2" fill="none" stroke-linecap="round"/>`
      }
      <rect x="-17" y="-1" width="34" height="9" rx="4.5" fill="url(#fur${
        o.id
      })"/>
      ${
        o.small
          ? ""
          : [-12, -6, 0, 6, 12]
              .map((x) => `<circle cx="${x}" cy="0" r="2.7" fill="#fff"/>`)
              .join("")
      }
      <g>${sway(o, 23, 3, 6, 2.6)}
        <circle cx="22.5" cy="6" r="5" fill="url(#fur${o.id})"/>
        <circle cx="21" cy="4.4" r="1.7" fill="#fff"/>
      </g>
    </g></g>`,

  // The confetti lives in .pop: hidden at rest, it bursts only when Jarvis smiles.
  newyear: (o) => {
    const teeth = Array.from({ length: 10 }, (_, i) => {
      const x = -9.45 + i * 2.1,
        y = 3 + 1.75 * (1 - (x / 10) ** 2);
      return `<path d="M${r2(x - 1.05)} ${r2(y)} L${r2(x + 1.05)} ${r2(
        y
      )} L${r2(x)} ${r2(y + 2.8)} Z"/>`;
    }).join("");
    const tuft = Array.from(
      { length: 8 },
      (_, k) =>
        `<ellipse cx="0" cy="-24.4" rx="1.15" ry="3" fill="${
          k % 2 ? "#fff" : "#fde047"
        }" transform="rotate(${k * 45} 0 -21)"/>`
    ).join("");
    const confetti =
      [
        [60, 8, -30, "#f472b6"],
        [63, 30, 45, "#4ade80"],
        [58, 50, 15, "#fde047"],
        [-9, 40, -20, "#38bdf8"],
        [30, -12, 30, "#fde047"],
      ]
        .map(
          ([x, y, r, c]) =>
            `<rect x="${x}" y="${y}" width="3" height="6.4" rx="1" fill="${c}" transform="rotate(${r} ${
              x + 1.5
            } ${y + 3.2})"/>`
        )
        .join("") +
      `<circle cx="66" cy="18" r="1.6" fill="#38bdf8"/><circle cx="-7" cy="54" r="1.5" fill="#f472b6"/><circle cx="48" cy="64" r="1.5" fill="#4ade80"/><circle cx="44" cy="-8" r="1.5" fill="#f472b6"/>`;
    return `<defs>
        ${lin(`cone${o.id}`, [
          [0, "#f9a8d4"],
          [1, "#db2777"],
        ])}
        <clipPath id="cc${
          o.id
        }"><path d="M-10 3 L0 -21 L10 3 Q0 6.5 -10 3 Z"/></clipPath>
      </defs>
      ${contact(
        o,
        `<ellipse cx="15" cy="13" rx="11" ry="3.4" transform="rotate(-16 15 13)"/>`
      )}
      <g transform="translate(14 9) rotate(-16) scale(.92)"><g class="hop">
        <path d="M-10 3 L0 -21 L10 3 Q0 6.5 -10 3 Z" fill="url(#cone${o.id})"/>
        <g clip-path="url(#cc${o.id})">
          <g stroke="#fde047" stroke-width="2.6"><path d="M-14 -4 L10 -16"/><path d="M-14 4 L14 -10"/>${
            o.small ? "" : `<path d="M-10 10 L14 -2"/>`
          }</g>
          <path d="M0 -21 L12 3 L12 8 L0 8 Z" fill="#831843" opacity=".2"/>
        </g>
        <g fill="#fbbf24">${teeth}</g>
        ${tuft}<circle cx="0" cy="-21" r="1.8" fill="#f59e0b"/>
      </g></g>${o.small ? "" : `<g class="pop">${confetti}</g>`}`;
  },

  diwali: (o) => `<defs>
      ${rad(`glow${o.id}`, [
        [0, "#fde68a", 0.95],
        [0.45, "#fbbf24", 0.35],
        [1, "#fbbf24", 0],
      ])}
      ${lin(
        `clay${o.id}`,
        [
          [0, "#f97316"],
          [0.55, "#c2410c"],
          [1, "#7c2d12"],
        ],
        0,
        1
      )}
      ${lin(
        `fl${o.id}`,
        [
          [0, "#fbbf24"],
          [0.55, "#f97316"],
          [1, "#ea580c"],
        ],
        0,
        1
      )}
    </defs>
    <circle class="glow" cx="60.5" cy="36" r="15" fill="url(#glow${o.id})"/>
    ${contact(o, `<ellipse cx="44" cy="61" rx="17" ry="3"/>`)}
    <path d="M28 46 Q29 58 44 59 Q55 59 59 49 L63 44.5 Q57 46.5 52 46.5 Z" fill="url(#clay${
      o.id
    })"/>
    <path d="M28 46 Q40 43.2 52 46.5 Q40 49 28 46 Z" fill="#431407"/>
    <path d="M32 46.1 Q40 45 47 46.3" stroke="#fbbf24" stroke-opacity=".6" stroke-width="1" fill="none" stroke-linecap="round"/>
    ${
      o.small
        ? ""
        : `<path d="M30.6 51 Q44 55.5 57.4 50.6" stroke="#fde68a" stroke-width="1.4" fill="none"/>
      ${[
        [35, 53.4],
        [41, 54.8],
        [47, 54.8],
        [53, 53.2],
      ]
        .map(([x, y]) => `<circle cx="${x}" cy="${y}" r="1.1" fill="#fff"/>`)
        .join("")}`
    }
    <path d="M60.5 42.5 V45" stroke="#451a03" stroke-width="1.2" stroke-linecap="round"/>
    <g class="flare"><g class="flicker">
      <path d="M60.5 26 C66 33 67 38.5 64.5 42 C63 44 58 44 56.5 42 C54.5 38.5 56 33 60.5 26 Z" fill="url(#fl${
        o.id
      })"/>
      <path d="M60.5 31 C63.8 35.5 64.3 39 62.8 41.2 C61.8 42.6 59.2 42.6 58.2 41.2 C56.7 39 57.2 35.5 60.5 31 Z" fill="#fde047"/>
      <path d="M60.5 35.5 C62.2 38 62.3 40 61.5 41.2 C61 41.9 60 41.9 59.5 41.2 C58.7 40 58.8 38 60.5 35.5 Z" fill="#fffbeb"/>
    </g></g>`,

  // Just the pot now: no sugarcane competing with it. Steam bubbles pop on the smile.
  pongal: (o) => `<defs>${lin(`pot${o.id}`, [
    [0, "#f97316"],
    [0.5, "#c2410c"],
    [1, "#7c2d12"],
  ])}</defs>
    ${contact(o, `<ellipse cx="45" cy="62.5" rx="13" ry="2.6"/>`)}
    <path d="M33.5 47 C30 52 31 59.5 37 61.5 Q44 63.5 51 61.5 C57 59.5 58 52 54.5 47 Z" fill="url(#pot${
      o.id
    })"/>
    ${
      o.small
        ? ""
        : `<path d="M33.2 52 Q36.6 50 40 52 T47 52 T54.6 52" stroke="#fff" stroke-width=".9" fill="none" opacity=".9"/>
      ${[37.5, 41, 44.5, 48, 51.5]
        .map(
          (x) =>
            `<circle cx="${x}" cy="55.6" r=".85" fill="#fff" opacity=".9"/>`
        )
        .join("")}`
    }
    <path d="M36.5 47.5 L37.4 44 H50.6 L51.5 47.5 Z" fill="#9a3412"/>
    ${
      o.small
        ? ""
        : `<path d="M37.5 45.6 C33 46.5 29.5 50 29 54 C33 52.5 36 50 38 47.2 Z" fill="#15803d"/>
      <path d="M50.5 45.6 C55 46 58.5 49 59.5 53 C55.5 52 52.5 49.5 50 47.2 Z" fill="#16a34a"/>
      <path d="M37 45.7 H51" stroke="#facc15" stroke-width="1"/>`
    }
    <rect x="34.5" y="41.5" width="19" height="3.4" rx="1.7" fill="#ea580c"/>
    <rect x="35.5" y="41.8" width="17" height="1" rx=".5" fill="#fdba74" opacity=".8"/>
    <g class="puff">
      <circle cx="38" cy="41.2" r="3" fill="#fff"/><circle cx="42.5" cy="39.6" r="3.7" fill="#fff"/>
      <circle cx="47.2" cy="39.9" r="3.3" fill="#fff"/><circle cx="51" cy="41.4" r="2.6" fill="#fff"/>
      ${
        o.small
          ? ""
          : `<path d="M37.2 42 Q36.8 46.6 38 47.1 Q39.2 46.6 38.8 42 Z" fill="#fff"/>
        <path d="M51.4 42 Q51.6 45 52.5 45.4 Q53.3 45 52.9 42 Z" fill="#fff"/>
        <circle cx="41" cy="38.6" r="1" fill="#e5e7eb"/><circle cx="46" cy="38.8" r=".8" fill="#e5e7eb"/>`
      }
    </g>
    ${
      o.small
        ? ""
        : `<g class="pop">${[
            [40, 32, 1.7],
            [45.5, 29, 2.1],
            [50.5, 32, 1.5],
            [42.5, 25, 1.3],
            [48.5, 24, 1.4],
          ]
            .map(
              ([x, y, r]) =>
                `<circle cx="${x}" cy="${y}" r="${r}" fill="#fff" stroke="#d6d3d1" stroke-width=".5"/>`
            )
            .join("")}</g>`
    }`,

  // Just the crescent and the spark-star: the lantern is gone.
  eid: (o) => `<defs>
      ${lin(`gold${o.id}`, [
        [0, "#fef3c7"],
        [0.5, "#fcd34d"],
        [1, "#d97706"],
      ])}
      ${rad(`eg${o.id}`, [
        [0, "#fde68a", 0.6],
        [1, "#fde68a", 0],
      ])}
      <linearGradient id="str${
        o.id
      }" gradientUnits="userSpaceOnUse" x1="0" y1="-16" x2="0" y2="-3"><stop offset="0" stop-color="#fde68a" stop-opacity="0"/><stop offset="1" stop-color="#fde68a"/></linearGradient>
      <mask id="cr${
        o.id
      }"><rect x="20" y="-10" width="40" height="40" fill="#fff"/><circle cx="44.5" cy="3.5" r="8" fill="#000"/></mask>
    </defs>
    <g class="swing">
      <g>${sway(o, 40, -16, 3, 4.2)}
        <path d="M40 -16 V-3.5" stroke="url(#str${o.id})" stroke-width=".9"/>
        ${
          o.small
            ? ""
            : `<circle cx="40" cy="6" r="15" fill="url(#eg${o.id})"/>`
        }
        <g transform="${
          o.small ? "translate(40 6) scale(1.35) translate(-40 -6)" : ""
        }"><circle cx="40" cy="6" r="9.5" fill="url(#gold${
    o.id
  })" mask="url(#cr${o.id})"/></g>
      </g>
      <g>${sway(o, 53, -16, 4, 3.4, 0.6)}
        <path d="M53 -16 V3" stroke="url(#str${o.id})" stroke-width=".8"/>
        <path d="${STAR}" fill="url(#gold${
    o.id
  })" transform="translate(53 8.5) scale(.46) translate(-12 -12)"/>
      </g>
    </g>`,

  // Dressed for Onam: Maveli's golden olakkuda (palm-leaf umbrella) held over
  // the head, its shaft resting on the shoulder, and a cream kasavu with its gold
  // border. `crownOnly` draws just the umbrella (the scene floats it down alone).
  onam: (o) => {
    // Propped on the right shoulder: the canopy sits over the right of the head and
    // slopes down from left to right; the shaft runs down to the hand at (42, 45).
    const ribs = o.small
      ? ""
      : Array.from({ length: 9 }, (_, i) => {
          const x = -22 + i * 5.5;
          return `<path d="M0 -1.5 L${r2(x)} ${r2(
            11.6 + Math.abs(x) * 0.02
          )}"/>`;
        }).join("");
    const beads = o.small
      ? ""
      : Array.from({ length: 9 }, (_, i) => {
          const x = -20 + i * 5;
          return `<circle cx="${x}" cy="${r2(
            14.6 - Math.abs(x) * 0.03
          )}" r=".9" fill="#b45309"/>`;
        }).join("");
    const umbrella = `<g class="twirl">
      <path d="M50.5 5.6 L42.1 45" stroke="#78350f" stroke-width="${
        o.small ? 2.4 : 1.8
      }" stroke-linecap="round"/>
      <g transform="translate(53 -6) rotate(12) scale(.85)">
        <path d="M-24 12 Q0 -6 24 12 Q12 15.6 0 15.6 Q-12 15.6 -24 12 Z" fill="url(#um${
          o.id
        })"/>
        <g stroke="#92400e" stroke-opacity=".45" stroke-width=".7">${ribs}</g>
        <path d="M-24 12 Q0 17.4 24 12" stroke="#78350f" stroke-width="${
          o.small ? 2.2 : 1.6
        }" fill="none" stroke-linecap="round"/>
        ${beads}
        <path d="M-1.6 -2 L0 -7 L1.6 -2 Z" fill="#b45309"/><circle cx="0" cy="-1.6" r="1.8" fill="#d97706"/>
      </g></g>`;
    if (o.crownOnly)
      return `<defs>${lin(
        `um${o.id}`,
        [
          [0, "#fef3c7"],
          [0.5, "#fbbf24"],
          [1, "#b45309"],
        ],
        0,
        1
      )}</defs>${umbrella}`;
    const kasavu = `<g clip-path="url(#t${o.id})">
      <path d="M-2 45 Q28 54 58 45 V58 H-2 Z" fill="url(#kv${o.id})"/>
      ${
        o.small
          ? ""
          : `<path d="M13 51.6 Q14.4 55 13.8 58 M43 51.6 Q41.6 55 42.2 58 M28 54 V58" stroke="#e6dcc3" stroke-width="1" fill="none"/>`
      }
      <path d="M-2 45 Q28 54 58 45" stroke="url(#kz${o.id})" stroke-width="${
      o.small ? 4 : 3.4
    }" fill="none"/>
      ${
        o.small
          ? ""
          : `<path d="M-2 48.7 Q28 57.7 58 48.7" stroke="#d4a017" stroke-width=".9" fill="none"/>`
      }
    </g>`;
    return `<defs>
        ${lin(
          `kv${o.id}`,
          [
            [0, "#fffdf5"],
            [1, "#f1e6cc"],
          ],
          0,
          1
        )}
        ${lin(
          `kz${o.id}`,
          [
            [0, "#f7d774"],
            [0.5, "#c9971c"],
            [1, "#f7d774"],
          ],
          1,
          0
        )}
        ${lin(
          `um${o.id}`,
          [
            [0, "#fef3c7"],
            [0.5, "#fbbf24"],
            [1, "#b45309"],
          ],
          0,
          1
        )}
      </defs>
      ${contact(
        o,
        `<path d="M-2 43.4 Q28 52.4 58 43.4" stroke="${SHADOW}" stroke-width="2.2" fill="none"/>`
      )}
      ${contact(
        o,
        `<ellipse cx="44" cy="9" rx="22" ry="7" opacity=".55" transform="rotate(12 44 9)"/>`
      )}
      ${kasavu}
      ${umbrella}`;
  },

  // Birthday: a little frosted cake held at the side, one candle. On the smile
  // the flame goes out (.bflame) and a curl of smoke rises (.smoke).
  birthday: (o) => `<defs>
      ${lin(
        `ck${o.id}`,
        [
          [0, "#fef3c7"],
          [1, "#f5c27a"],
        ],
        0,
        1
      )}
      ${lin(
        `cf${o.id}`,
        [
          [0, "#fbcfe8"],
          [1, "#f472b6"],
        ],
        0,
        1
      )}
      ${lin(
        `bfl${o.id}`,
        [
          [0, "#fbbf24"],
          [0.55, "#f97316"],
          [1, "#ea580c"],
        ],
        0,
        1
      )}
    </defs>
    ${contact(o, `<ellipse cx="47" cy="61.5" rx="15" ry="2.6"/>`)}
    <ellipse cx="47" cy="59.4" rx="14.5" ry="2.8" fill="#e5e7eb"/>
    <rect x="35" y="46" width="24" height="12.5" rx="2.4" fill="url(#ck${
      o.id
    })"/>
    ${
      o.small
        ? ""
        : `<path d="M35 52.4 H59" stroke="#f472b6" stroke-opacity=".55" stroke-width="1.2"/>`
    }
    <path d="M35 48 Q35 44.5 38 44.5 H56 Q59 44.5 59 48 V49.4 Q57.4 52.6 55.8 49.6 Q54 47.6 52.4 50.6 Q50.6 53 48.8 49.8 Q47 47.4 45.2 50.4 Q43.4 53.4 41.6 50 Q39.8 47.6 38 50.4 Q36.4 52.4 35 49.6 Z" fill="url(#cf${
      o.id
    })"/>
    ${
      o.small
        ? ""
        : [
            [39, 47.2, "#38bdf8"],
            [43.4, 46.4, "#facc15"],
            [51, 46.6, "#a855f7"],
            [55.4, 47.3, "#22c55e"],
          ]
            .map(
              ([x, y, c]) =>
                `<rect x="${x}" y="${y}" width="2.2" height=".9" rx=".45" fill="${c}" transform="rotate(25 ${
                  x + 1.1
                } ${y + 0.45})"/>`
            )
            .join("")
    }
    <rect x="46" y="35.4" width="2.6" height="9.4" rx=".9" fill="#fff"/>
    ${
      o.small
        ? ""
        : `<path d="M46 37.6 L48.6 36.2 M46 40.6 L48.6 39.2 M46 43.6 L48.6 42.2" stroke="#ec4899" stroke-width=".9"/>`
    }
    <g class="smoke" opacity="0"><path d="M47.3 33 q-2 -3 0 -6 q2 -3 0 -6" stroke="#cbd5e1" stroke-width="1.3" fill="none" stroke-linecap="round"/></g>
    <g class="bflame"><g class="flicker">
      <path d="M47.3 27 C50.4 30.6 50.8 33.4 49.3 35 C48.4 35.9 46.2 35.9 45.3 35 C43.8 33.4 44.2 30.6 47.3 27 Z" fill="url(#bfl${
        o.id
      })"/>
      <path d="M47.3 30.6 C48.6 32.2 48.7 33.6 48 34.4 C47.6 34.8 47 34.8 46.6 34.4 C45.9 33.6 46 32.2 47.3 30.6 Z" fill="#fef9c3"/>
    </g></g>`,

  // Powder on the tile; the flecks only fly when Jarvis smiles.
  holi: (o) => `<g clip-path="url(#t${o.id})">
      <path d="${blob(52, 4, 12, 7)}" fill="#22c55e"/><path d="${blob(
    51,
    5,
    6.5,
    8
  )}" fill="#86efac" opacity=".6"/>
      <path d="${blob(3, 53, 11, 3)}" fill="#f97316"/><path d="${blob(
    4,
    52,
    6,
    4
  )}" fill="#fdba74" opacity=".6"/>
      <path d="${blob(57, 52, 6.5, 11)}" fill="#06b6d4"/>
    </g>
    ${
      o.small
        ? ""
        : `<g class="pop">${
            specks(52, 4, -100, 10, 12, 15.5, "#22c55e", 21, 6) +
            specks(3, 53, 80, 190, 11, 14.5, "#f97316", 22, 6) +
            specks(57, 52, -10, 80, 7, 9.5, "#06b6d4", 23, 3)
          }</g>`
    }`,

  independence: (o) => `${contact(o, `<circle cx="11" cy="50.5" r="11"/>`)}
    <g class="wiggle">
    <path d="M7.5 49 L3.5 64 L7 62 L9.2 65.5 L11 50 Z" fill="#ff9933"/>
    <path d="M12.5 49 L16.5 64 L13 62 L10.8 65.5 L9 50 Z" fill="#138808"/>
    <circle cx="10" cy="48" r="9" fill="#ff9933"/>
    ${
      o.small
        ? ""
        : ring(
            16,
            10,
            48,
            9,
            (cx, cy, i) =>
              `<circle cx="${cx}" cy="${cy}" r="2.3" fill="${
                i % 2 ? "#ff9933" : "#f08a24"
              }"/>`
          )
    }
    <circle cx="10" cy="48" r="6.6" fill="#fff"/>
    ${
      o.small
        ? ""
        : ring(
            12,
            10,
            48,
            5.5,
            (cx, cy) =>
              `<path d="M${cx} ${cy} L10 48" stroke="#e5e7eb" stroke-width=".6"/>`
          )
    }
    <circle cx="10" cy="48" r="4.4" fill="#138808"/>
    <circle cx="10" cy="48" r="2.1" fill="#1e3a8a"/>
    <circle cx="9.2" cy="47.2" r=".7" fill="#fff" opacity=".6"/>
    </g>`,
};
// The designs that have art: the keys of ACC, so a design cannot be added to one list
// and not the other. A test holds them equal to the answer reader's own list.
export const DESIGNS = Object.keys(ACC);

// Where each item sits, as the face offset that looks at it (tile units). While
// the face shows, the eyes glance around and then settle on the item. Holi has
// no entry: its gulal is on the face itself, so the default glance stays.
export const LOOK_AT = {
  christmas: [6, -7],
  newyear: [-8, -7],
  diwali: [9, 3],
  pongal: [8, 3],
  eid: [6, -7],
  onam: [4, -7],
  independence: [-9, 3],
  birthday: [9, 3],
};

// Holi's gulal sits on the face itself, so it glances with the eyes.
function cheeks(design, o) {
  if (design !== "holi") return "";
  return `<defs>${rad(`bp${o.id}`, [
    [0, "#ec4899", 0.95],
    [0.55, "#ec4899", 0.6],
    [1, "#ec4899", 0],
  ])}
    ${rad(`by${o.id}`, [
      [0, "#facc15", 0.95],
      [0.55, "#facc15", 0.6],
      [1, "#facc15", 0],
    ])}</defs>
    <g class="cheeks"><circle class="cheek" cx="14.5" cy="38" r="7" fill="url(#bp${
      o.id
    })"/><circle class="cheek" cx="41.5" cy="38" r="7" fill="url(#by${
    o.id
  })"/></g>`;
}

/**
 * The SVG children for one worn item, or "" for a design with no art.
 * opts: idPrefix (unique per mark), radius (tile corner, in 56-unit space, for
 * the clip the contact shadow and powder use), small (drop fine detail, for
 * marks of 32px or less), motion (false under reduced motion: no SMIL sway).
 * The scenes fly a copy of the item over the page: noShadow leaves out the contact shadow
 * (it belongs on the tile, not on a flying copy), crownOnly (Onam) draws the umbrella alone,
 * without the kasavu.
 */
export function wornMarkup(
  design,
  {
    idPrefix,
    radius = 14,
    small = false,
    motion = true,
    noShadow = false,
    crownOnly = false,
  } = {}
) {
  if (!Object.prototype.hasOwnProperty.call(ACC, design)) return "";
  const o = { id: idPrefix, small, motion, noShadow, crownOnly };
  return `<defs><clipPath id="t${
    o.id
  }"><rect width="56" height="56" rx="${radius}"/></clipPath></defs>
    ${cheeks(design, o)}<g class="acc">${ACC[design](o)}</g>`;
}

// Percent offsets for the HTML face, which JarvisMark moves with a % transform
// (its size is the face's size, so 1 tile unit is 100/56 percent).
const pct = (n) => `${Math.round((n / 56) * 10000) / 100}%`;

function lookRules() {
  return Object.entries(LOOK_AT)
    .map(
      ([k, [x, y]]) => `
@keyframes jvh-look-${k} { 0%, 100% { transform: translate(0, 0); } 14% { transform: translate(-16.07%, -8.04%); } 32% { transform: translate(17.86%, -3.93%); } 50%, 84% { transform: translate(${pct(
        x
      )}, ${pct(y)}); } }
@keyframes jvh-svglook-${k} { 0%, 100% { transform: translate(0, 0); } 14% { transform: translate(-9px, -4.5px); } 32% { transform: translate(10px, -2.2px); } 50%, 84% { transform: translate(${x}px, ${y}px); } }
.jvh[data-design="${k}"] .jv-mood-thinking .jv-face,
.jvh[data-design="${k}"] .jv-peek-on .jv-face,
.jvh[data-design="${k}"] .jv-hoverpeek:hover .jv-face { animation-name: jvh-look-${k}; }
.jvh-layer[data-design="${k}"][data-state="peek"] .cheeks,
.jvh-layer[data-design="${k}"][data-state="thinking"] .cheeks { animation-name: jvh-svglook-${k}; }
@media (prefers-reduced-motion: reduce) {
  .jvh[data-design="${k}"] .jv-peek-on .jv-face, .jvh[data-design="${k}"] .jv-hoverpeek:hover .jv-face { transform: translate(${pct(
        x
      )}, ${pct(y)}); }
}`
    )
    .join("");
}

// Reactions: when the mark smiles (data-state="happy" on the layer) the item
// acts. Ported from the approved design, where the layer was called `.jm`.
const REACTION_CSS = `
.jvh-layer .acc { transform-box: fill-box; transform-origin: 50% 100%; }
.jvh-layer .tip { transform-box: fill-box; transform-origin: 15% 100%; }
.jvh-layer .hop, .jvh-layer .flare, .jvh-layer .puff, .jvh-layer .bflame, .jvh-layer .smoke, .jvh-layer .flicker { transform-box: fill-box; transform-origin: 50% 100%; }
.jvh-layer .swing { transform-box: fill-box; transform-origin: 50% 0%; }
.jvh-layer .twirl { transform-box: fill-box; transform-origin: 28% 100%; }
.jvh-layer .wiggle, .jvh-layer .pop, .jvh-layer .cheek { transform-box: fill-box; transform-origin: 50% 50%; }
.jvh-layer .pop { opacity: 0; }
.jvh-layer .cheeks { animation-duration: 3.4s; animation-timing-function: ease-in-out; animation-iteration-count: infinite; }
.jvh-layer[data-state="peek"] .cheeks, .jvh-layer[data-state="thinking"] .cheeks { animation-name: jvh-look; }
.jvh-layer[data-state="happy"] .cheeks { animation: jvh-cheer 1.5s ease-in-out 1 both; }
.jvh-layer .flicker { animation: jvh-flicker 1.9s ease-in-out infinite; }
.jvh-layer .glow { animation: jvh-glow 2.4s ease-in-out infinite; }
.jvh-layer[data-state="happy"] .twirl { animation: jvh-twirl 1.3s ease-in-out 1 both; }
.jvh-layer[data-state="happy"] .tip { animation: jvh-tip 1.2s ease-in-out 1 both; }
.jvh-layer[data-state="happy"] .hop { animation: jvh-hop 1s ease-in-out 1 both; }
.jvh-layer[data-state="happy"] .flare { animation: jvh-flare 1s ease-out 1 both; }
.jvh-layer[data-state="happy"] .swing { animation: jvh-swing 1.4s ease-in-out 1 both; }
.jvh-layer[data-state="happy"] .wiggle { animation: jvh-wiggle 1s ease-in-out 1 both; }
.jvh-layer[data-state="happy"] .cheek { animation: jvh-blush 1.4s ease-in-out 1 both; }
.jvh-layer[data-state="happy"] .puff { animation: jvh-puff 1s ease-out 1 both; }
.jvh-layer[data-state="happy"] .pop { animation: jvh-pop 1.3s ease-out .15s 1 both; }
.jvh-layer[data-state="happy"] .bflame { animation: jvh-blowout 1.5s ease-in-out 1 both; }
.jvh-layer[data-state="happy"] .smoke { animation: jvh-smoke 1.5s ease-out 1 both; }
@keyframes jvh-look { 0%, 28%, 100% { transform: translate(-9px, -4.5px); } 52% { transform: translate(10px, -2.2px); } 70% { transform: translate(0, -6.7px); } }
@keyframes jvh-cheer { 0%, 100% { transform: translateY(1.7px); } 50% { transform: translateY(-6.7px); } }
@keyframes jvh-flicker { 0%, 100% { transform: scale(1, 1) skewX(0); opacity: 1; } 30% { transform: scale(.94, 1.05) skewX(-3deg); opacity: .92; } 60% { transform: scale(1.04, .96) skewX(2deg); opacity: 1; } }
@keyframes jvh-glow { 0%, 100% { opacity: .85; } 50% { opacity: 1; } }
@keyframes jvh-twirl { 0%, 100% { transform: rotate(0); } 30% { transform: rotate(-7deg); } 62% { transform: rotate(4deg); } }
@keyframes jvh-tip { 0%, 100% { transform: rotate(0); } 30% { transform: rotate(-14deg); } 60% { transform: rotate(6deg); } }
@keyframes jvh-hop { 0%, 50%, 100% { transform: translateY(0); } 25% { transform: translateY(-4px); } 70% { transform: translateY(-2px); } }
@keyframes jvh-flare { 0%, 100% { transform: scale(1); } 35% { transform: scale(1.35, 1.45); } }
@keyframes jvh-swing { 0%, 100% { transform: rotate(0); } 25% { transform: rotate(9deg); } 55% { transform: rotate(-6deg); } 80% { transform: rotate(3deg); } }
@keyframes jvh-wiggle { 0%, 100% { transform: rotate(0); } 25% { transform: rotate(-12deg); } 50% { transform: rotate(10deg); } 75% { transform: rotate(-5deg); } }
@keyframes jvh-blush { 0%, 100% { transform: scale(1); } 40% { transform: scale(1.3); } }
@keyframes jvh-puff { 0%, 100% { transform: scale(1); } 35% { transform: scale(1.18, 1.25); } }
@keyframes jvh-pop { 0% { opacity: 0; transform: scale(.4); } 25% { opacity: 1; } 100% { opacity: 0; transform: scale(1.25); } }
@keyframes jvh-blowout { 0% { transform: scale(1); opacity: 1; } 18% { transform: scale(.55, .3) skewX(-14deg); opacity: .4; } 26%, 82% { transform: scale(.2); opacity: 0; } 100% { transform: scale(1); opacity: 1; } }
@keyframes jvh-smoke { 0%, 20% { opacity: 0; transform: translateY(0); } 40% { opacity: .85; } 90%, 100% { opacity: 0; transform: translateY(-9px); } }
@media (prefers-reduced-motion: reduce) {
  .jvh-layer .cheeks, .jvh-layer .flicker, .jvh-layer .glow, .jvh-layer .twirl, .jvh-layer .tip, .jvh-layer .hop, .jvh-layer .flare, .jvh-layer .swing, .jvh-layer .wiggle, .jvh-layer .cheek, .jvh-layer .puff, .jvh-layer .pop, .jvh-layer .bflame, .jvh-layer .smoke { animation: none !important; }
}`;

/** The stylesheet text: item reactions plus the per-design eye glance. */
export function wornCss() {
  return REACTION_CSS + lookRules();
}

const STYLE_ID = "jv-holiday-worn-css";

/** Add the stylesheet to the document once. Safe to call on every mount. */
export function installWornCss(doc = document) {
  if (doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = wornCss();
  doc.head.appendChild(style);
}
