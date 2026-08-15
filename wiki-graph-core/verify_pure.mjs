// Pure-JS verification (#47) of wiki-graph-core analysis/similarity/actions.
// Run with node20 from anywhere; the modules' bare `graphology` imports resolve
// from wiki-graph-core/node_modules (self-contained package).
import { computeSimilarity, contentSuggestions } from "./src/similarity.js";
import { analyze } from "./src/graphAnalysis.js";
import { fullAnalysis } from "./src/analysis.js";
import { computeActions } from "./src/actions.js";

let pass = 0,
  fail = 0;
const ok = (c, m) => {
  if (c) {
    pass++;
  } else {
    fail++;
    console.error("  ✗ FAIL:", m);
  }
};
const section = (t) => console.log("\n== " + t + " ==");

// ---- deterministic corpus (seeded LCG, no Math.random) ----
let _s = 123456789;
const rnd = () => (_s = (_s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
const VOCAB = (
  "invoice payment reconciliation ledger tax gst filing return credit debit " +
  "customer supplier item warehouse stock delivery order sales purchase quotation " +
  "employee payroll salary attendance leave shift expense claim asset depreciation " +
  "workflow approval notification email report dashboard chart filter export"
).split(" ");

function makePages(n, wordsPer) {
  const pages = [];
  for (let i = 0; i < n; i++) {
    const words = [];
    // each page draws from a shifting window of the vocab so overlaps vary
    const base = Math.floor(rnd() * (VOCAB.length - 8));
    for (let w = 0; w < wordsPer; w++) {
      const idx = (base + Math.floor(rnd() * 8)) % VOCAB.length;
      words.push(VOCAB[idx]);
    }
    pages.push({
      kind: "page",
      id: `page:p${i}`,
      slug: `p${i}`,
      label: `Page ${i} ${VOCAB[i % VOCAB.length]}`,
      summary: words.join(" "),
    });
  }
  return pages;
}

// ---- independent brute-force TF-IDF cosine (mirrors similarity.js formulas) ----
const STOP = new Set(
  (
    "the a an of on in to for and or but with without from by as at is are was were be been " +
    "this that these those it its i we you they he she our your their my his her no not into " +
    "over under about above below up down out off then than so if else per via etc has have " +
    "had will shall may can could should would do does did what when where which who whom why"
  ).split(" ")
);
const tok = (s) =>
  String(s || "")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length > 2 && !STOP.has(t));

function bruteSimilar(pages, topK, minSim) {
  const docs = [],
    df = new Map();
  for (const p of pages) {
    const toks = tok([p.label, p.summary].filter(Boolean).join(" "));
    if (!toks.length) continue;
    const tf = new Map();
    for (const t of toks) tf.set(t, (tf.get(t) || 0) + 1);
    for (const t of tf.keys()) df.set(t, (df.get(t) || 0) + 1);
    docs.push({ slug: p.slug, tf });
  }
  const N = docs.length;
  const vecs = docs.map((d) => {
    let norm = 0;
    const raw = new Map();
    for (const [t, c] of d.tf) {
      const idf = Math.log((N + 1) / ((df.get(t) || 0) + 1)) + 1;
      const w = (1 + Math.log(c)) * idf;
      raw.set(t, w);
      norm += w * w;
    }
    norm = Math.sqrt(norm) || 1;
    const v = new Map();
    for (const [t, w] of raw) v.set(t, w / norm);
    return v;
  });
  const out = {};
  for (let i = 0; i < N; i++) {
    const ranked = [];
    for (let j = 0; j < N; j++) {
      if (i === j) continue;
      let dot = 0;
      const [a, b] =
        vecs[i].size < vecs[j].size ? [vecs[i], vecs[j]] : [vecs[j], vecs[i]];
      for (const [t, w] of a) {
        const wj = b.get(t);
        if (wj) dot += w * wj;
      }
      if (dot >= minSim)
        ranked.push({ slug: docs[j].slug, score: Math.round(dot * 100) / 100 });
    }
    ranked.sort((x, y) => y.score - x.score || (x.slug < y.slug ? -1 : 1));
    if (ranked.length) out[docs[i].slug] = ranked.slice(0, topK);
  }
  return out;
}

const canon = (list) =>
  list
    .map((x) => `${x.slug}:${x.score}`)
    .sort()
    .join(",");

// ================= R4: inverted-index top-k == brute force =================
section("R4 — inverted-index top-k matches brute-force all-pairs cosine");
{
  const pages = makePages(60, 14);
  const topK = 6,
    minSim = 0.12;
  const { similar, docCount } = computeSimilarity(pages, { topK, minSim });
  const brute = bruteSimilar(pages, topK, minSim);
  ok(docCount === 60, `docCount 60 (got ${docCount})`);

  // same set of source slugs with suggestions
  const ks1 = Object.keys(similar).sort().join(",");
  const ks2 = Object.keys(brute).sort().join(",");
  ok(
    ks1 === ks2,
    `source-slug sets match (${Object.keys(similar).length} vs ${
      Object.keys(brute).length
    })`
  );

  // for each source, the top-k neighbor+score set matches (canonicalized to
  // drop tie-order cosmetics; the invariant is same neighbors & scores)
  let mism = 0,
    cmp = 0;
  for (const slug of Object.keys(brute)) {
    cmp++;
    // compare top-1 exactly (highest score must agree) and the k-set
    const a = similar[slug] || [];
    const b = brute[slug];
    if (canon(a) !== canon(b)) {
      // tolerate a boundary tie: same scores multiset even if slugs at the
      // cut differ — check score multiset first
      const sa = a
        .map((x) => x.score)
        .sort()
        .join(",");
      const sb = b
        .map((x) => x.score)
        .sort()
        .join(",");
      if (sa !== sb) {
        mism++;
        if (mism <= 3)
          console.error(`    slug ${slug}: inv=${canon(a)} brute=${canon(b)}`);
      }
    }
    if (a.length && b.length)
      ok(
        a[0].score >= b[0].score - 1e-9,
        `${slug} top-1 score not below brute`
      );
  }
  ok(
    mism === 0,
    `all ${cmp} sources: top-k set matches brute force (${mism} mismatched)`
  );
  console.log(
    `  compared ${cmp} sources; top1 example: ${
      Object.keys(similar)[0]
    } -> ${JSON.stringify((similar[Object.keys(similar)[0]] || [])[0])}`
  );
}

// ================= R7: edgeless graph still yields content suggestions =====
section("R7 — edgeless wiki bootstraps content suggestions");
{
  const pages = makePages(30, 14);
  const data = { nodes: pages, edges: [] }; // NO edges at all
  const sugg = contentSuggestions(pages, [], { max: 12 });
  ok(sugg.length > 0, `content suggestions on edgeless graph (${sugg.length})`);
  ok(
    sugg.every((s) => s.a && s.b && s.a !== s.b),
    "every suggestion is a valid distinct pair"
  );
  ok(
    sugg.every((s) => s.source === "content"),
    "all sourced from content (no structure available)"
  );
  // fullAnalysis merges content-first: suggestedLinks non-empty on edgeless graph
  const a = fullAnalysis(data);
  ok(
    (a.lists.suggestedLinks || []).length > 0,
    `fullAnalysis suggestedLinks non-empty edgeless (${a.lists.suggestedLinks.length})`
  );
  console.log(
    `  example: ${sugg[0].aLabel} <-> ${sugg[0].bLabel} (score ${sugg[0].score})`
  );
}

// ================= R7b: existing edges are not re-suggested ================
section("R7b — already-linked pairs are excluded from suggestions");
{
  const pages = makePages(20, 14);
  const first = contentSuggestions(pages, [], { max: 20 });
  ok(first.length > 0, "have a baseline suggestion to link");
  const top = first[0];
  const edges = [{ kind: "links-to", source: top.a, target: top.b }];
  const after = contentSuggestions(pages, edges, { max: 20 });
  const stillThere = after.some((s) => {
    const k1 = `${s.a}|${s.b}`,
      k2 = `${s.b}|${s.a}`;
    const t1 = `${top.a}|${top.b}`;
    return k1 === t1 || k2 === t1;
  });
  ok(!stillThere, "the now-linked pair is no longer suggested");
}

// ================= R8: degenerate inputs never crash =======================
section("R8 — degenerate graphs: guarded, no crash");
const cases = {
  empty: { nodes: [], edges: [] },
  "one node": {
    nodes: [
      {
        kind: "page",
        id: "page:a",
        slug: "a",
        label: "A",
        summary: "hello world content",
      },
    ],
    edges: [],
  },
  "one node no content": {
    nodes: [{ kind: "page", id: "page:a", slug: "a", label: "", summary: "" }],
    edges: [],
  },
  "two disconnected": {
    nodes: [
      {
        kind: "page",
        id: "page:a",
        slug: "a",
        label: "Alpha",
        summary: "invoice tax gst",
      },
      {
        kind: "page",
        id: "page:b",
        slug: "b",
        label: "Beta",
        summary: "payroll salary leave",
      },
    ],
    edges: [],
  },
  "all orphan (no links-to)": { nodes: makePages(8, 10), edges: [] },
  "all empty content": {
    nodes: [
      { kind: "page", id: "page:a", slug: "a", label: "", summary: "" },
      { kind: "page", id: "page:b", slug: "b", label: "", summary: "" },
    ],
    edges: [],
  },
  "self-loop + dup edge": {
    nodes: makePages(4, 8),
    edges: [
      { kind: "links-to", source: "page:p0", target: "page:p0" },
      { kind: "links-to", source: "page:p0", target: "page:p1" },
      { kind: "links-to", source: "page:p0", target: "page:p1" },
    ],
  },
  "dangling edge target": {
    nodes: makePages(3, 8),
    edges: [{ kind: "links-to", source: "page:p0", target: "page:MISSING" }],
  },
};
for (const [name, data] of Object.entries(cases)) {
  try {
    const a = fullAnalysis(data);
    const acts = computeActions(data, a);
    const sim = computeSimilarity(data.nodes);
    ok(
      a && a.metrics && a.lists && a.communities,
      `${name}: fullAnalysis shape intact`
    );
    ok(
      acts &&
        Array.isArray(acts.stale) &&
        Array.isArray(acts.orphans) &&
        Array.isArray(acts.busFactor) &&
        Array.isArray(acts.duplicates),
      `${name}: computeActions shape intact`
    );
    ok(typeof sim.docCount === "number", `${name}: computeSimilarity ran`);
  } catch (e) {
    ok(false, `${name}: threw ${e && e.message}`);
  }
}

// ================= computeActions semantics ================================
section("computeActions — stale / orphan / busFactor / duplicates");
{
  const pages = [
    {
      kind: "page",
      id: "page:a",
      slug: "a",
      label: "Invoicing",
      summary: "invoice tax",
      stale: true,
    },
    {
      kind: "page",
      id: "page:b",
      slug: "b",
      label: "Invoicing",
      summary: "invoice gst",
      contradiction: true,
    },
    {
      kind: "page",
      id: "page:c",
      slug: "c",
      label: "Orphan One",
      summary: "lonely page",
    },
    {
      kind: "page",
      id: "page:d",
      slug: "d",
      label: "Linked",
      summary: "payroll salary",
    },
    {
      kind: "page",
      id: "page:e",
      slug: "e",
      label: "Linked Two",
      summary: "payroll leave",
    },
  ];
  const edges = [
    { kind: "links-to", source: "page:d", target: "page:e" },
    { kind: "authored", source: "user:alice", target: "page:d" }, // single author -> bus factor
  ];
  const data = { nodes: pages, edges };
  const a = analyze(data);
  const acts = computeActions(data, a);
  ok(
    acts.stale.length === 2,
    `2 stale/contradicted (got ${acts.stale.length})`
  );
  ok(
    acts.stale.some((s) => s.reason === "contradiction") &&
      acts.stale.some((s) => s.reason === "stale"),
    "both stale reasons present"
  );
  ok(
    acts.orphans.some((o) => o.slug === "c"),
    "orphan 'c' flagged"
  );
  ok(
    !acts.orphans.some((o) => o.slug === "d" || o.slug === "e"),
    "linked d/e not orphaned"
  );
  ok(
    acts.busFactor.some((b) => b.slug === "d" && b.author === "alice"),
    "bus-factor: single author 'alice' on d"
  );
  ok(
    acts.duplicates.some((d) => d.slugs.includes("a") && d.slugs.includes("b")),
    "near-dup title 'Invoicing' merged (a,b)"
  );
}

// ================= lists.debt — hot+stale pages, sorted by demand (#13) ====
section("analyze — lists.debt (knowledge debt panel)");
{
  const pages = [
    {
      kind: "page",
      id: "page:a",
      slug: "a",
      label: "A",
      summary: "invoice tax",
      debt: true,
      demand: 5,
    },
    {
      kind: "page",
      id: "page:b",
      slug: "b",
      label: "B",
      summary: "invoice gst",
      debt: true,
      demand: 20,
    },
    {
      kind: "page",
      id: "page:c",
      slug: "c",
      label: "C",
      summary: "payroll salary",
    }, // not debt
  ];
  const data = { nodes: pages, edges: [] };
  const a = analyze(data);
  ok(Array.isArray(a.lists.debt), "lists.debt is an array");
  ok(
    a.lists.debt.length === 2,
    `only debt-flagged pages included (got ${a.lists.debt.length})`
  );
  ok(!a.lists.debt.some((p) => p.slug === "c"), "non-debt page excluded");
  ok(
    a.lists.debt[0] && a.lists.debt[0].slug === "b",
    "sorted by demand desc (b before a)"
  );
}

// ================= summary =================================================
console.log(
  `\n${
    fail === 0 ? "✅" : "❌"
  } pure-JS verification: ${pass} passed, ${fail} failed`
);
process.exit(fail === 0 ? 0 : 1);
