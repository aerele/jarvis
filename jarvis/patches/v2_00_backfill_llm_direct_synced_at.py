"""Backfill Jarvis Settings.llm_direct_synced_at for pre-marker direct tenants.

The marker ("a DIRECT single-model config has been CONFIRMED-applied to the
container at least once") was introduced with the round-4 R4-P0-6 / P1-10 fix:
is_ready_for_chat's api_key branch used to admit chat from local
key/provider/model presence alone — which is config INTENT committed at save,
before the async admin apply runs — so a first apply still "applying" (busy
lock / read-timeout / CAS refusal) opened chat on a container still on the stub.
The direct-sync worker now stamps this marker only on a confirmed admin
status="applied", and the gate reads it.

Grandfather rule (mirrors v1_10_backfill_llm_pool_synced_at): a pre-marker
DIRECT api-key tenant (proxy_active=0, auth_mode=api_key) with local creds
(llm_api_key + llm_provider + llm_model) whose sync has EVER reached a terminal
state (last_sync_at set) is stamped — before this deploy the gate was local
presence alone, so an existing serving direct tenant whose LATEST re-save is
transiently "pending"/"failed" at migrate time (while the container keeps
serving its previous key) was chat-ready and must stay so.

NOT stamped: a tenant with no completed sync (last_sync_at empty) — its creds
have demonstrably never been applied; leaving it unstamped routes it to
onboarding rather than a chat surface where every turn fails, and its marker
sets on the first confirmed sync.

Reads go through the document API, not frappe.db.get_single_value, for the same
empty-Datetime coercion reason documented in v1_10.
"""

import frappe


def execute():
	settings = frappe.get_single("Jarvis Settings")
	if getattr(settings, "proxy_active", 0):
		return  # pool tenant — llm_pool_synced_at owns its gate
	if settings.llm_direct_synced_at:
		return
	auth_mode = (getattr(settings, "llm_auth_mode", "") or "api_key").strip()
	if auth_mode != "api_key":
		return  # oauth/subscription gate on llm_oauth_connected_at, not this marker
	key = (settings.get_password("llm_api_key", raise_exception=False) or "").strip()
	provider = (getattr(settings, "llm_provider", "") or "").strip()
	model = (getattr(settings, "llm_model", "") or "").strip()
	if not (key and provider and model):
		return  # not a configured direct tenant
	if not settings.last_sync_at:
		return  # no sync ever completed: nothing to grandfather
	frappe.db.set_single_value("Jarvis Settings", "llm_direct_synced_at", settings.last_sync_at)
