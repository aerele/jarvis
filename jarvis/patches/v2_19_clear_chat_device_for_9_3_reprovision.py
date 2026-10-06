"""9.3 reprovision: restore the bench-owned state the fresh container came up empty of.

The 6.8 -> 9.3 upgrade rebuilds each tenant's agent container FRESH (the old agent state
can't migrate in place). Two things that live ONLY on the bench are lost to the new
container and are restored here, proactively on the post-reprovision app upgrade -- the
operator reprovision never runs the customer-driven reset poll that normally drives this:

1. **Chat-device pairing.** The stored chat-device token is stale (the new container has
   no record of it), so every connect fails with "device token mismatch". Clearing the
   four chat_device_* credentials flips ``device.needs_bootstrap`` True, so the bench's
   next connection re-pairs cleanly -- Mechanism A on a 9.3 container, or the legacy forge
   if it is somehow still 6.8 (the re-pair adapts to the tenant's image). Firing here is
   proactive: the re-pair no longer depends on the reactive device_token_mismatch
   self-heal, nor on the customer chatting first.

2. **Skills + LLM credential.** The control plane re-carries its own config, but the
   custom/learned skills and the LLM credential live ONLY on the bench (the LLM-key
   invariant -- admin holds no copy). ``_resync_after_rebuild`` re-pushes the skills
   (always) and re-drives the LLM config (when one exists); both legs enqueue + dedupe and
   never raise. Without this the fresh container would pair but have no model creds or
   skills, because the resync's normal trigger -- the ``workspace_reset_state`` poll -- is
   customer-SPA-driven, and an operator-initiated upgrade never fires it.

One-time (patches run once, tracked). Idempotent. ``chat_device_token`` /
``chat_device_private_key`` are Password fields (in __Auth); ``db.set_value`` clears those
and the two plain fields. Dropping the keypair is safe: bootstrap regenerates a stable one.
The connect-time self-heal still covers any future stale-token case; this de-risks the roll.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "Jarvis Settings"):
		return
	# 1. Clear the stale chat-device pairing -> clean re-pair on the next connection.
	frappe.db.set_single_value(
		"Jarvis Settings",
		{
			"chat_device_id": "",
			"chat_device_public_key": "",
			"chat_device_token": "",
			"chat_device_private_key": "",
		},
	)
	frappe.clear_cache(doctype="Jarvis Settings")
	# 2. Re-push the bench-owned skills + LLM credential the fresh container lacks (CP
	#    carries its own config; these live only on the bench). Enqueued; never raises.
	from jarvis.onboarding import _resync_after_rebuild

	_resync_after_rebuild(frappe.get_single("Jarvis Settings"))
