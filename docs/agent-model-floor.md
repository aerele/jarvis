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
   Keyed pushes need fleet contract 1.43 and the container's
   `render_context.json` (integration-status `render_context_present`).
4. The fleet renders the delegate pinned to that model, or omits it (fail
   closed) when the model cannot be placed.

A direct-mode tenant (no pool) can only pin its configured model: it is the one
model the platform renders for it. If that model is below an agent's tier, the
agent is `needs_model` until an eligible provider is connected.

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
| `apply_in_progress` | The container was being reconfigured for longer than the run start waits (any tenant, flag on or off) | Retry in a minute; a scheduled run retries on the next sweep |
| `render_context_missing` | The container has no `render_context.json` (never re-rendered since its host reached 1.43) | Run a no-op reapply for the tenant (`bulk_reapply_config`), then retry |
| `delegate_blocked` | Admin/fleet marked the pinned model unusable | Row moves to `needs_model`; pick another model, Apply |
| `delegate_model_unresolved` | The pushed model is not in the current render | Apply catalog changes; if it persists, check the pool still serves that provider |
| `platform_upgrade_pending` on a tenant Apply (409) | Host fleet-agent below 1.43, or the container has no render context yet. Nothing was pushed or saved on admin | Upgrade the host (admin already queued a no-op reapply for a missing context), then Apply catalog changes again. Reconcile does not retry an Apply |
| `platform_upgrade_pending` after a move / reconcile | Drain or reconcile stripped the choices (`agent_roster_models_pending = 1`, host event `agent_model_choice_stripped`); delegates run on the pool default | None: reconcile re-arms automatically once the host is on 1.43 and the render context exists |

A scheduled run refused for a model reason (`delegate_*`, `render_context_missing`)
records one failed run and one owner notice for that slot; it does not retry
every hour.

## Below-minimum runs

The choice is best-effort: the LLM proxy can still fail over to another model.
Each run records `model_rendered` (from the fleet at start) and `model_used`
(what the runtime reported). For pinned runs, `model_below_min = 1` when the
used model differs from the rendered one or is not eligible for the agent.
Unpinned (`legacy`, pool default) runs are never flagged. The Runs board and the
findings panel show it.

## Admin fields (Jarvis Tenant)

- `agent_roster_models`: last pushed logical choices, carried across moves.
- `agent_roster_models_pending`: a drain or reconcile push stripped the choices
  because the host was below 1.43 or the container had no render context;
  reconcile re-pushes once both are true. If the queued no-op reapply is itself
  blocked (for example an OAuth pool with no stored sign-in), the context is
  never written and the tenant stays pending until that is fixed.
- `agent_roster_models_digest`: what the last keyed push delivered; an LLM apply
  re-pushes only when it would change.

## Rollout

1. Admin catalog fields (capability tiers) deploy first; nothing reads them yet.
2. Fleet-agent 1.43 on every host.
3. `bulk_reapply_config` for all tenants, then check each container's
   integration-status `render_context_present` (a keyed push also queues the
   no-op reapply itself, but the bulk pass avoids first-Apply refusals).
4. Admin relay and floor.
5. Curate tiers: set `capability_tier` on every model tenants use. A model_id's
   api-key and subscription rows must carry the same tier (the save refuses
   otherwise), so edit both lanes together. Uncurated models are never
   eligible, so a tenant turned on before curation has every install blocked.
6. Store lint and registry with `min_model`, then the tenant app (flag off).
7. Turn the flag on per tenant, canary first. The flag is operator-only
   (permlevel 2): as Administrator on the tenant site,
   `frappe.db.set_single_value("Jarvis Settings", "enforce_agent_min_model", 1)`
   followed by `jarvis.chat.agent_models.on_enforcement_enabled()` and a commit,
   or save it from Desk as Administrator. Tenant System Managers can see it but
   not change it.

## Rollback

Turn `enforce_agent_min_model` off and Apply catalog changes. The push carries
no model keys, admin clears the tenant's `agent_roster_models`, and delegates
return to the pool default. Choice rows are kept for when it is turned back on.
