# The blank draft card: three fixes

Status: **draft, awaiting Fable review** · Date: 2026-07-17
Repos: `Aerele-RnD/jarvis` (app), `Aerele-RnD/jarvis-persona`

## What happened (a real production transcript, 2026-07-17)

User: *"Create the below tasks and assign to me — test the confirmation ui / test admin v2 / fix
issues in the list"*

The agent called `get_creation_context("ToDo", …)`, then emitted:

```
```jarvis-action
{"kind":"doc","verb":"create","doctype":"ToDo","title":"Create 3 assigned tasks",
 "summary":"3 open ToDos assigned to Administrator","docs":[{…},{…},{…}]}
```
```

**The user got a card that proposed nothing** — the model-written headline, zero field rows, and an
enabled Confirm. (Click Edit or Preview and you get a single empty Description box. See "What the
user actually saw" below: the summary card and the Edit panel fail differently, and the smoke must
check both.)

## Root cause: `docs` is not a `jarvis-action` key

The persona documents exactly one shape for a doc action (`AGENTS.md:122`):

```json
{"kind":"doc","verb":"create","doctype":"Sales Order","title":"…","summary":"…",
 "fields":[{"label":"customer","value":"…"}],"tables":[{"fieldname":"items","rows":[…]}]}
```

`fields` + `tables`. **No `docs`.** Verified: **nothing** in `frontend/src` or `pwa/src` reads
`docs` off an action block. (The two `args.docs` hits in `actionSummary.js:160/180` are
`receiptView`/`receiptNames`, which read a parked **tool call**'s args — a different path.)

So `buildDraftModel` (`ChatView.vue:2126`) reads `a.fields` → `undefined` → `proposed = {}`, then:

```js
const baseV = base.values[f.fieldname]   // {} on a create — loadDocForEdit only runs for update
if (!has && !f.reqd) continue            // skips every non-proposed, non-required field
```

Only `description` survives (it is `reqd`), with an undefined value. Hence one empty box.

**Nothing failed.** `buildDraftModel` did not throw, so `ensureActionSummary`'s error path never
fired. The agent never learns the card was blank — the only channels into its memory are a
continuation turn or the `[Context:]` bracket, and neither carries "your card rendered empty".

**Why the model did it — and the correction that matters.** An earlier draft of this spec claimed
"no rule covers three independent records". **That is false.** `AGENTS.md:115` covers it exactly:

> *"SEVERAL -> state the count ("54 Tasks + 1 Timesheet") and **ask ONCE**: stage each for review,
> or take consent now and create all in bulk. On bulk consent, stop asking per record:
> `jarvis__create_doc(docs=[...])` in batches of up to 20, one atomic card per batch."*

So the model **violated an existing rule** — it neither asked once nor called the tool. This is not
a contract hole it fell through, and the "it will recur" argument does not rest on one.

What **is** genuinely absent is any statement that `docs` is not a `jarvis-action` key. The block
shape is documented positively (`:122`) and never negatively, and nothing validates it. So fix 1
still earns its place, but as a **prohibition**, not as the missing batching rule — that rule is
already there, twenty lines up.

## Fix 1 — persona: one record per action block

`jarvis-persona/AGENTS.md`. **Budget: AGENTS.md is 30,309 of the fleet's 32,000/file cap — 1,691
chars of headroom.** This must be one sentence, not a section. Lint gates: ASCII only, no em-dash
(U+2014) or en-dash (U+2013), `./lint.sh` + `python validate.py` must pass.

Add to the drafting rule at `:117`, after "Draft the WHOLE record in ONE message.":

> One `jarvis-action` block is ONE record - `fields` + `tables`, never a `docs` list. Several
> independent records take the one-or-many fork above: ask once, then stage each or one
> `jarvis__create_doc(docs=[...])` batch (it parks).

**228 chars** — AGENTS.md lands at **30,538 of 32,000**, leaving **1,462**. (Measured, not
estimated; an earlier draft guessed "~210 / ~1,480".) `validate.py`'s call-shape check accepts
`jarvis__create_doc(docs=[...])` — `:129` already cites that exact shape and lints green.

The referential style ("the one-or-many fork above") is house style rather than a new ambiguity:
`:139` already says "take the bulk fork above" pointing at the same `:115`. And "(it parks)" is
unconditionally true — `TOOLS.md:146`: create_doc may fast-path but "never a batch", so a `docs`
batch parks even under auto-apply ON.

**Why this wording, and what an earlier draft got wrong.** The first version ended *"Several
independent records go in one `jarvis__create_doc(docs=[...])` call instead"* — which prescribes
bulk **directly**, skipping the ask-once consent fork `:115` mandates, two paragraphs below the rule
it contradicts. `:139` also routes "many records under ONE operation" to "the bulk fork above",
i.e. `:115`. A persona fix that quietly overrides an existing consent gate is worse than the bug it
fixes. The corrected sentence **points at** `:115` rather than restating a lossy version of it.

It still names the wrong shape explicitly (`never a docs list`) because that is the exact mistake
made, and a prohibition the model can pattern-match on is the whole point of the change.

## Fix 2 — fail loudly instead of rendering a blank card

`frontend/src/views/ChatView.vue` **and** `pwa/src/components/ActionCard.vue`. An earlier draft
hedged with "(+ the PWA if it parses action blocks)" — it does, and it has its own version of this
bug, so the hedge was just an unchecked assumption.

**PWA (`pwa/src/lib/blocks.js:67-99` `parseAction`, `pwa/src/components/ActionCard.vue:135`):** a
docs block renders a titled card with an **empty body and a live Create button**; pressing it builds
`values = {}` and fails at `ActionCard.vue:60-63` ("Couldn't match any of these fields..."). Better
than the desktop (it fails eventually) but still a blank card first.

> **⚠️ Do NOT guard by returning `null` from `parseAction`.** `STRIP_RES` (`blocks.js:17-26`) strips
> the ```jarvis-action fence from the prose **unconditionally**, independent of whether the parse
> succeeded. A null parse therefore makes the proposal **disappear entirely** — no card, no text,
> nothing — which is the exact failure `blocks.js`'s own header comment warns about, and strictly
> worse than the blank card.

> **⚠️ Do NOT copy the desktop condition.** ActionCard renders only `props.action.fields`
> (`:135`) and `apply()` sends only mapped scalar `values` (`:53-81`) — **the PWA neither renders
> nor applies `tables`** (desktop `applyDraft` does, `ChatView.vue:2360-2366`). So the desktop's
> "a tables-only create is legal" escape is meaningless here: a fields-empty block has nothing to
> show on the phone regardless of tables, and transcribing `&& !tables.length` would let it render
> blank again.

Add to `ActionCard.vue`'s script setup, after `heading` (`:28`) — a computed, checked up front so
the problem renders **before** a tap rather than inside `apply()`:

```js
// No `tables` escape here, unlike the desktop: this card neither renders nor
// applies child tables, so a block with no `fields` has nothing to show.
const invalid = computed(() => {
	if (!isWrite.value) return ""
	if (Array.isArray(props.action.docs))
		return "This draft carries a `docs` batch, which is a create_doc payload rather than a card. Ask Jarvis to apply them as a batch."
	if (verb.value === "create" && !(props.action.fields || []).length)
		return "This draft has no fields to show."
	return ""
})
```

Template (`:141`), reusing the existing error div: `<div v-if="invalid || error" class="jv-action-err">{{ invalid || error }}</div>`,
and `:disabled="state === 'busy' || !!invalid"` on the primary button (`:152`). **Keep Cancel live**
so the card stays dismissable.

Copy note: the PWA says "Ask **Jarvis**" (matching its existing voice at `:68`); the desktop says
"Ask **me**". Do not unify them.

A `kind:"doc"` action that yields **no renderable fields** must render an **error**, not an empty
form. Silent-blank is the worst outcome available: it looks like Jarvis didn't try, the user cannot
tell what went wrong, and no signal reaches anyone.

**`buildDraftModel` has TWO callers, and a throw breaks the other one.** `openDraftPanel`
(`ChatView.vue:2193-2197`) has no `try/catch` — it handles `undefined` (`if (!model) return`), not a
throw. And the **Edit** button (`ChatView.vue:300`) has **no `:disabled` binding**, unlike Confirm
(`:296`) and Preview (`:299`), so it renders even in the error state. Failure: docs block → error
card (good) → user clicks Edit → the rejection escapes the `@click` handler → dead button, console
error. `ChatView.vue:2325`'s `openDraftPanel(...).then(...)` in the `watch` has no `.catch` either.

Both must be fixed in the same change:

```js
// Open the editable panel for an action, via the shared buildDraftModel.
async function openDraftPanel(a) {
	let model
	// Deliberate: swallow to the SAME dead-end the old `if (!model) return` gave, so
	// a guard throw cannot escape a @click handler. The summary card already shows
	// the error. Do not "fix" this into a rethrow.
	try { model = await buildDraftModel(a) } catch (e) { return }
	if (!model) return
	draftPanel.value = model
}
```

plus `:disabled="!summaryState.model"` on the Edit button at `:300`, mirroring Preview.

**Place the guard right after the `verb` line (`:2128`), NOT after `proposed` is built.** It reads
only `a.fields`/`a.tables`/`a.docs`, so putting it later wastes a `_formMeta` network call and
implies a dependency on `proposed` that does not exist.

In `buildDraftModel`:

```js
	// An action block we cannot render must FAIL, not render empty - a blank card
	// reads as "Jarvis did not try" rather than "Jarvis emitted a shape I cannot
	// draw". `docs` is a create_doc batch payload, not an action key (AGENTS.md:122
	// documents fields+tables for ONE record) - throw on it for EITHER verb: a
	// {"verb":"update","docs":[...]} fusion otherwise builds an empty diff, renders
	// "No field changes." with Confirm ENABLED, and sends update_doc(dt, "", {}).
	// A hybrid carrying BOTH fields and docs throws too: rendering the fields and
	// dropping the docs is a half-card the user confirms believing it is the set.
	if (Array.isArray(a.docs)) {
		const err = new Error(
			"This draft carries a `docs` batch, which is a create_doc payload rather than a card. Ask me to apply them as a batch.")
		err.jvUserMessage = err.message
		throw err
	}
	// Create-only: a fieldless UPDATE block legitimately renders "No field changes.",
	// and a tables-only create is legal, so neither may throw.
	if (verb === "create" && !(a.fields || []).length && !(a.tables || []).length) {
		const err = new Error("This draft has no fields to show.")
		err.jvUserMessage = err.message
		throw err
	}
```

and in `ensureActionSummary`, surface a specific message rather than the generic one:

```js
	} catch (e) {
		const msg = (e && e.jvUserMessage) || "Could not load this draft. Tell me to try again."
		if (summaryState.value.key === key) summaryState.value = { key, model: null, view: null, error: msg }
		return
	}
```

**Scope note — this does not tell the agent.** Closing that loop needs a continuation turn or a
`[Context:]` line, which is a separate change. Fix 1 stops the shape being emitted; fix 2 stops it
being invisible when it is. **Deliberately not doing** the agent-feedback channel here.

**This is the precondition for "Jarvis analyses errors and suggests a fix".** That behaviour already
exists — `AGENTS.md:139` (*"on FAILED I surface the error verbatim and propose the fix"*) and
`_translate_write_error`'s `{ok:false, error}` envelope with `message`/`detail`/`hint`. It did not
fire here because **nothing failed**. Error analysis only works on failures that are visible as
failures.

## Fix 3 — split `auto`: "Frappe computes it" vs "Frappe guesses it"

`jarvis/tools/get_creation_context.py:242`:

```python
"auto": bool(df.fetch_from) or (df.default not in (None, "")),
```

One flag, two meanings, and the note says *"skip `auto`/`readonly` fields (Frappe fills those)"*:

- `assigned_by_full_name` — `fetch_from`. Frappe **computes** it. Genuinely skip.
- `date` — `default: "Today"`. Frappe **guesses** it. That is a decision, and it is made silently.

Those three ToDos are created **due today** and the user never sees it. "Valid without a value" and
"the user does not care about the value" are different claims; this line treats them as one.

**Change** — note this file is **4-SPACE indented**, not tabs. `jarvis/tools/*.py` is 4-space while
`jarvis/chat/*.py` and `jarvis/api.py` are tabs; CLAUDE.md's "ruff uses tabs" describes the *config*,
not the files. A tab-indented block typed in here raises `TabError`. Verify with `cat -t` before
writing.

```python
        rec = {
            "fieldname": df.fieldname,
            "label": df.label,
            "fieldtype": df.fieldtype,
            "options": df.options,
            "mandatory": bool(df.reqd),
            # `auto` means Frappe COMPUTES this (fetch_from) - skipping it is safe.
            # A field with a DEFAULT is different: Frappe GUESSES it, and the guess is
            # a decision the human never sees unless it reaches the draft. Surface the
            # default value and let the model decide, rather than hiding both.
            "auto": bool(df.fetch_from),
            "readonly": bool(df.read_only),
        }
        if df.default not in (None, ""):
            rec["default"] = df.default
```

and the note (`get_creation_context.py:159-162`) is rewritten — **not** merely extended:

> "Skip `auto`/`readonly` fields (Frappe computes those). A field with a `default` is NOT auto -
> Frappe applies the default silently, so decide it yourself: leave it out to accept the default,
> set it when the default is wrong for this record; a symbolic default (`Today`, `:Company`) is
> resolved by Frappe - never copy the token as a value. Set every **remaining** `mandatory` field
> before create_doc. Ask the user only about mandatory fields you cannot determine."

**Two traps this wording exists to avoid — note the ORDER is load-bearing:**

1. **The reqd+default double-bind.** Sales Order's `order_type` (reqd, default "Sales"),
   `transaction_date` (reqd, default "Today") and `status` (reqd, default "Draft") all flip from
   `auto: true` (skip) to **mandatory-and-not-auto**. The original note leads with *"Set every
   `mandatory` field that is not already `auto`"* — which then **commands** setting them, while the
   decide-clause **permits** leaving them: two rules, same field, opposite directions. A previous
   draft of this spec softened the clause and claimed the contradiction was resolved. **It was not**
   — the command was never qualified. The fix is structural: state the default rule **first**, then
   scope the mandatory command to every **remaining** field. Now all four cases resolve —
   reqd+no-default (command applies), reqd+default (default rule claims it first), optional+default
   (decide-clause), optional+no-default (untouched, governed by `:131`'s "set only what the user
   gave").
2. **Symbolic defaults.** `df.default` is frequently a token, not a value (`Today`, `:Company`,
   `0`). Shipping it raw invites a model to copy `"Today"` into a Date field, which fails downstream.
   Frappe resolves the token itself, so the note says accept-or-override — never copy.

**Known risk, stated plainly.** This is the same shape as `get_schema`'s `actions` block and PR
#343's docfield properties: **data surfaced to the model that the model may simply not use.** Both
of those shipped correct and unreachable. The mitigation is that this one lands on a field map the
agent demonstrably *does* read (this very transcript shows it consuming `mandatory`/`auto` from it),
which neither of those did. It is still a bet.

**Not in scope:** rendering Frappe-applied defaults on the card. For the gated `_create_card` the
data exists (`would` carries the resolved doc) but is filtered to the agent's `values` keys; for the
draft card no resolved doc is fetched at all on a create (`base.values` is `{}`). Both are real, both
are bigger, and neither is needed if fix 3 puts the default in the draft.

## Testing

**Fix 2** — `frontend/src/lib/actionSummary.test.js` exists (`node --test`); `buildDraftModel` lives
in `ChatView.vue` and is not exported, so it has no unit surface. Verify by compiling and by manual
smoke. **CI does not run node tests** (`ci.yml` is Python-only).

Fix 2 spans **two frontends**, so the smoke does too:
- **Desktop:** a docs block renders the error card, not a blank one; **Edit and Preview do not die**
  (the throw is caught, the button is disabled); a normal single-record draft still renders.
- **PWA:** a docs block renders the error inside the card with the primary button **disabled** and
  **Cancel still live**; a fields-empty create likewise; the block never vanishes from the prose.

**Fix 3** — `jarvis/tests/test_get_creation_context.py` **exists** (an earlier draft hedged "if
present", which just meant nobody looked). Its shape assertion at `:40` is a subset check
(`assertLessEqual`), so the conditional `default` key passes it. Add:
- a `fetch_from` field is `auto: true` and carries no `default`
- `ToDo.date` (`default: "Today"`) is `auto: false` and carries `default: "Today"`
- `ToDo.description` (`reqd`) is still `mandatory: true` (it is computed from `df.reqd`
  independently of `auto`, so the change is isolated)
- a reqd+default field (Sales Order `transaction_date`) is `mandatory: true, auto: false`
- the note contains the decide-it-yourself and symbolic-default clauses

**Regression: answerable, and the answer is "nothing in code".** No app code reads `auto` back from
this tool — the only consumers are the model (`AGENTS.md:127`, `TOOLS.md:25`) and the plugin's
tool description (`tool-defs.ts:158`), all of which stay textually valid. So the blast radius is
entirely behavioural, not structural.

Run: `cd bench && bench --site jarvis-test.localhost run-tests --app jarvis --module jarvis.tests.<module>`

**Fix 1** — `cd jarvis-persona && ./lint.sh && python validate.py`, and confirm AGENTS.md stays
under 32,000.

## Rollout

Fixes 2 and 3 are **app-only** — no plugin, no container restart. Fix 1 is a **persona** change: it
needs a `git pull` on the persona checkout plus a container restart per tenant (a brief chat blip),
so batch it with the next persona roll rather than forcing an outage for a sentence.

Fix 3 changes what the model sees, so its benefit only appears once tenants pick it up.

## What the user actually saw — corrected

An earlier draft said "a card with one empty Description box". **That is the Edit-panel /
DraftPreview surface, not the summary card.** On the summary card a docs block renders the
model-written headline ("3 open ToDos assigned to Administrator", `actionSummary.js:30` reads
`action.summary`), **zero rows**, and an **enabled Confirm** (`ChatView.vue:296` only checks
`!summaryState.model`, and a model *was* built). The empty Description box appears once you click
Edit or Preview — both fed by `buildDraftModel`.

The mechanism in this spec is right; the surface attribution was not. **The manual smoke must check
all three surfaces** — summary card, Preview, Edit panel — not just the one. Note the pre-fix
Confirm path does fail visibly on its own: `apply_action` → `create_doc(ToDo, {})` → the
mandatory-field error envelope (`actions_api.py:187-247`). So the bug is "silent until you commit
to it", not "silent forever".

## Open

- **Card display of Frappe-applied defaults** (above, "not in scope").
- **The agent-feedback channel for fix 2** — nothing tells the agent its block failed to render.
- **The `kind:"email"` sibling.** The same fusion failure exists there: a model emitting a
  `messages:[...]` bulk-email shape renders a card with empty To/Subject/body
  (`ChatView.vue:229`). Lower stakes since the Send button is gone (legacy note, `:242-246`), but it
  is the same silent-blank family, and `docs` is unlikely to be the only key a model will invent.
  The generic no-fields guard in fix 2 catches the create case; the email kind has no equivalent.
- **The PWA drops child tables on apply — pre-existing, found while specifying fix 2.**
  `ActionCard.apply()` sends only `values` derived from `action.fields` (`:53-81`); it never reads
  `action.tables`, which the desktop's `applyDraft` does (`ChatView.vue:2360-2366`). So a
  fields+tables draft — AGENTS.md's own Sales Order example — applied **from the phone** writes the
  parent without its rows. Where the ERP mandates the table (SO items) it errors visibly; where it
  does not, it is a **silent partial write**. Same family as the bug this spec kills, strictly out
  of scope, recorded so it is not lost.
