# Settings: dismissing a confirm no longer closes Settings too - 2026-07

Evidence for jarvis#452, captured live on the running bench (`jarvis.proxy`, an
onboarded tenant, real deployed build).

Verification required `fix/confirm-dialog-pointer-events` (#451, still a draft
owned by a different session) applied on top of `develop`, because before #451
the shell `ConfirmDialog` is not clickable at all, so #452 cannot be reproduced
or verified. The screenshots below were captured from a throwaway scratch
merge of `develop` + #451 (+ this PR's commit for the "after" set), built and
swapped into the live checkout's build output only, then discarded. This PR's
branch itself contains only the `SettingsDialog.vue` change; it does not
depend on #451 landing first.

| # | Screenshot | What it shows |
|---|---|---|
| 01 | `before-01-dark-cancel-closes-settings-too.jpg` | **The defect**, dark theme. `develop` + #451, no fix. Settings was open on General > Danger zone with the "Delete ALL chat history?" confirm on top. After clicking **Cancel**, the confirm is gone *and* Settings has closed too, dropping back to the raw chat view. Cancel correctly did not delete anything, but it also threw the user out of Settings. |
| 02 | `after-01-dark-confirm-open.jpg` | With the fix, same surface: the "Delete ALL chat history?" confirm open on top of Settings, dark theme. |
| 03 | `after-02-dark-cancel-settings-stays-open.jpg` | After clicking **Cancel**: the confirm is dismissed and Settings **stays open**, still on General, still scrolled to Danger zone. This is the fix. |
| 04 | `after-03-light-confirm-open.jpg` | Same confirm, light theme. Confirms the fix does not depend on theme, and that the confirm and settings dialog both still resolve their `jv-` palette correctly (white surface, dark text, red danger button, no unstyled/transparent regions). |
| 05 | `after-04-light-cancel-settings-stays-open.jpg` | Light theme after Cancel: Settings stays open on Danger zone, fully styled. |

## Root cause

Confirmed by reading frappe-ui's `Dialog.vue` and reka-ui's `DismissableLayer`/
`DialogContentModal`, not just the issue's description. The shell
`ConfirmDialog` is teleported to `<body>` (#438), so it is a DOM sibling of the
settings dialog's `DialogContent`, not a descendant. reka's dismissable layer
treats any `pointerdown` outside `DialogContent` as an "outside interaction"
and, unless `disableOutsideClickToClose` is set, dismisses the layer once the
interaction is not prevented. Clicking anything inside the teleported confirm,
including Cancel, is such a pointerdown, so it closed Settings too.

The issue's suggested literal fix (add an `@interact-outside` handler on the
settings `<Dialog>`) does not work: frappe-ui's `Dialog.vue` binds
`@interact-outside` internally on its own `<DialogContent>` and only ever
calls `e.preventDefault()` there based on its own `disableOutsideClickToClose`
prop. It never re-emits or forwards that event to its own callers, so a
listener attached from `SettingsDialog.vue` would never fire.

## The fix

`SettingsDialog.vue` now binds `:disable-outside-click-to-close` on `<Dialog>`
to a `confirmOpen` computed (`confirmState.value !== null`, from the shared
`useConfirm` state), instead of leaving it unset. This is the prop frappe-ui's
own `Dialog.vue` already reads before deciding whether to prevent an
interact-outside dismissal, used the way it is designed to be used, just made
reactive instead of a fixed `false`.

- When no confirm is open, `disableOutsideClickToClose` is `false`, so a plain
  click on the settings backdrop still closes Settings (the #405-verified
  behaviour - see the regression check below).
- While a confirm is open, `disableOutsideClickToClose` is `true`, so no
  outside interaction closes Settings. Since the confirm's own full-viewport
  overlay covers the real settings backdrop while it is open, this does not
  block any interaction a user could otherwise reach.

## Regression check: plain backdrop click must still close Settings

Not screenshotted (a closed dialog has nothing to show), but verified live in
both themes: with no confirm open, clicking outside the settings dialog
content closes Settings exactly as before this change.

## Also verified, not separately screenshotted

- **Escape** and the **confirm's own backdrop click** both dismiss only the
  confirm, leaving Settings open on the same pane - same as Cancel.
- All three in-settings confirm surfaces the issue names: General > Danger
  zone > Delete all, Plan and billing > Cancel subscription, AI models >
  Remove. All three: confirm dismisses, Settings stays open on the same pane,
  the guarded action does not happen (chat history intact, subscription still
  Active, model still listed).
