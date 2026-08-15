"""Backfill Jarvis LLM Pool Model rows collapsed into "openai_compat" that were
actually GLM / Z.ai (either the pay-as-you-go or the Coding Plan product),
restoring their first-class "zai" / "zai_coding" provider id.

Root cause (fixed in the same deploy as this patch): jarvis.onboarding.save_llm_pool
normalized the incoming provider label BEFORE persisting it into
Jarvis Settings.models[].provider, and pool_serialize._PROVIDER_ALIASES mapped
"glm / z.ai" straight to "openai_compat" - a different provider's canonical id,
unlike every other alias in that table (which maps a label to ITS OWN id). So a
row saved as "GLM / Z.ai" was permanently stored as provider="openai_compat";
the row's `model` (glm-4.6) and `base_url` (https://api.z.ai/...) survived, but
the Provider dropdown and the AI-models row chip both rendered
"OpenAI-Compatible" on every reload, with no way for the customer to tell the
row was ever GLM without re-checking the base_url.

The fix moves that collapse to WIRE-serialization time only (inside
pool_serialize.build_pool_payload, via _wire_provider()), so new saves store
the first-class "zai"/"zai_coding" id and the label renders correctly, while
the Bifrost wire payload is unchanged (still emits "openai_compat" + base_url
for either - Bifrost has no native provider for either z.ai product).

z.ai sells two distinct products on two distinct endpoints, and both were
folded into the same "openai_compat" bucket by the old bug:
- pay-as-you-go API credits: https://api.z.ai/api/paas/v4        -> "zai"
- GLM Coding Plan subscription: https://api.z.ai/api/coding/paas/v4 -> "zai_coding"
(A Coding Plan key authenticates fine but reports "insufficient balance" on
the pay-as-you-go endpoint - a different, unrelated bug this patch does not
fix; it only restores whichever provider id the row's base_url actually names.)

This patch backfills rows written under the old (storage-collapsing) code:
provider == "openai_compat" AND base_url points at a Z.ai host (api.z.ai) ->
provider = "zai_coding" if the URL path is the coding-plan one, else "zai".
Path match is a simple "/coding/" substring check - both known coding-plan
URL shapes ("https://api.z.ai/api/coding/paas/v4" and any future path under
that same coding-plan family) contain it, and the pay-as-you-go path
("/api/paas/v4") never does.

Conservative by design: a genuine openai_compat row (a Claude-CLI gateway
shim, a local relay, anything NOT hosted at api.z.ai) is left untouched -
matching on base_url host, not merely "has a base_url", avoids
misclassifying unrelated custom-endpoint rows as GLM.

Direct field update via frappe.db.set_value on the child row, bypassing
Jarvis Settings.save()/on_update entirely (mirrors v1_seed_llm_models's
insert-not-save pattern): this is a label-only correction of already-applied
config, so it must not re-validate, re-render openclaw.json, or make any
admin/fleet-agent network call during bench migrate.
"""

import frappe

# The Z.ai API host - shared by both products, only the path differs.
_ZAI_HOST = "api.z.ai"
# Substring present in every known GLM Coding Plan endpoint path, absent from
# the pay-as-you-go path ("/api/paas/v4").
_ZAI_CODING_PATH_HINT = "/coding/"


def _zai_provider_for_base_url(base_url: str) -> str | None:
	"""Return the canonical provider id a collapsed openai_compat row's
	base_url actually names ("zai" or "zai_coding"), or None if it isn't a
	Z.ai endpoint at all (a genuine openai_compat row - left untouched)."""
	url = (base_url or "").strip().lower()
	if _ZAI_HOST not in url:
		return None
	if _ZAI_CODING_PATH_HINT in url:
		return "zai_coding"
	return "zai"


def execute():
	rows = frappe.get_all(
		"Jarvis LLM Pool Model",
		filters={"parenttype": "Jarvis Settings", "provider": "openai_compat"},
		fields=["name", "base_url"],
	)
	for row in rows:
		target = _zai_provider_for_base_url(row.base_url)
		if target:
			frappe.db.set_value(
				"Jarvis LLM Pool Model",
				row.name,
				"provider",
				target,
				update_modified=False,
			)
	frappe.db.commit()
