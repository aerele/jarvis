// Where a scene's pieces stand, read off the fake DOM (day-scene-fakes.test-support.mjs): each
// drawn shape's box in stage units, with the transforms down the tree and, for a node an
// animation moves, the place its last translate puts it (animations fill "both", so that is
// where it stays). Paths are bounded by every point they name, control points included, so a
// box is never smaller than the drawing. Not a renderer: enough to hold every piece inside the
// phone window and clear of the hero.
const NUM = /-?\d*\.?\d+(?:e[-+]?\d+)?/gi;
const nums = (text) => (String(text).match(NUM) || []).map(Number);

const IDENTITY = [1, 0, 0, 1, 0, 0];

const mul = ([a, b, c, d, e, f], [A, B, C, D, E, F]) => [
  a * A + c * B,
  b * A + d * B,
  a * C + c * D,
  b * C + d * D,
  a * E + c * F + e,
  b * E + d * F + f,
];

const FNS = {
  translate: ([x, y = 0]) => [1, 0, 0, 1, x, y],
  scale: ([x, y = x]) => [x, 0, 0, y, 0, 0],
  rotate: ([deg, cx = 0, cy = 0]) => {
    const [cos, sin] = [
      Math.cos((deg * Math.PI) / 180),
      Math.sin((deg * Math.PI) / 180),
    ];
    return mul(mul([1, 0, 0, 1, cx, cy], [cos, sin, -sin, cos, 0, 0]), [
      1,
      0,
      0,
      1,
      -cx,
      -cy,
    ]);
  },
};

/** The matrix of an SVG transform attribute (translate, scale, rotate; the rest is identity). */
export function matrixOf(transform) {
  let m = IDENTITY;
  for (const [, name, args] of String(transform || "").matchAll(
    /(\w+)\(([^)]*)\)/g
  ))
    if (FNS[name]) m = mul(m, FNS[name](nums(args)));
  return m;
}

/** The last translate an animation of this node ends on, or null (loops are not a place). */
export function finalPlace(node) {
  const moves = node.svg.animations.filter(
    (a) => a.node === node && a.options.iterations !== Infinity
  );
  let place = null;
  for (const a of moves)
    for (const f of a.frames) {
      const t = /translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(
        f.transform || ""
      );
      if (t) place = [Number(t[1]), Number(t[2])];
    }
  return place;
}

/** Points a path names, in order, as [x, y] (relative commands resolved). */
function pathPoints(d) {
  const pts = [];
  let [x, y, sx, sy] = [0, 0, 0, 0];
  for (const [, cmd, rest] of String(d).matchAll(
    /([MmLlHhVvQqCcZz])([^MmLlHhVvQqCcZz]*)/g
  )) {
    const u = cmd.toUpperCase();
    const rel = cmd !== u;
    const v = nums(rest);
    if (u === "Z") [x, y] = [sx, sy];
    const size = { M: 2, L: 2, H: 1, V: 1, Q: 4, C: 6 }[u];
    for (let i = 0; size && i + size <= v.length; i += size) {
      if (u === "H") x = rel ? x + v[i] : v[i];
      else if (u === "V") y = rel ? y + v[i] : v[i];
      else {
        const [bx, by] = rel ? [x, y] : [0, 0];
        for (let k = 0; k < size; k += 2)
          pts.push([bx + v[i + k], by + v[i + k + 1]]);
        [x, y] = pts.at(-1);
        if (u === "M") [sx, sy] = [x, y];
        continue;
      }
      pts.push([x, y]);
    }
  }
  return pts;
}

const attr = (n, k) => Number(n.attrs[k] ?? 0);

// The shape's own box [x0, y0, x1, y1], or null for what draws nothing.
function ownBox(n) {
  if (n.tag === "rect")
    return [
      attr(n, "x"),
      attr(n, "y"),
      attr(n, "x") + attr(n, "width"),
      attr(n, "y") + attr(n, "height"),
    ];
  if (n.tag === "circle") {
    const [cx, cy, r] = [attr(n, "cx"), attr(n, "cy"), attr(n, "r")];
    return [cx - r, cy - r, cx + r, cy + r];
  }
  if (n.tag === "ellipse") {
    const [cx, cy] = [attr(n, "cx"), attr(n, "cy")];
    return [
      cx - attr(n, "rx"),
      cy - attr(n, "ry"),
      cx + attr(n, "rx"),
      cy + attr(n, "ry"),
    ];
  }
  if (n.tag === "text") {
    // Digits stand about 0.72 of the font size wide.
    const half = (n.text.length * 0.72 * attr(n, "font-size")) / 2;
    return [
      attr(n, "x") - half,
      attr(n, "y") - attr(n, "font-size"),
      attr(n, "x") + half,
      attr(n, "y"),
    ];
  }
  if (n.tag === "path") {
    const pts = pathPoints(n.attrs.d);
    if (!pts.length) return null;
    const [xs, ys] = [pts.map((p) => p[0]), pts.map((p) => p[1])];
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  }
  return null;
}

const NOT_DRAWN = new Set([
  "defs",
  "mask",
  "clipPath",
  "style",
  "filter",
  "linearGradient",
  "radialGradient",
]);

const place = (n, parent) => {
  const moved = finalPlace(n);
  return mul(
    parent,
    moved ? [1, 0, 0, 1, moved[0], moved[1]] : matrixOf(n.attrs.transform)
  );
};

/**
 * Every drawn shape under `root` as {node, box: [x0, y0, x1, y1], chain}, in the units of
 * `root`'s own space. `skip(node)` leaves a node and everything in it out. `chain` is the
 * node's ancestors' classes plus its own, for picking pieces by class.
 */
export function shapesUnder(root, skip = () => false) {
  const out = [];
  const walk = (n, parent, chain) => {
    if (NOT_DRAWN.has(n.tag) || skip(n)) return;
    const m = place(n, parent);
    const classes = [...chain, ...n.classes];
    const own = ownBox(n);
    if (own) {
      const xs = [own[0], own[2]].flatMap((x) =>
        [own[1], own[3]].map((y) => m[0] * x + m[2] * y + m[4])
      );
      const ys = [own[0], own[2]].flatMap((x) =>
        [own[1], own[3]].map((y) => m[1] * x + m[3] * y + m[5])
      );
      out.push({
        node: n,
        box: [
          Math.min(...xs),
          Math.min(...ys),
          Math.max(...xs),
          Math.max(...ys),
        ],
        chain: classes,
      });
    }
    n.children.forEach((c) => walk(c, m, classes));
  };
  root.children.forEach((c) => walk(c, IDENTITY, []));
  return out;
}
