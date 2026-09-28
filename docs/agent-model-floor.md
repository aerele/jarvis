# Agent minimum model

A marketplace agent can declare `min_model: {"tier": "Standard" | "Advanced" | "Frontier"}`.
The admin catalog ranks each model by an operator-curated `capability_tier`; a
model is eligible for an agent when its rank meets the agent's tier and the
tenant's own connected pool can serve it.

Each agent runs on one model for the whole workspace, stored as a
`Jarvis Agent Model Choice` row (no DocPerm; read and written only through
`jarvis.chat.agent_models`). Any installer can change it; the change is audited
in the agent activity log.

Everything is inert unless `Jarvis Settings.enforce_agent_min_model` is on.
With it off no row is created, no model keys are pushed, and no run is gated.

## How a choice reaches the container

1. Install, pick, or re-validation updates the row and marks the catalog dirty.
   Nothing pushes or restarts.
2. The next reviewer **Apply catalog changes** pushes the agent roster with
   `model_choice` / `model_fallbacks` / `model_state` keys.
3. Admin checks each choice against the tenant's own models, canonicalizes the
   provider for the tenant's current render shape, and relays to the fleet.
   Keyed pushes need fleet contract 1.43.
4. The fleet renders the delegate pinned to that model, or omits it (fail
   closed) when the model cannot be placed.

## Row states

| State | Meaning | Operator action |
| --- | --- | --- |
| `legacy` | Installed before enforcement; runs on the pool default | None. Picking a model moves it to `chosen` |
| `auto` | Jarvis picked the best eligible model | None |
| `chosen` | A person picked it | None |
| `changed` | The pick stopped being eligible; moved to a stored fallback | Check the note; pick again if needed |
| `needs_model` | No eligible model; runs are refused | Connect a provider with an eligible model, pick it, Apply |

`fleet_unresolved = 1` means the fleet could not place the pushed model in its
render. It clears on the next successful run start. A non-empty
`admin_rejected` means admin refused the model for this pool; it expires on the
next pool sync or when the pool fingerprint changes.

## Run errors

| Run error / token | Cause | Fix |
| --- | --- | --- |
| "needs … model and none is available" | Row is `needs_model` (checked before the run starts) | Connect an eligible provider, pick a model, Apply |
| `apply_in_progress` | A config apply is running on the host | Retry in a minute |
| `render_context_missing` | Host never wrote `render_context.json` (pre-1.43 render) | Run a no-op reapply for the tenant, then retry |
| `delegate_blocked` | Admin/fleet marked the pinned model unusable | Row moves to `needs_model`; pick another model, Apply |
| `delegate_model_unresolved` | The pushed model is not in the current render | Apply catalog changes; if it persists, check the pool still serves that provider |
| `platform_upgrade_pending` (Apply, 409) | Host fleet-agent is below contract 1.43 | Upgrade the host fleet-agent; reconcile re-arms the choices |

## Below-minimum runs

The choice is best-effort: the LLM proxy can still fail over to another model.
Each run records `model_rendered` (from the fleet at start) and `model_used`
(what the runtime reported). `model_below_min = 1` when the used model differs
from the rendered one or is not eligible for the agent. The Runs board and the
findings panel show it.

## Admin fields (Jarvis Tenant)

- `agent_roster_models`: last pushed logical choices, carried across moves.
- `agent_roster_models_pending`: a drain or reconcile push stripped the choices
  because the host was below 1.43; reconcile re-pushes once the host upgrades.
- `agent_roster_models_digest`: what the last keyed push delivered; an LLM apply
  re-pushes only when it would change.

## Rollback

Turn `enforce_agent_min_model` off and Apply catalog changes. The push carries
no model keys, admin clears the tenant's `agent_roster_models`, and delegates
return to the pool default. Choice rows are kept for when it is turned back on.
