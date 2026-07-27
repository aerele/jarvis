# Desk setup nudge survives onboarding - 2026-07

Evidence for jarvis-admin-v2#46 ("Setup jarvis prompt is being show post
onboarding"), captured from the running bench on `jarvis.proxy`, a tenant that
is fully onboarded (`jarvis.account.is_ready_for_chat` returns
`{"ready": true}`).

Kept here so the PR can point at the exact defect, and so a future reader can
see why the nudge is not allowed to trust `frappe.boot` alone.

| # | Screenshot | What it shows |
|---|---|---|
| 01 | `before-01-nudge-survives-stale-boot.jpg` | **The defect.** With `frappe.boot.jarvis_onboarded` forced back to `false` to reproduce a Desk that booted before setup finished, the "Set up Jarvis" bubble renders and then survives every route change, because `sync()` re-reads the same frozen boot value. In the reported flow the stale value arrives for real: the user opens the Desk, goes to /jarvis, completes onboarding, and presses Back, so the browser restores the Desk from bfcache and no `boot_session` runs. Only a hard reload cleared it. |
| 02 | `state-02-desk-clean-on-fresh-load.jpg` | The same Desk on a **fresh** load: `boot.jarvis_onboarded: true`, no nudge in the DOM, no dismissal flag in sessionStorage. Confirms the tenant state was never the problem and the nudge is correct when the flag is current. |
| 03 | `state-03-widget-chat-ready.jpg` | The floating Desk widget on the same tenant opens straight into chat with a composer, no setup nudge. The readiness gate added in #417 classifies this workspace as `ready`. |
| 04 | `state-04-spa-no-onboarding-gate.jpg` | The full SPA at `/jarvis`: no `.jv-gate` poster, no "finish setting up" copy. The third setup surface is clean too. |

## Why there is no "after" screenshot

The fix's whole point is that the nudge disappears, so an "after" image is an
empty Desk indistinguishable from #02. The observable proof is the transition,
captured live against the real endpoint with the flag forced stale:

```
synchronous, right after the bfcache restore -> flag false, nudge present
+3s, after the server answered               -> flag true,  nudge gone
POST /api/method/jarvis.account.is_ready_for_chat   200
```

Before this change the second line never came: the nudge stayed through every
route change and every tab return until a hard reload.
