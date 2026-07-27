# Jarvis Design Language

This document is the design contract for the Jarvis frontend (`frontend/`). It distills the
Frappe design language as actually implemented in **frappe-ui**, **Frappe CRM**, **Frappe
Helpdesk**, and **Frappe Cloud (press)**, and states how Jarvis applies it. Rules are drawn
from those codebases; where Jarvis makes its own call (dialog size, rail width, pane skeleton,
sentence case, the activation checklist) it is labeled as a Jarvis rule. When you need to know
"which button variant, where does the primary action go, what does a settings pane header look
like" — the answer is here.

**Scope: appearance only.** A visual rewrite must preserve every existing behavior contract of
the surface it touches — API calls and their `{ok, data, error}` envelope handling, emitted
events, store wiring (`store.settingsActions`, `settingsOpen`/`settingsSection`), System
Manager gating, Esc-close, lazy pane loading, and the onboarding readiness guard with its
`window.location.assign("/jarvis/")` full-reload exit.

Jarvis ships **frappe-ui 0.1.278** (pinned; same generation press runs). Token names below are
the 0.1.x names that work in Jarvis today. frappe-ui v1 renames a few (noted in §2.1) — do not
use v1 names until the dependency is upgraded.

**Where the reference apps disagree, Jarvis follows:** frappe-ui component semantics for
primitives (buttons, forms, dialogs), **CRM/Helpdesk** for the settings-dialog shape, and
**press** for billing/plan/signup surfaces.

**In-repo reference implementations** — these Jarvis files are already on-language; copy them
rather than inventing: `src/pages/files/FilesList.vue`, `src/pages/agents/AgentsList.vue`,
`src/pages/approvals/ApprovalsBoard.vue`, `src/components/shell/UserMenu.vue` (also
`src/pages/skills/*`, `src/pages/macros/*`).

---

## 1. The design language

Seven principles, each observable in the reference code:

1. **Neutral first; near-black is the brand action color.** The UI is gray-on-white
   (`surface-*` / `ink-gray-*`). The primary CTA is a *solid gray* button — near-black
   `#171717`, never blue. Blue is reserved for links, info states, and the sidebar
   growth/onboarding nudges (CRM's "Upgrade plan" trial banner and frappe-ui's
   getting-started banner are the only blue buttons). Selection states are dark-gray rings
   on press plan cards, not blue (Jarvis writes the ring with a semantic token — §4.2).
2. **Hue carries meaning, never decoration.** Green = success/active, red = danger/failure,
   amber/orange = warning/pending/unsaved, blue = info/links/running. Color appears in badges,
   banners, status dots, and error text — nowhere else.
3. **Nothing moves on hover.** Buttons transition background/border color only
   (`transition-colors`). Zero `hover:translate`, `hover:scale`, pulses, or gradients anywhere
   in frappe-ui, CRM, Helpdesk, or press (sole exception: stacked avatars `hover:scale-110`).
   Hover shadows are nearly as rare: the kit's *outline inputs* add `hover:shadow-sm` as a
   border affordance — do not add lift shadows to cards or buttons. Overlays animate opacity +
   `scale(0.98)→1` in 100ms, out in 150ms.
4. **Plain verbs, no glyphs in labels.** "Create", "Save", "Continue", "Send invites".
   Direction is an icon prop (`icon-right="lucide-arrow-right"`, chevron prefix), **never a
   "→" character in a button label** (zero hits across all four repos; `→` appears only in
   plain-text links like press's "All analytics →"). Casing: Jarvis standardizes on
   **sentence case** (frappe-ui's own direction); the reference apps are inconsistent
   ("Send Invites", "Pay Now", "Drop Site") — do not copy Title Case.
5. **Hairlines and subtle elevation, not heavy chrome.** Default `border` = gray-200
   (`--outline-gray-1`). Cards are `rounded-md border`, mostly unshadowed. `shadow-sm` marks
   the *selected/active* thing (nav item, segmented pill, focused input) — never a card or
   button lift.
6. **One kit, semantic tokens only.** All chrome uses `bg-surface-*`, `text-ink-*`,
   `border-outline-*`. Never raw grays, never hex, never `dark:` variants in app code — dark
   mode is a CSS-variable flip on `<html data-theme="dark">` and comes free.
7. **Dense but calm.** 14px Inter body, 28px controls, 4px spacing grid, tight 1.15
   line-height for chrome and 1.5 for prose. Headings are 600, never 700.

---

## 2. Tokens

### 2.1 Color

Three semantic families, emitted as CSS variables by the frappe-ui Tailwind preset and flipped
by `[data-theme="dark"]`:

| Family | Utilities | Purpose |
|---|---|---|
| `surface-*` | `bg-surface-white`, `bg-surface-gray-1..7`, `bg-surface-cards/modal/menu-bar` | Backgrounds |
| `ink-*` | `text-ink-gray-4..9`, `text-ink-white`, `text-ink-blue-link`, `text-ink-red-4`… | Text, icons, fills |
| `outline-*` | `border-outline-gray-1..5`, `outline-gray-modals`, `ring-outline-gray-3` | Borders, rings |

A bare `border` class already resolves to `var(--outline-gray-1)` (gray-200 hairline) — do not
add a color class to ordinary borders.

**Gray scale (light):** `50 #F8F8F8 · 100 #F3F3F3 · 200 #EDEDED · 300 #E2E2E2 · 400 #C7C7C7 ·
500 #999999 · 600 #7C7C7C · 700 #525252 · 800 #383838 · 900 #171717`.

**Ink roles in practice** (from CRM/Helpdesk/press usage):

| Token | Role |
|---|---|
| `ink-gray-9` | Page-level headings, primary values |
| `ink-gray-8` | Titles, body text, button labels |
| `ink-gray-7` | Secondary text |
| `ink-gray-6` | Descriptions, pane subtitles |
| `ink-gray-5` | Field labels, muted text, nav group headers, placeholders' darker kin |
| `ink-gray-4` | Placeholders, disabled, hints |
| `ink-white` | Text on solid (near-black / colored) fills |
| `ink-blue-link` | Links only |
| `ink-red-4` / `ink-red-3` | Error text, red badge ink / required asterisk, error-banner ink (the red ink ramp is `red-1..4` only) |

**Accents:** `blue-500 #0289F7` (info/links), `red` (danger), `green` (success), `amber/orange`
(warning/unsaved). Each has `surface-{hue}-1..`, `ink-{hue}-*`, `outline-{hue}-*` ramps for
badges and banners.

> **frappe-ui v1 renames (do not use yet):** v1 extends the ramp — solid primary becomes
> `bg-surface-gray-10`, `ink-white` becomes `ink-base`, and colors move to oklch. On 0.1.278
> the equivalents are `bg-surface-gray-7` and `text-ink-white`. If Jarvis upgrades, only these
> names shift; the language is identical.

> **Verify token values against the *built* CSS, not `frappe-ui/tailwind/colors.js`.** The
> `colors.js` keys (`--text-ink-gray-9`, and a `--surface-blue-2` that maps to `blue[500]`) are
> the config's internal naming and do **not** match what ships. The emitted runtime variables
> are `--ink-*`, `--surface-*`, `--outline-*`, and `--surface-blue-2` is the **pale** tint
> `#E6F4FF`. If you are checking a contrast ratio or a var name, read the compiled stylesheet
> (`jarvis/public/frontend/assets/*.css`) — reading `colors.js` will give you the wrong answer.

### 2.2 The legacy `jv-*` palette and how the stacks coexist

Legacy surfaces (settings dialog, onboarding) still use `frontend/src/theme.js`
`LIGHT_VARS`/`DARK_VARS` applied via inline `:style="paletteVars"` + a `.jv-dark` class. New
and rewritten code must use the semantic Tailwind tokens instead. Until legacy CSS is retired,
this is the mapping (use it when porting, never when writing new UI):

| jv-* var | Semantic equivalent | Notes |
|---|---|---|
| `--surface` | `bg-surface-white` | page background |
| `--surface-1` / `--surface-2` / `--surface-3` | `bg-surface-gray-1` / `-2` / `-3` | wells, hovers, active |
| `--border` / `--border-2` | `border-outline-gray-1` / `-2` | hairline / stronger |
| `--text` / `--text-2` / `--text-3` | `text-ink-gray-9` / `-7` / `-5` | |
| `--cta` / `--cta-fg` / `--cta-bg` / `--cta-bd` | near-black solid button (`bg-surface-gray-9` + `text-ink-white`) | **The CTA pair — and it inverts by theme:** fill `#1c1c22` light · `#ececf0` dark; `--cta-fg` is the readable foreground on that fill (`#ffffff` light · `#1c1c22` dark). **Always use them together** — `background: var(--cta); color: var(--cta-fg)`. Pairing `--cta` with a hard-coded `#fff` renders white-on-near-white in dark mode. (Formerly `--blue`, which was a misnomer: PR #294 repointed it from indigo to near-black but kept the name, so every consumer still reading it *as a blue* — links, gradient stops, icon strokes — silently turned near-black. Renamed to `--cta` so this cannot recur.) |
| `--link` | `text-ink-blue-link` | link + info text (`#1579d0` light · `#6e8bff` dark) — **the only sanctioned blue**: links, `b-run` badges, info states. If it should look like a link, use this. Never `--cta`. |
| `--green` / `--green-bg` / `--green-bd` | `text-ink-green-*` / `bg-surface-green-2` / `border-outline-green-*` | |
| `--red`, `--amber` families | red / amber semantic ramps | |
| brand purple `#8b5cf6`, gradients, glow rgba | **no equivalent — delete from chrome and controls on rewrite** | untokenized; violates §1.1–1.3 |

> **Brand-asset exception:** the gradient/purple ban applies to chrome and controls, not to
> brand-identity assets. The Jarvis mark, and a single sanctioned "processing" illustration
> during a long provisioning wait (SetupNeuralNet), may keep the brand gradient — provided
> they honor `prefers-reduced-motion` and read their colors from tokens, not hard-coded rgba.

Coexistence rules: `theme.js#applyTheme()` already stamps `data-theme` on `<html>`, so
Tailwind semantic tokens work everywhere, including inside legacy subtrees. The reverse is not
true — `jv-*` vars only resolve inside a subtree that bound `paletteVars`. Therefore: new
components use Tailwind tokens unconditionally; never bind `paletteVars` in new code; collapse
the two theme singletons to `@/theme` (`useJarvisTheme()`) when touching either.

### 2.3 Typography

Inter (variable) at **14px body** (`text-base`), positive `+0.02em` tracking — never negative.
The `InterVar` variable font is bundled by frappe-ui's stylesheet (its `@font-face` ships in
the built CSS; `index.html` deliberately carries no Google-Fonts link). Legacy CSS that
declares its own `font-family: 'Inter', system-ui` should drop the declaration and inherit.

Two line-height families: `text-<size>` is tight (1.15) for chrome; `text-p-<size>` is 1.5 for
sentences. Weights: regular is **420** (the 0.1.278 preset sets `fontWeight: 420` on the
`text-*` and `text-p-*` sizes), `font-medium` 500 for emphasized values and md/lg button
labels, `font-semibold` 600 for all titles. **Never 700 for headings.**

| Role | Recipe |
|---|---|
| Dialog title | `text-2xl font-semibold leading-6 text-ink-gray-9` (20px on 0.1.x) |
| Settings pane title | `text-lg font-semibold text-ink-gray-8` (16px) |
| Pane description | `text-p-sm text-ink-gray-6` (13px/1.5) |
| Section header inside a pane | `text-base font-semibold text-ink-gray-9` (14px) |
| Setting-row title | `text-base font-medium text-ink-gray-8` |
| Setting-row help | `text-p-sm text-ink-gray-6` |
| Field label | `text-xs text-ink-gray-5` (12px), sentence case, above the field |
| Body copy in dialogs | `text-p-base text-ink-gray-7` |
| List rows | `text-base`; secondary cells `text-ink-gray-6` |
| Nav/rail group label | `text-xs font-medium text-ink-gray-5` |

Casing: **sentence case everywhere** — titles, buttons, badges, labels (a Jarvis rule; see
§1.4). No ALL-CAPS, no uppercase micro-labels (Jarvis's 10px uppercase group headers and 8.5px
`sm-tag` pill are off-language).

### 2.4 Spacing & control metrics

4px grid; the preset adds half-steps (`h-7.5`, `h-10.5` are real). Key heights:
**controls default `h-7` (28px)** (sm buttons, inputs, selects, nav items), `h-8` md, `h-10`
lg; app header bar `h-10.5` (42px); settings list rows `h-14`. Density: `gap-2` icon-to-label,
`px-2` inside sm controls, settings pane padding `px-10 py-8`, page body `p-5`, form stacks
`flex flex-col gap-4` (or `gap-5`).

### 2.5 Radius

`rounded-sm` 4px · **`rounded` 8px (default — buttons, inputs, nav items)** · `rounded-md`
10px (cards, banners) · `rounded-lg` 12px (bigger cards, chips, dropdown menus) · `rounded-xl`
16px (dialogs) · `rounded-2xl` 20px (auth card) · `rounded-full` (badges, pills, avatars).

### 2.6 Shadows & focus

Most containers are **border-only**. `shadow-sm` = selected/active marker (active nav item,
active segmented pill, focused input). `shadow-xl` = dialogs. `shadow-2xl` = dropdown menus
and the auth card. Elevation never grows on hover for cards or buttons; the one sanctioned
hover shadow is the outline input's `hover:shadow-sm` (built into `TextInput`).

Focus is an outline ring, not a box-shadow glow: buttons get
`focus-visible:ring focus-visible:ring-outline-gray-3` (theme-matched for blue/green/red);
inputs show focus by turning white with a darker border (see §3.4) plus
`focus-visible:ring-2 ring-outline-gray-3`.

---

## 3. Components

### 3.1 Buttons — frappe-ui `Button`, nothing else

Props: `variant: solid | subtle (default) | outline | ghost`, `theme: gray (default) | blue |
green | red`, `size: sm (default) | md | lg | xl | 2xl`, `label`, `icon` (icon-only),
`iconLeft/iconRight`, `loading`, `loadingText`, `route`/`link`.

| Variant (gray) | Recipe | Use for |
|---|---|---|
| **solid** | `text-ink-white bg-surface-gray-7 hover:bg-surface-gray-6 active:bg-surface-gray-5` | **The** primary action. One per surface, top-right of a pane/page or last in a dialog footer. "Create", "Save", "Continue". |
| **subtle** | `text-ink-gray-8 bg-surface-gray-2 hover:bg-surface-gray-3` | Default secondary: row actions ("Change card", "Edit"), Cancel, Refresh, Load more, wizard "Skip". |
| **outline** | white bg + `border-outline-gray-2` | Rare: Back in wizard footers, Cancel in programmatic dialogs, option chips. |
| **ghost** | transparent, `hover:bg-surface-gray-3` | Icon-only buttons (close `lucide-x`, overflow `more-horizontal`, copy), inline tertiary actions. |

Sizes: `sm h-7 text-base px-2 rounded` · `md h-8 text-base font-medium px-2.5 rounded` ·
`lg h-10 text-lg font-medium px-3 rounded-md`. Icon-only: square, icon `size-4` at sm.

Rules:
- **One solid button per surface.** Everything else is subtle/ghost.
- **Themes:** red *solid* only inside confirm dialogs. A danger trigger resting on a page
  ("Delete" in a danger zone or on a member row) is red **subtle/outline** and must open a
  red-solid confirm dialog — exactly what press does on its settings panes. `blue` only for
  growth/onboarding nudges; everything else gray.
- **No arrows or glyphs in labels** — no `→ ↗ › ✓ ▲ ▼ ✕ ＋ ● ✦`. Use icon props: plus prefix
  for "New", `lucide-arrow-right` iconRight for forward motion, chevron suffix for dropdowns.
- **No hover motion.** Color shift only. No `translateY`, no scale, no shadow growth, no
  gradients, no pulse animations.
- **Loading:** set `:loading` on the button itself — spinner replaces the prefix icon, button
  disables. Use `loadingText` for long operations ("Creating site... This may take a while...").
  Never a separate spinner next to a still-clickable button.
- Links look like links (`text-ink-blue-link` or underline), buttons look like buttons. No
  `<a>` styled as a button, no button styled as an underlined link.

### 3.2 Dialogs — frappe-ui `Dialog`

Anatomy (all from the component; don't rebuild it):
- Overlay `bg-black-overlay-200` (dark: `-700`); panel `rounded-xl bg-surface-modal shadow-xl`;
  in 100ms scale 0.98→1 + fade, out 150ms.
- Sizes via `size`: default `lg` (512px); form dialogs `md`–`3xl`; settings shell `5xl`.
- Header: title `text-2xl font-semibold leading-6 text-ink-gray-9` left, **ghost icon button
  `lucide-x` top-right**. Optional 28px round icon chip before the title: `bg-surface-{theme}-2`
  fill; icon ink by appearance — default `text-ink-gray-5`, warning `text-ink-amber-3`, info
  `text-ink-blue-3`, danger `text-ink-red-4`, success `text-ink-green-3`.
- Body copy `text-p-base text-ink-gray-7`; body padding `px-4 pb-6 pt-5 sm:px-6`.
- Footer block `px-4 pb-7 pt-4 sm:px-6`.

**Footer conventions** (both observed; pick by dialog type):
- *Confirm / single-action dialogs*: use the `actions` array — 0.1.278 renders **stacked
  full-width buttons**, each with automatic loading state while its async `onClick` settles.
  List the primary (solid) action first; the ordering is caller convention (press's
  ConfirmDialog renders a single solid action), not enforced by the component.
- *Form dialogs (md–3xl)*: right-aligned pair in the footer — subtle "Cancel" then **solid
  primary last** (`flex justify-end gap-2` / CRM's `flex-row-reverse`).

**Destructive dialogs** (press `ArchiveSiteDialog` recipe): warning banner in the body with the
consequence spelled out, a "type the name to confirm" `FormControl` for irreversible actions,
single action `{ label: 'Delete …', variant: 'solid', theme: 'red' }`. Never `window.confirm()`.

**Unsaved-changes guard** (Helpdesk trio): Save button appears only when dirty (fade
transition), orange subtle `Badge label="Unsaved"` beside the title, `:dismissible="!isDirty"`
plus a confirm on tab switch.

### 3.3 The settings dialog with left rail (the Jarvis Settings shell)

Jarvis follows the CRM/Helpdesk shape — one `Dialog size="5xl"` (bare body), height
`calc(100vh - 8rem)`. (The exact `5xl` size and 224px rail width are Jarvis's pick; the shape
is theirs.)

```html
<Dialog v-model="show" :options="{ size: '5xl' }">
  <template #body>
    <div class="flex overflow-hidden" style="height: calc(100vh - 8rem)">
      <!-- left rail: 224px, sidebar surface -->
      <div class="flex w-56 shrink-0 flex-col overflow-y-auto rounded-l-lg bg-surface-menu-bar p-1">
        <!-- group label -->
        <div class="flex h-7.5 items-center gap-1.5 px-2 text-xs font-medium text-ink-gray-5">Workspace</div>
        <nav class="space-y-[3px] px-1">
          <!-- item: h-7, icon 16px, active = raised card -->
          <button class="flex h-7 w-full items-center gap-2 rounded px-2 text-sm text-ink-gray-8
                         hover:bg-surface-gray-2 [&.active]:bg-surface-white [&.active]:shadow-sm">
            <FeatherIcon name="settings" class="h-4 w-4 text-ink-gray-7" /> General
          </button>
        </nav>
      </div>
      <!-- content pane -->
      <div class="flex flex-1 flex-col overflow-y-auto bg-surface-modal">
        <component :is="activePane" />
      </div>
    </div>
  </template>
</Dialog>
```

- Rail groups ("Workspace", "Account & billing") use the 12px medium `ink-gray-5` label — not
  uppercase. Items gate by permission via a `condition` function, exactly like CRM/Helpdesk.
- Active item = `bg-surface-white shadow-sm` (raised card on the gray rail); inactive hover =
  `hover:bg-surface-gray-2`. Icons are 16px Feather/Lucide components, never hand-pasted SVG paths.
- **The dialog itself has no title bar and no global footer** — each pane owns its header and
  actions (§4.1). Do not render a pane label in dialog chrome *and* an `h2` inside the pane.
- List→detail inside a pane is stepped component state (Helpdesk), not routes: detail flips the
  header to a ghost back button (`icon-left="lucide-chevron-left"` + record name) with the
  Unsaved badge next to it.

### 3.4 Forms — frappe-ui `FormControl` for every input

- **Never hand-roll inputs, selects, textareas, checkboxes, or switches.** `FormControl` takes
  `type`, `label`, `description`, `size`, `variant`, `required`, `v-model`. **There is no
  `error` prop** — render errors with a separate `<ErrorMessage>` under the field.
- Label: `text-xs text-ink-gray-5`, above the field; required = automatic red asterisk
  (`text-ink-red-3`) + sr-only "(required)".
- Input recipe (default `subtle sm`): `h-7 rounded text-base`, **gray fill at rest that turns
  white on focus** with a darker border + `focus:shadow-sm` — the signature input look. Use
  `variant="outline"` (white + `border-outline-gray-2`) only on white cards (auth/signup, per
  press).
- Help text: `description` prop → `text-ink-gray-5` under the control (`text-p-xs` at sm,
  `text-p-base` at md).
- Choices: plain option sets → `FormControl type="select"`; searchable or large sets (company,
  provider) → frappe-ui `Autocomplete`. Never a native `<select>`, never a hand-rolled combobox.
- Errors: inline `<ErrorMessage :message="err" class="mt-2" />` (`text-sm text-ink-red-4`,
  `role="alert"`) under the field or at the bottom of the form block, above the CTA — plus
  `toast.error(...)` for request failures. No red-filled error panels.
- Success: `toast.success('Updated successfully')` — dark pill toast, never a green banner.
- Layout: single column `flex flex-col gap-4`; pairs `grid grid-cols-2 gap-4`. Section
  grouping: `text-base font-semibold text-ink-gray-9` heading, content `mt-6`, sections split
  by `<hr class="my-8" />`. No fieldset boxes.
- **Toggle row** (the settings staple): label `text-base font-medium text-ink-gray-8` + help
  `text-p-sm text-ink-gray-6` on the left, frappe-ui `<Switch>` on the right,
  `flex items-center justify-between`. Switch on-state is **near-black**, not green/blue.
  Never present an inverted toggle (label must describe the ON state).
- Search inputs: `TextInput` with `lucide-search size-4` prefix, `:debounce="300"`, ghost
  `lucide-x` clear button.

### 3.5 Tabs & segmented controls

- **Tabs** (frappe-ui `Tabs`): items `text-base text-ink-gray-5 py-2.5
  data-[state=active]:text-ink-gray-9`, optional `size-4` icon; active indicator = animated
  **2px near-black underline** (`bg-surface-gray-7`). No background, no bold. Use for page
  sections (press Settings/Billing pages route each tab).
- **Segmented control** (frappe-ui `TabButtons`): gray track `rounded-md bg-surface-gray-2
  p-px ring-1 ring-inset ring-outline-gray-1`, active pill = raised white chip
  `bg-surface-white shadow-sm !border-outline-gray-1 text-ink-gray-8` with a built-in
  `motion-safe:active:scale-[0.98]` press animation, inactive `text-ink-gray-5
  hover:bg-surface-gray-3/80`. Use for small in-place mode switches (Appearance
  Light/Dark/System, "Chat subscription | API keys").

### 3.6 Badges, pills, status

- frappe-ui `Badge`: always `rounded-full`, default `variant="subtle"` = ink on a tinted fill
  per theme — gray `text-ink-gray-6 bg-surface-gray-2`, blue `text-ink-blue-2
  bg-surface-blue-2`, green `text-ink-green-3 bg-surface-green-2`, orange `text-ink-amber-3
  bg-surface-amber-1`, red `text-ink-red-4 bg-surface-red-2`. Sizes `sm h-4` /
  `md h-5 px-1.5 text-xs` / `lg h-6`. Sentence case labels ("Unsaved", "Paid", "Default inbox").
- **Status→theme map** (press convention — adopt verbatim): Active/Paid/Success/Completed →
  green · Pending/Unpaid/Trial/Installing → orange · Running/Processing/Enabled → blue ·
  Failed/Broken/Attention required → red · Expired/Closed/On hold → gray (Jarvis extends:
  Inactive → gray).
- Record-level status may also be a **colored dot + text** (16px circle icon tinted by status)
  as in CRM/Helpdesk lists — never an emoji, never a bare colored `<b>`.

### 3.7 Banners & notices

- Inline alert (press `AlertBanner` shape): `flex items-center justify-between rounded-md p-2`
  with typed fill — info `bg-surface-blue-2 text-ink-blue-3`, warning `bg-surface-amber-2
  text-ink-amber-3`, error `bg-surface-red-2 text-ink-red-3`, success green; icon
  `lucide-info` / `lucide-alert-triangle` at `size-4`; copy `font-medium text-ink-gray-8`;
  optional action button + ghost `lucide-x` dismiss.
- Quiet informational note (Helpdesk): bordered box, not colored fill —
  `rounded-md p-2 ring-1 ring-outline-gray-modals` + `size-4` icon + `text-xs text-ink-gray-7`
  (`outline-gray-modals` matches `outline-gray-1` in light but diverges in dark).
- Persistent nudges (trial/getting-started) live as a small sidebar card
  (`rounded-lg shadow-sm py-2.5 px-3 bg-surface-modal`) with a blue growth button — the
  sanctioned blue of §1.1.

### 3.8 Empty, loading, error + retry

- **Empty state**: centered icon (`size-7.5`–`size-10 text-ink-gray-5`, or a 58px
  `bg-surface-gray-1` circle with a 24px icon), title `text-lg font-medium text-ink-gray-8`
  ("No agents found"), one sentence `text-p-base text-ink-gray-6`. When a filter/search is
  active, the copy switches to "Change your search terms or filters". CTA optional (CRM keeps
  Create in the page header).
- **Loading**: buttons carry their own `:loading`; panes center a `LoadingIndicator`
  (inherits currentColor) or `LoadingText` ("Loading..." in `text-ink-gray-4`); long jobs get a
  `Progress` bar with a step label. Skeletons (`animate-pulse bg-surface-gray-3`) only for
  dashboard-like card grids, first load only. Loading is never bespoke spinners or "Loading…"
  in random colors.
- **Error + retry**: inline `ErrorMessage` (red text) **with the failed action's button right
  there as the retry affordance** — a normal subtle/solid Button, `:loading` while retrying.
  Every fetch-failure state in a pane must have exactly this one retry pattern (Jarvis
  currently has three different retry buttons and one pane with none).
- **Error page/blocked state**: centered icon (`size-10 text-ink-gray-4` or red triangle for
  permission blocks in a dashed-border box), `text-lg font-medium` title, `text-p-base
  text-ink-gray-6` body, subtle Button with `arrow-left` prefix to go back.

### 3.9 Icons

- **Lucide is the icon set** (Feather via frappe-ui `FeatherIcon` is acceptable legacy for
  chevrons/simple glyphs). Stroke width 1.5. Default size **16px** (`size-4`) in buttons, nav,
  menus, banners; `size-6`–`size-10` in empty/error art. Icons inherit `currentColor` — tint at
  the call site with `text-ink-*`.
- Consume as components (`~icons/lucide/*`, `<FeatherIcon name>`) or string props
  (`icon="lucide-x"`) — **never hand-pasted `<svg>` path data in templates**.
- **Emoji are never UI**: not status (`✅⚠️❌`), not controls (`▲▼✕＋⇪`), not decorations
  (`✦📈🔍`). The only sanctioned emoji is user-chosen content (e.g. a user-picked view icon).
  Status semantics = Lucide glyph + semantic ink color.

### 3.10 Copy-to-clipboard

Ghost Button `icon="lucide-copy"` with a "Copy" tooltip; on success swap the icon to
`lucide-check` for ~1.5s — never a "Copied ✓" label change. The string being copied lives in
`text-sm font-mono rounded p-2 bg-surface-gray-2`.

---

## 4. Patterns

### 4.1 Settings panes

Every pane in the Settings dialog owns its own header (there is no global dialog footer):

```html
<div class="flex h-full flex-col gap-6 px-10 py-8 text-ink-gray-8">
  <div class="flex items-start justify-between">
    <div class="flex flex-col gap-1">
      <h2 class="flex items-center gap-2 text-lg font-semibold text-ink-gray-8">
        Plan &amp; billing <Badge v-if="dirty" label="Unsaved" theme="orange" variant="subtle" />
      </h2>
      <p class="max-w-md text-p-sm text-ink-gray-6">One-line description of the pane.</p>
    </div>
    <Button v-if="dirty" variant="solid" label="Save" :loading="saving" />
  </div>
  <div class="flex-1 overflow-y-auto">…rows / sections…</div>
  <ErrorMessage :message="error" />
</div>
```

- Title 16px semibold + 13px gray-6 description left; the **one solid action** top-right
  ("Save" appears only when dirty, "New" for list panes). No second heading inside the body.
- Content is stacked **setting rows** (title + help left, control right, hairline
  `border-outline-gray-1`/`hr` separators) or sections split by `hr class="my-8"` with
  `text-base font-semibold text-ink-gray-9` section heads.
- **Read-only detail rows** (Status/Model/Expires, Mode/Sync, connected-at): one KV component,
  used everywhere — `flex items-center justify-between py-2`, label `text-sm text-ink-gray-6`
  left, value `text-base text-ink-gray-8` right; status values use a Badge or status dot
  (§3.6), never a colored `<b>`; rows separated by `h-px bg-surface-gray-2`. Jarvis currently
  has four clones of this row — build it once.
- **Editable list rows** (model pool, key lists): each row `flex items-center gap-2`;
  FormControls fill the row, then ghost icon buttons `lucide-chevron-up`/`lucide-chevron-down`
  (reorder) and `lucide-x` (remove) at `size-4`. "Add model" is a subtle Button with a plus
  icon below the list — not a dashed full-width `＋` bar. Saving goes through the pane's single
  solid action, never a giant inline-styled button.
- **Long panes** that scroll past a viewport (the pool editor) may pin the single solid Save
  in a `sticky bottom-0 border-t bg-surface-modal px-10 py-3` action bar instead of the
  header — still one solid button, still dirty-gated. The footerless onboarding variant
  exposes `save()` to the wizard footer instead of rendering its own.
- **Async-save lifecycle** (saves that trigger a backend sync): the Save button carries its
  own `:loading`; while the sync poller runs, show a subtle blue `Badge label="Syncing…"`
  beside the pane title; on terminal success `toast.success('Saved')`; on `failed`/`skipped`,
  an inline `ErrorMessage` in the pane. Never a persistent green "Saved" flash text node.
- Stat/usage displays: bordered cards (`rounded-md border p-3/4`), value
  `text-2xl font-medium text-ink-gray-8`, label `text-sm text-ink-gray-6`. Charts read the
  theme from the single theme singleton.
- **Danger zone**: a plain section at the bottom — normal heading, explanatory
  `text-p-sm text-ink-gray-6` line, and a `subtle`/`outline` `theme="red"` button that opens a
  **red-solid confirm dialog** (§3.2). No red panels, no red *solid* buttons on the pane —
  press does exactly this (red subtle "Delete" on its Role settings page and member rows, each
  opening a confirm dialog).

### 4.2 Billing & plan surfaces (press is the reference)

- **Current plan block** (the pane's primary content): plan name `text-lg font-medium
  text-ink-gray-9` with price in `text-ink-gray-7` and the status Badge (§3.6 map) on one
  line; renewal on a `text-p-sm text-ink-gray-6` line ("Renews 12 Aug · auto-renew on");
  features as a plain `text-p-sm` list. No bordered plan card for the plan you're already on —
  cards are for choosing.
- **Plan cards** (`PlansCards` recipe): responsive grid (`grid grid-cols-2 gap-3
  @xl:grid-cols-3`); each plan is a `<button class="flex flex-col rounded-md border text-left
  hover:bg-surface-gray-1">`. Header `h-16 border-b p-3`: price `text-lg font-medium
  text-ink-gray-9` ("₹800") + `/mo` in `text-ink-gray-7`, sub-line `text-sm text-ink-gray-6`.
  Body `p-3 text-p-sm` feature rows (value + gray unit label). **Selected = a dark ring, not
  blue, not a lift** — press writes raw `ring-1 ring-gray-900`; Jarvis writes
  `border-outline-gray-5 ring-1 ring-outline-gray-5` (the darkest semantic outline) to stay
  inside the token contract.
- **Change-plan target state is an in-SPA dialog** (press pattern): `size="3xl"`, plan grid
  inside, full-width solid footer button whose label flips per state ("Select plan" →
  "Upgrade plan"). If billing details/payment method are missing, the same dialog becomes a
  stepped wizard with a `Progress` bar on top. **Until the in-SPA Razorpay flow exists**
  (Phase-2), keep the Desk deep-link (`/app/jarvis-account?billing=1`) but render it as a
  plain `text-ink-blue-link` text link ("Manage plan & billing"), never an `Upgrade →`
  button-styled `<a>`.
- **Billing overview**: definition-list rows separated by `h-px bg-surface-gray-2` — row title
  `font-medium` + value `text-ink-gray-7` left, **one subtle button per row right-aligned**
  ("Add card", "Edit", "Add credit"). Solid is reserved for the single action you want taken
  ("Pay now" on an unpaid strip). Status via the badge map (§3.6). Upgrade CTAs are quiet
  subtle buttons — never `Upgrade →` links styled as buttons.
- Payment failures: the row's error icon opens a confirm dialog embedding the raw gateway
  message in `text-sm font-mono rounded p-2 bg-surface-gray-2`, primary action "Change payment
  method".

### 4.3 Onboarding & wizards

- **Shape (Jarvis decision)**: onboarding is a sequence of routed steps sharing one centered
  card at `/jarvis/onboarding` (press-signup shape), not a Dialog. Keep the router readiness
  guard and the full-reload exit (`window.location.assign("/jarvis/")`) exactly as they are.
  Card: `mx-auto w-full sm:w-96 rounded-lg` (or `sm:w-112 rounded-2xl shadow-2xl`) on
  `bg-surface-gray-1` page tint; logo above; title `text-2xl font-semibold`; fields
  `variant="outline"` stacked.
- **Step indication**: flat progress segments (`h-1 flex-1 rounded-full`, done/current
  `bg-surface-gray-7`, upcoming `bg-surface-gray-3`) or a `Progress` bar with interval count.
  No numbered circles with connector lines, no checkmark-character dots.
- **Footer nav**: Back (`subtle`/`outline`, optionally `icon-left="lucide-arrow-left"`) far
  left; right cluster = optional subtle "Skip" + **solid primary last** ("Continue", final step
  "Finish"/"Done" — with `icon-right="lucide-arrow-right"` if a directional cue is wanted).
  Primary disabled until the step validates. In a card, the primary CTA is a single
  **full-width solid button** at the bottom; secondary path is a full-width subtle button;
  back/skip are plain text links under the card.
- **CTA discipline**: the finishing CTA is the same near-black solid button as everywhere
  else. No gradients, no glow shadows, no pulse animations, no decorative background orbs
  (brand-asset exception: §2.2 — the Jarvis mark and the provisioning illustration only). The
  wizard proves quality through calm, not sparkle.
- **Post-signup activation**: prefer a checklist over a marketing tour — steps registered with
  `useOnboarding(app)`, each step's action **deep-links into the real UI** (opens the settings
  dialog at the right tab, routes to the real form) and is marked complete where the action
  actually happens; progress lives in a small sidebar banner ("3/6 steps" + the one blue
  "Continue" button). If the existing 6-slide tour is retained, keep its `finish`/`skip`
  contract and restyle the slides in the language: Lucide icons and semantic tokens in the CSS
  mock devices (`bg-surface-*`, hairline borders), no emoji, no hard-coded hex, no `✦` eyebrow
  glyphs.
- Self-host/system checks render as icon rows: `lucide-circle-check text-ink-green-*` /
  `lucide-alert-triangle text-ink-amber-*` / `lucide-circle-x text-ink-red-*` — not `✅⚠️❌`.

### 4.4 The OAuth paste-back connect flow — one component

The sign-in-and-paste-back flow (subscription connect, pool account connect, onboarding
connect) is **one shared component** reused by every surface — Jarvis currently implements it
three times. Recipe:

- Numbered steps as plain `text-base` text ("1. Sign in with your provider…") — no
  connector-line circles, no "Step 1 -" hyphen separators.
- Primary solid Button "Open sign-in" with `iconRight="lucide-external-link"` — never an
  `<a>` styled as a button, never `↗` in the label.
- The sign-in URL in a mono copy block with the §3.10 copy button.
- A `FormControl` paste field (never a bare `<textarea>`), inline `ErrorMessage` for
  expired/invalid-nonce errors, countdown hint in `text-p-sm text-ink-gray-5`.

### 4.5 Voice capture — two surfaces, two retention models (say which one you are)

Jarvis has **two** voice-recording surfaces with deliberately different contracts, and a user who
learns one carries its expectations to the other. State which one a new surface implements.

- **Chat composer dictation** (`useDictationRecorder` + `utils/voiceDictationStore.js`, ChatView):
  a take is **one recording**, transcribed in **one call** with its full context, and its audio is
  **retained** in an IndexedDB mirror until the transcript is part of an accepted send. While
  recording, ~15 s timeslice fragments are mirrored as they arrive purely for crash-safety — they
  are never a transcription unit. Anything unresolved is **visible and actionable as one chip per
  recording** — didn't transcribe (Retry/Download/✕), edited out before sending
  (Restore/Download/✕), audio that couldn't be saved (Download/Retry) — plus a previous-session
  recovery banner. Nothing is ever removed silently except the success path (chips clear on the
  send that carried their words; no toast — that would be noise on the common case).
- **Single-shot capture** (`useAudioRecorder`, `VoiceRecorder.vue` — wiki nudge, Business tab):
  record → transcribe → text, **no retention, no chips, no recovery**. A failure is a toast and a
  re-record, and the audio is gone.

Rules for either. **Never split one utterance into independently-transcribed pieces** — a speech
model handed a fragment with no beginning and no end invents words to bridge the gap, and its
failure mode is fluent, confident text nobody said; if a take is too long for one call, that is a
budget problem to solve, not a reason to chop it up. Chip copy must distinguish "not sent yet"
from "your last message went without this" (identical copy for both is what makes a correctly
retained recording read as a stuck one). An action's tooltip must promise only what it can do
*now* (Retry cannot edit a message that has already been sent — it adds to the current draft). A
progress indicator may only state something it actually knows: the length of the recording being
transcribed is honest, a percentage is theatre. And a leave/close warning may claim loss **only**
when something is genuinely at risk — durably mirrored, re-offerable audio must not arm it (a
warning that cries wolf gets trained away). If a new surface wants the chip model, reuse the store
rather than growing a third contract.

---

## 5. Do / Don't — current Jarvis anti-patterns

Each "Don't" is live in the Jarvis codebase today (settings dialog, onboarding, tour); each
"Do" is the reference behavior or the pattern defined above.

| # | Don't (current Jarvis) | Do (the language) |
|---|---|---|
| 1 | Five+ parallel button systems (`.jv-btn--*`, `.jv-mon-retry`, `.jv-dsub-btn*`, `.jv-ob-btn*`, `.btn--*`) | One: frappe-ui `Button` with variant/theme/size |
| 2 | Hover-lift `translateY(-1px/-2px)` on buttons and plan cards | Color-shift-only hover; selection = dark ring, not motion |
| 3 | `Upgrade →`, `Submit →`, `Start Chatting →`, `Open sign-in ↗`, `Open ›`, `Copied ✓`, `● Unsaved changes`, `✦ Welcome` | Plain verb labels; direction/state via icon props (`lucide-arrow-right`, `lucide-check`); dirty state = orange Badge |
| 4 | Gradient pulsing CTA, purple glow shadows, decorative blurred orbs | Near-black solid CTA; zero decorative motion; no gradients (brand-asset exception: §2.2) |
| 5 | Literal emoji as status (`✅⚠️❌` self-host checks) and controls (`▲▼✕＋⇪`, `📈🔍✎` mocks) | Lucide glyphs tinted with semantic ink colors; reorder/remove as icon buttons (§4.1) |
| 6 | Three retry paradigms across four panes; PlanBillingPane has no retry | One pattern: inline `ErrorMessage` + the action's own Button with `:loading` |
| 7 | Dialog header repeats as an `h2`/`h3` inside the pane (double heading) | Pane owns its single header; dialog chrome carries no duplicate title |
| 8 | `<a>` styled as buttons (`jv-acct-btn-sm`), buttons styled as links (`jv-ob-link`) | Buttons are `Button`; links are `text-ink-blue-link`/underline text |
| 9 | Custom 42px inputs, native selects/checkboxes, hand-rolled `.jv-switch` | frappe-ui `FormControl`/`Switch`; 28px inputs, gray-fill→white-on-focus |
| 10 | Massive inline `:style` objects and inline-styled close button + `!important` hover patches | Tailwind semantic utility classes; no inline styles, no `!important` |
| 11 | `paletteVars` inline vars + `.jv-dark` class; two theme singletons | `<html data-theme>` + semantic tokens; one `useJarvisTheme()` singleton. (The `--blue` misnomer in this row is **fixed** — it is now `--cta`/`--cta-fg`; see §2.2.) |
| 12 | 10px uppercase group headers, 8.5px `sm-tag` micro-pill | 12px `font-medium text-ink-gray-5` group labels; `Badge size="sm"` minimum 11px |
| 13 | Hand-pasted SVG path data per nav item | `FeatherIcon`/Lucide components at `size-4` |
| 14 | `window.confirm()` for disconnect | frappe-ui `confirmDialog` / red-solid Dialog action |
| 15 | Billing exits the SPA to Desk (`/app/jarvis-account?billing=1`) with link-buttons | Target: in-SPA plan/billing dialogs (press pattern); until Phase-2, the Desk link stays but styled as a plain text link (§4.2) |
| 16 | Toast errors in one pane, inline red text in others, green flash notes | Errors: inline `ErrorMessage` + `toast.error`; success: `toast.success`; dirty: orange "Unsaved" badge; sync: "Syncing…" badge (§4.1) |
| 17 | Inverted toggle ("Confirm before changes" bound to `!autoApply`) | Switch state matches its label; rename the label if needed |
| 18 | "Coming soon." placeholder card | Ship the section when it exists; empty states only for real data absence |
| 19 | Four read-only kv-row clones (`jv-set-row`, `jv-mon-kv`, `jv-acct-kv`, `jv-dsub-kv`) | One KV row component (§4.1) |
| 20 | OAuth paste-back flow implemented three times (DirectSubscriptionCard + LlmPoolEditor ×2) | One shared connect-flow component (§4.4) |

---

## 6. Accessibility & dark mode

**Dark mode**
- Single mechanism: `useJarvisTheme()` sets `<html data-theme="dark|light">` (persisted at
  `localStorage["jarvis-theme"]`, `system` via `matchMedia`). The Tailwind preset is configured
  `darkMode: ['selector', '[data-theme="dark"]']`.
- App code **never writes `dark:` variants** and never branches on theme for colors — semantic
  tokens flip automatically. The only code that may read the dark flag is canvas/chart code
  (ECharts), which cannot consume CSS vars directly.
- Never hardcode hex/named colors in components; that is the entire dark-mode contract.
- Elevation in dark mode comes from lighter surfaces (`surface-modal`/`surface-cards` step up),
  not darker shadows — provided by the tokens; don't add custom dark shadows.

**Accessibility**
- Focus is always visible: keep the token focus rings (`focus-visible:ring
  ring-outline-gray-3`); never `outline: none` without a replacement. Inputs signal focus by
  the white-fill + darker-border swap plus the ring.
- Every control is labeled: `FormControl` wires `label for`; required fields get the sr-only
  "(required)". Errors use `ErrorMessage`'s `role="alert"`. frappe-ui does **not** wire
  `aria-invalid`/`aria-errormessage` — add them at the call site when a field has an inline
  error.
- `Button :loading` disables the button and swaps in a spinner (built in). It does **not** set
  `aria-busy` — add it at the call site for long operations.
- Segmented controls and tabs are real `role="tablist"`/radio-group semantics — use the
  frappe-ui components rather than styled divs.
- Motion respects `prefers-reduced-motion` where motion exists: `TabButtons`' press animation
  is `motion-safe`-gated, and decorative canvases must render a static frame (keep
  `SetupNeuralNet`'s reduced-motion contract in any replacement). frappe-ui's spinners have no
  reduced-motion handling — another reason not to add decorative motion that would need it.
- Color is never the only signal: status pairs hue with a label or icon (badge text, dot +
  word), matching the badge/status-dot patterns.
- Icon-only buttons **must carry a `label`** (frappe-ui `Button`) or an explicit `aria-label`
  (native `<button>`). **`tooltip` is not an accessible name** — `Button.vue` binds
  `:aria-label="label"` to the `label` prop *only*; `tooltip` merely renders a hover bubble. An
  icon-only `<Button icon="x" tooltip="Close" />` announces as a bare, unnamed "button". If the
  label would be visually redundant, keep `label` and hide it: frappe-ui renders no text for an
  icon-only button, so `label` costs nothing visually and is the accessible name.
- Contrast: body text at `ink-gray-7`+ on `surface-white`; `ink-gray-5` is the floor for
  meaningful text (labels/hints); `ink-gray-4` only for placeholders/disabled.

---

## Appendix: quick reference

```html
<!-- Primary action (near-black; ONE per surface) -->
<Button variant="solid" label="Save" :loading="saving" />

<!-- Secondary / row action -->
<Button label="Edit" />                     <!-- subtle by default -->

<!-- Icon-only close -->
<Button variant="ghost" icon="lucide-x" @click="close" />

<!-- Destructive (red solid only inside a confirm dialog) -->
{ label: 'Delete workspace', variant: 'solid', theme: 'red' }

<!-- Field (no error prop — ErrorMessage is separate) -->
<FormControl type="text" label="Company name" v-model="name" required
             description="Shown on invoices." />
<ErrorMessage :message="errors.name" class="mt-1" />

<!-- Toggle row -->
<div class="flex items-center justify-between">
  <div class="flex flex-col gap-1">
    <span class="text-base font-medium text-ink-gray-8">Ask before applying changes</span>
    <span class="text-p-sm text-ink-gray-6">Jarvis will confirm before writing to the ERP.</span>
  </div>
  <Switch v-model="confirmFirst" />
</div>

<!-- Status badge -->
<Badge label="Active" theme="green" variant="subtle" />

<!-- Read-only KV row (one component; see §4.1) -->
<div class="flex items-center justify-between py-2">
  <span class="text-sm text-ink-gray-6">Model</span>
  <span class="text-base text-ink-gray-8">gpt-5.5</span>
</div>

<!-- Empty state -->
<div class="flex flex-col items-center text-center">
  <FeatherIcon name="inbox" class="size-8 text-ink-gray-5" />
  <div class="mt-3 text-lg font-medium text-ink-gray-8">No macro runs yet</div>
  <div class="mt-1 text-p-base text-ink-gray-6">Runs will appear here after a macro executes.</div>
</div>

<!-- Error + retry (the one pattern) -->
<ErrorMessage :message="error" class="mt-2" />
<Button label="Retry" :loading="retrying" @click="load" />
```

When in doubt: make it gray, make it 14px, make it 28px tall, put the one solid button
top-right or footer-last, and let the tokens do the theming.
