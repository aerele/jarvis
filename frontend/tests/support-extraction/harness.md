# Support-extraction verification harness

Copy-run this checklist for every extraction task (Tasks 2-4) in
`docs/superpowers/plans/2026-07-24-support-ui-pr1-extraction.md`. It backs up
the "chat must be byte-identical" constraint three ways: a vitest snapshot
(automated), a mechanical CSS orphan-sweep (automated grep, manual read), and
a manual light/dark/mobile pass (human).

## (a) Orphan-sweep procedure

When a template block moves out of `ChatView.vue` into a shared component,
every CSS rule that targeted it must move too — nothing should be left
behind "orphaned" (dead) in `ChatView.vue`'s `<style scoped>`, and nothing
should be missing from the new component's `<style scoped>`.

For every `class="…"` token used in the moved template:

1. `grep -n '<class>' src/views/ChatView.vue`
2. For each hit inside the `<style scoped>` block, confirm the class is not
   the **last selector element** of any remaining rule (e.g. `.jv-umsg:hover
   .jv-msgbar` is a hit on `jv-msgbar` but is fine to leave if `.jv-msgbar`
   itself moved — the *trigger* class staying in ChatView is expected; the
   *rule that paints `.jv-msgbar`* must not remain).
3. Explicitly re-run the grep against:
   - the dark-mode block (`.jv-dark …`, roughly `~10589-10700` — locate by
     content, line numbers drift)
   - every `@media` block (hover-capability, max-width breakpoints)
4. Confirm the same rules exist, verbatim or adapted, in the new component's
   `<style scoped>`.
5. Zero orphaned rules is the bar — not "close enough".

## (b) Manual pass

Exercise the task's interaction checklist (see the task's own "Orphan sweep
+ manual pass" step in the plan) in all three of:

- **Light** theme
- **Dark** theme
- **Mobile** width (≤720px)

Compare against `develop` (pre-extraction) side by side or from memory of
the last-verified state. The result must be indistinguishable — same
spacing, same colors, same hover/focus states, same behavior. Any visual or
behavioral diff is a bug in the extraction, not an acceptable regression.
