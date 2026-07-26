# Graphite — dashboard canvas theme

A dark theme: near-black page, graphite surfaces, a light ink ramp and a soft blue accent. Designed for low-light and wall displays.

This is a **dark** theme (`data-theme="dark"`). Light/dark is driven **only** by the theme's `data-theme` on `<html>`
(and the `jarvis:theme` CustomEvent on change) — never by
`@media (prefers-color-scheme)` or a `color-scheme` declaration, both of which
the save-time validator rejects.

The machine-enforceable copy of everything below lives in `theme.json` (tokens,
palette order, scales and the validator allow-lists). This file is the human
recipe and the source for the generation cheatsheet injected into the
`jarvis-dashboards` skill.

## 1. Color tokens

Injected as CSS custom properties on `:root`; compose with `var(--jd-*)`, never
hardcode a hex. The validator rejects any off-theme color VALUE in any syntax —
only these token hexes, the ordered chart palette below, and the approved
neutrals white/black/transparent are allowed. The status tokens
(`--jd-positive`/`--jd-negative`/`--jd-warning`/`--jd-info`) are the text-safe
status colors (AA on `--jd-surface`), deliberately darker than the vivid
chart-palette values used for series fills.

| Token | Value | Role |
| --- | --- | --- |
| `--jd-bg` | `#191919` | Page background |
| `--jd-surface` | `#222222` | Card / panel background |
| `--jd-surface-2` | `#2a2a2a` | Subtle fill (table header, insets) |
| `--jd-ink` | `#ececec` | Body text |
| `--jd-heading` | `#ececec` | Headings |
| `--jd-muted` | `#9a9a9a` | Secondary / labels |
| `--jd-line` | `#333333` | Hairline borders |
| `--jd-accent` | `#8ab4f8` | Primary accent |
| `--jd-positive` | `#4ade80` | Up / good status |
| `--jd-negative` | `#f87171` | Down / bad status |
| `--jd-warning` | `#fbbf24` | Caution status |
| `--jd-info` | `#8ab4f8` | Informational status |
| `--jd-radius` | `10px` | Corner radius |
| `--jd-shadow` | `none` | Card shadow |

### Categorical chart palette (in order)

Series colors come from `window.JARVIS_THEME.palette` — use it in order, never
hardcode chart colors. Do not reorder.

| # | Color |
| --- | --- |
| 0 | `#8ab4f8` |
| 1 | `#4ade80` |
| 2 | `#fbbf24` |
| 3 | `#f87171` |
| 4 | `#c084fc` |
| 5 | `#22d3ee` |
| 6 | `#fb923c` |
| 7 | `#a3e635` |

## 2. Typography

Both body and display use the system sans stack (`--jd-font` == `--jd-font-display`). Webfonts are impossible inside the canvas (the CSP is
`font-src data:` with no network) and `@font-face` is rejected — never name a
webfont like Inter.

Exact type scale (px / weight / line-height / letter-spacing):

| Role | Size | Weight | Line-height | Tracking |
| --- | --- | --- | --- | --- |
| display | 32px | 700 | 1.15 | -0.02em |
| h1 | 24px | 700 | 1.2 | -0.01em |
| h2 | 18px | 600 | 1.3 | -0.005em |
| h3 | 15px | 600 | 1.35 | 0 |
| label | 13px | 600 | 1.3 | 0.01em |
| body | 14px | 400 | 1.5 | 0 |
| caption | 12px | 400 | 1.4 | 0 |

## 3. Spacing, radius, border

- Spacing steps (px): 4 · 8 · 12 · 16 · 24 · 32 · 48. Gutter between cards is
  24px.
- Radius: cards use `--jd-radius` (10px); small chips 6px; pills 999px.
- Borders: 1px hairline in `--jd-line` is the default separator; 2px only for
  emphasis. No heavy rules.

## 4. Layout grid

- Page: max-width 1200px, centered, 24px gutter
  (`.jd-page`).
- KPI row: 4 columns, auto-fit `minmax(200px, 1fr)`
  (`.jd-kpi-row`).
- Chart grid: 2 columns, auto-fit
  `minmax(320px, 1fr)` (`.jd-chart-grid`).
- Breakpoints (px): sm 640 · md 768
  · lg 1024 · xl 1280.

## 5. Component recipes (named classes)

The canvas shell injects these classes in a winning `@layer theme` — **compose
them** instead of restyling; that is what keeps every dashboard structurally
consistent within the theme. All colors/fonts inside come from tokens.

- `.jd-page` — the centered page frame.
- `.jd-page-header` — title block; `h1` + optional `.jd-subtitle`.
- `.jd-kpi-row` > `.jd-kpi` — stat tiles. Inside: `.jd-kpi-label`,
  `.jd-kpi-value`, `.jd-kpi-delta.up` / `.jd-kpi-delta.down` for the change.
- `.jd-card` / `.jd-chart-card` — panel chrome; `.jd-card-header` for the title,
  `.jd-card-footnote` for the as-of / source note.
- `.jd-chart-grid` — two-up chart layout.
- `.jd-table` — data table (hairline rows, muted `--jd-surface-2` header).
- `.jd-empty` / `.jd-error` — empty and error states (error uses
  `--jd-negative`).

Minimal skeleton:

```html
<div class="jd-page">
  <div class="jd-page-header"><h1>Title</h1><div class="jd-subtitle">As of ...</div></div>
  <div class="jd-kpi-row">
    <div class="jd-kpi"><div class="jd-kpi-label">Revenue</div>
      <div class="jd-kpi-value">$1.2M</div>
      <div class="jd-kpi-delta up">+4.1%</div></div>
  </div>
  <div class="jd-chart-grid">
    <div class="jd-chart-card"><div class="jd-card-header">Trend</div>
      <div id="trend" style="height:280px"></div></div>
  </div>
</div>
```

## 6. Chart chrome (ECharts)

- Series colors: `window.JARVIS_THEME.palette` in order.
- Axis labels / legend: `--jd-muted`. Gridlines: `--jd-line`. Text: `--jd-ink`.
- Tooltip: surface `--jd-surface`, border `--jd-line`, text `--jd-ink`.
- Re-render on the `jarvis:theme` event if you read tokens at chart-build time.

## 7. Accessibility

- Text contrast AA 4.5:1; large text / UI 3:1. The text tokens are tuned to clear
  4.5:1 on both `--jd-bg` and `--jd-surface`: body on `--jd-ink`, headings on
  `--jd-heading`, secondary/labels on `--jd-muted`, and the status tokens
  `--jd-positive`/`--jd-negative`/`--jd-warning`/`--jd-info` as status TEXT. Those
  status text tokens are deliberately darker than the vivid chart-palette values
  (which are for series fills / dots, not body text) — keep them off colored
  fills. A per-theme contrast test guards every recipe's fg/bg token pair.
- Units on every number; an as-of date on static data.

## 8. Forbidden (the validator rejects these)

- Any off-theme color VALUE, in ANY syntax — a hex/rgb/hsl not in §1, a modern
  color function (`oklch`/`lab`/`lch`/`hwb`/`color()`/`color-mix()`), or a named
  CSS color — beyond white/black/transparent. Compose `var(--jd-*)` instead.
- `font-family` naming a non-system face (e.g. Inter), including a `var(--x)`
  indirection to a non-theme font; any `@font-face`.
- Redefining a `--jd-*` theme token (only the theme layer owns them).
- `@media (prefers-color-scheme)` or `color-scheme` (use `data-theme`).
- External URLs (http/https, protocol-relative, `@import "//…"`); load nothing
  over the network.
- Color in an inline `style=` attribute; `!important` on ANY declaration.

The validator enforces COLOR, font-family, no-`!important`, no-`prefers-color-scheme`
and the safety bans — it does NOT lint STRUCTURE. Spacing, the type scale and
layout stay consistent through the winning component classes (in `@layer theme`)
and the generation prompt, not a hard spacing rule.
