"""Move a site off the OpenAI subscription models retired upstream.

gpt-5.4 and gpt-5.4-mini were retired from the ChatGPT/Codex catalog: the tenant's
cliproxy answers "unknown provider" for them (measured 2026-09-06 on the pinned
binary). Three places can still hold such an id, and every one is forwarded as-is:

- ``Jarvis Conversation.model_override`` (send_message only re-validates a FRESH
  override): cleared, so the conversation returns to the workspace default.
- ``Jarvis Settings.llm_model`` (the workspace default; turn_handler resolves
  ``conv.model_override or settings.llm_model``): repointed to the tier default.
- an enabled subscription pool row whose ``model`` is retired: repointed to the tier
  default so the account entry it carries is kept.

Scoped to sites that use an OpenAI chat subscription, either directly
(``llm_auth_mode`` oauth/subscription with an OpenAI provider) or through an
enabled subscription pool row whose accounts sit on the ``openai`` upstream. An
api-key site keeps everything: the OpenAI API still serves these ids.

Data only. No document hooks and no sync is enqueued (a fleet apply must not run
inside ``bench migrate``), so a repointed workspace default or pool row reaches the
container on the tenant's next LLM settings save or operator resync. The patch logs
what it changed so those tenants can be found.
"""

import json

import frappe

RETIRED_SUBSCRIPTION_MODELS = ("gpt-5.4", "gpt-5.4-mini")
REPLACEMENT_MODEL = "gpt-5.6-terra"  # the OpenAI subscription default when this patch shipped
_SUBSCRIPTION_MODES = {"oauth", "subscription"}
_OPENAI = {"openai", "openai-codex"}


def execute():
	if not _site_uses_openai_subscription():
		return
	cleared = frappe.db.sql(
		"""UPDATE `tabJarvis Conversation` SET model_override = NULL
		WHERE model_override IN %(retired)s""",
		{"retired": RETIRED_SUBSCRIPTION_MODELS},
	)
	repointed_default = _repoint_workspace_default()
	repointed_rows = _repoint_subscription_pool_rows()
	if repointed_default or repointed_rows:
		frappe.clear_cache(doctype="Jarvis Settings")
	frappe.logger().info(
		"retired OpenAI subscription models: cleared pins=%s, workspace default repointed=%s, pool rows repointed=%s",
		cleared,
		repointed_default,
		repointed_rows,
	)


def _site_uses_openai_subscription() -> bool:
	return _direct_lane_is_openai_subscription() or _pool_has_openai_subscription()


def _direct_lane_is_openai_subscription() -> bool:
	mode = (frappe.db.get_single_value("Jarvis Settings", "llm_auth_mode") or "").strip().lower()
	provider = (frappe.db.get_single_value("Jarvis Settings", "llm_provider") or "").strip().lower()
	return mode in _SUBSCRIPTION_MODES and provider in _OPENAI


def _repoint_workspace_default() -> bool:
	"""``llm_model`` is repointed only when the direct lane itself is an OpenAI
	subscription; on an api-key direct lane that happens to carry a subscription
	pool row, the default names an id the OpenAI API still serves."""
	if not _direct_lane_is_openai_subscription():
		return False
	model = (frappe.db.get_single_value("Jarvis Settings", "llm_model") or "").strip()
	if model not in RETIRED_SUBSCRIPTION_MODELS:
		return False
	frappe.db.set_single_value("Jarvis Settings", "llm_model", REPLACEMENT_MODEL)
	return True


def _repoint_subscription_pool_rows() -> list[str]:
	repointed = []
	for row in _openai_subscription_pool_rows():
		if (row.model or "").strip() not in RETIRED_SUBSCRIPTION_MODELS:
			continue
		frappe.db.set_value(
			"Jarvis LLM Pool Model", row.name, "model", REPLACEMENT_MODEL, update_modified=False
		)
		repointed.append(row.name)
	return repointed


def _pool_has_openai_subscription() -> bool:
	return bool(_openai_subscription_pool_rows())


def _openai_subscription_pool_rows() -> list:
	settings = frappe.get_single("Jarvis Settings")
	rows = []
	for row in settings.get("models") or []:
		if not row.enabled or (row.credential_type or "") != "subscription":
			continue
		try:
			accounts = json.loads(row.get_password("subscription_accounts", raise_exception=False) or "[]")
		except (ValueError, TypeError):
			continue
		if any((a.get("upstream") or "").lower() == "openai" for a in accounts if isinstance(a, dict)):
			rows.append(row)
	return rows
