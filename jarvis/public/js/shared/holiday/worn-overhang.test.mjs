// WORN_OVERHANG (scene-geometry.mjs) says how far each design's worn item reaches above the
// tile. The page keeps the hero low enough by that table before the item is drawn, so the
// table must not fall below the real item. Here the real reach is derived from the markup:
// the item's box the way a browser's getBBox gives it (each group's box is the union of its
// children's boxes pushed through their transforms; a curve counts by its control points),
// pushed further by the worst frame of each reaction the item plays when the mark smiles
// (worn.mjs wornCss: a tip tilts, a hat hops, an umbrella twirls...).
import { test } from "node:test";
import assert from "node:assert/strict";
import { WORN_OVERHANG, wornOverhangUnits } from "./scene-geometry.mjs";
import { DESIGNS, wornCss, wornMarkup } from "./worn.mjs";

const IDENTITY = [1, 0, 0, 1, 0, 0];
const mul = (m, n) => [
  m[0] * n[0] + m[2] * n[1],
  m[1] * n[0] + m[3] * n[1],
  m[0] * n[2] + m[2] * n[3],
  m[1] * n[2] + m[3] * n[3],
  m[0] * n[4] + m[2] * n[5] + m[4],
  m[1] * n[4] + m[3] * n[5] + m[5],
];
const about = ([x, y], m) =>
  mul(mul([1, 0, 0, 1, x, y], m), [1, 0, 0, 1, -x, -y]);
const numbers = (text) =>
  text
    .split(/[\s,]+/)
    .filter(Boolean)
    .map(parseFloat);

/** A transform attribute or CSS value as one matrix. */
function matrixOf(text = "") {
  let m = IDENTITY;
  for (const [, fn, args] of text.matchAll(/(\w+)\(([^)]*)\)/g)) {
    const v = numbers(args);
    if (fn === "translate") m = mul(m, [1, 0, 0, 1, v[0], v[1] ?? 0]);
    else if (fn === "translateY") m = mul(m, [1, 0, 0, 1, 0, v[0]]);
    else if (fn === "scale") m = mul(m, [v[0], 0, 0, v[1] ?? v[0], 0, 0]);
    else if (fn === "rotate") {
      const a = (v[0] * Math.PI) / 180;
      const r = [Math.cos(a), Math.sin(a), -Math.sin(a), Math.cos(a), 0, 0];
      m = mul(m, v.length > 1 ? about([v[1], v[2]], r) : r);
    }
  }
  return m;
}

// ---- the reactions, read from the stylesheet ---------------------------------------------

const CSS = wornCss();
// class -> where it turns, as fractions of its own box: `.jvh-layer .tip { ...transform-origin: 15% 100% }`
const ORIGINS = {};
for (const [, names, x, y] of CSS.matchAll(
  /((?:\.jvh-layer \.\w+,?\s*)+)\{\s*transform-box:\s*fill-box;\s*transform-origin:\s*([\d.]+)%\s+([\d.]+)%/g
))
  for (const [, name] of names.matchAll(/\.jvh-layer \.(\w+)/g))
    ORIGINS[name] = [x / 100, y / 100];
// class -> the transforms its reaction passes through
const REACTIONS = {};
const keyframes = Object.fromEntries(
  [...CSS.matchAll(/@keyframes ([\w-]+) \{((?:[^{}]|\{[^}]*\})*)\}/g)].map(
    ([, name, body]) => [
      name,
      [...body.matchAll(/\{[^}]*transform:\s*([^;}]*)/g)].map((m) => m[1]),
    ]
  )
);
for (const [, cls, name] of CSS.matchAll(
  /data-state="happy"\] \.(\w+)\s*\{\s*animation:\s*([\w-]+)/g
))
  REACTIONS[cls] = keyframes[name] || [];

// ---- boxes ---------------------------------------------------------------------------------

const boxOf = (points) => ({
  l: Math.min(...points.map((p) => p[0])),
  r: Math.max(...points.map((p) => p[0])),
  t: Math.min(...points.map((p) => p[1])),
  b: Math.max(...points.map((p) => p[1])),
});
const through = (box, m) =>
  boxOf(
    [
      [box.l, box.t],
      [box.r, box.t],
      [box.l, box.b],
      [box.r, box.b],
    ].map(([x, y]) => [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]])
  );

/**
 * The points that bound a path: its end points and control points (absolute commands; T
 * reflects the last quadratic control point).
 */
function pathPoints(d) {
  const points = [];
  let [x, y] = [0, 0];
  let control = [0, 0];
  for (const [, cmd, args] of d.matchAll(/([MLHVCQTZ])([^MLHVCQTZ]*)/g)) {
    const n = numbers(args);
    if (cmd === "M" || cmd === "L") [x, y] = n;
    else if (cmd === "H") x = n[0];
    else if (cmd === "V") y = n[0];
    else if (cmd === "C" || cmd === "Q") {
      for (let i = 0; i < n.length; i += 2) points.push([n[i], n[i + 1]]);
      if (cmd === "Q") control = [n[0], n[1]];
      [x, y] = n.slice(-2);
    } else if (cmd === "T") {
      for (let i = 0; i < n.length; i += 2) {
        control = [2 * x - control[0], 2 * y - control[1]];
        points.push(control);
        [x, y] = [n[i], n[i + 1]];
        points.push([x, y]);
      }
    }
    points.push([x, y]);
  }
  return points;
}

function shapeBox({ tag, attrs: a }) {
  const f = (k) => Number(a[k] ?? 0);
  if (tag === "rect")
    return {
      l: f("x"),
      r: f("x") + f("width"),
      t: f("y"),
      b: f("y") + f("height"),
    };
  if (tag === "circle" || tag === "ellipse") {
    const [rx, ry] = tag === "circle" ? [f("r"), f("r")] : [f("rx"), f("ry")];
    return {
      l: f("cx") - rx,
      r: f("cx") + rx,
      t: f("cy") - ry,
      b: f("cy") + ry,
    };
  }
  return tag === "path" ? boxOf(pathPoints(a.d || "")) : null;
}

/** The markup as a tree of {tag, attrs, children}. */
function parse(markup) {
  const root = { tag: "root", attrs: {}, children: [] };
  const open = [root];
  for (const [, close, tag, rest, self] of markup.matchAll(
    /<(\/?)([a-zA-Z]+)([^>]*?)(\/?)>/g
  )) {
    if (close) {
      open.pop();
      continue;
    }
    const attrs = Object.fromEntries(
      [...rest.matchAll(/([\w:-]+)="([^"]*)"/g)].map((m) => [m[1], m[2]])
    );
    const node = { tag, attrs, children: [] };
    open.at(-1).children.push(node);
    if (!self) open.push(node);
  }
  return root;
}

const NOT_DRAWN = [
  "defs",
  "clipPath",
  "mask",
  "linearGradient",
  "radialGradient",
];

/**
 * The box of `node` in its own space, with every child at its attribute transform and at each
 * frame of the reactions its classes play.
 */
function boxOfNode(node) {
  if (NOT_DRAWN.includes(node.tag)) return null;
  if (node.tag !== "g" && node.tag !== "root") return shapeBox(node);
  const parts = node.children.flatMap((child) => {
    const own = boxOfNode(child);
    if (!own) return [];
    const base = matrixOf(child.attrs.transform);
    const boxes = [through(own, base)];
    for (const cls of (child.attrs.class || "").split(/\s+/)) {
      const [ox, oy] = ORIGINS[cls] || [0.5, 0.5];
      const origin = [
        own.l + ox * (own.r - own.l),
        own.t + oy * (own.b - own.t),
      ];
      for (const frame of REACTIONS[cls] || [])
        boxes.push(through(own, mul(base, about(origin, matrixOf(frame)))));
    }
    return boxes;
  });
  return parts.length
    ? boxOf(
        parts.flatMap((b) => [
          [b.l, b.t],
          [b.r, b.b],
        ])
      )
    : null;
}

function findAcc(node) {
  if (node.attrs.class === "acc") return node;
  for (const child of node.children) {
    const found = findAcc(child);
    if (found) return found;
  }
  return null;
}

/** How far (tile units) the design's item reaches above the tile's top, worst frame. */
function reachOf(design) {
  const acc = findAcc(parse(wornMarkup(design, { idPrefix: "x" })));
  return Math.max(0, -boxOfNode(acc).t);
}

test("the stylesheet's reactions were found, so the derivation below means something", () => {
  assert.ok(Object.keys(REACTIONS).length >= 5, Object.keys(REACTIONS).join());
  assert.deepEqual(ORIGINS.tip, [0.15, 1]);
  assert.ok(REACTIONS.tip.length >= 2);
});

test("WORN_OVERHANG is not below the item drawn, and not far above it", () => {
  for (const design of Object.keys(WORN_OVERHANG)) {
    const real = reachOf(design);
    const stated = WORN_OVERHANG[design];
    assert.ok(
      stated >= real - 0.05,
      `${design}: ${stated} is below the item, ${real}`
    );
    assert.ok(
      stated - real <= 1.5,
      `${design}: ${stated} is more than 1.5 above the item, ${real}`
    );
  }
});

test("a design with no entry wears nothing above the tile", () => {
  const without = DESIGNS.filter((d) => !Object.hasOwn(WORN_OVERHANG, d));
  assert.ok(without.length >= 3, without.join());
  for (const design of without) {
    assert.equal(reachOf(design), 0, design);
    assert.equal(wornOverhangUnits(design), 0, design);
  }
});
