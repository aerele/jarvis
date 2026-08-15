// The pool-mode "Auto" sentinel model id. Mirrors
// jarvis.chat.turn_handler.POOL_VIRTUAL_MODEL on the bench: an unpinned
// BIFROST-fronted pool conversation's agent session gets patched to this
// value (turn_handler._session_model_for), so those Auto-routed turns are
// recorded under it in jarvis.chat.usage - Bifrost's actual per-request model
// choice never comes back to the bench. An agent-direct pool instead RESETS
// its session and is attributed to the model the gateway really resolved. This
// is NOT a real model id and must never be shown to a customer verbatim - see
// modelDisplayLabel below.
export const POOL_VIRTUAL_MODEL = "jarvis-pool";

// Human label for a model id as it should appear in usage UI (per-model
// bars in UsagePane.vue, the admin per-user drill-down and cap editor in
// UsageAdminPane.vue). Only the pool auto-routed sentinel is special-cased
// today; every other model id renders verbatim. Centralised here so the two
// panes can't drift on the wording.
export function modelDisplayLabel(model) {
	return model === POOL_VIRTUAL_MODEL ? "Pool (auto-routed)" : model;
}
