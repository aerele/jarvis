"""Role -> agent-profile fixture and resolver.

Spec: ``docs/superpowers/specs/2026-08-16-role-profile-agents-design.md``.

Two independent axes, both curated data (spec §5), never runtime discovery:

* **Tool tier**: the jarvis-plane role decides ``full`` (today's 94 tools)
  vs ``standard`` (68 tools; ``STANDARD_DROP_TOOLS`` is the 26-tool drop
  list, spec §3).
* **Skill set**: ERPNext roles decide which of the 6 named skill sets
  (``SKILL_SETS``), plus the always-on ``SHARED_CORE_SKILLS``, a user's
  profile includes.

:func:`resolve_profile` is the only entry point other modules should call.
It never raises: any failure anywhere falls back to the main agent (spec §3
fallback rule 3), so a bad role lookup can never leave a user with a broken
or over-trimmed agent. The mapping itself is fixed data reviewed with the
app, not something resolved against a live agent container.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe

# --- Axis 1: tool tier -----------------------------------------------------

FULL_TIER_ROLES = frozenset({"Jarvis Admin", "System Manager"})

# Verified drop list (26 tools, spec §3): app-learning tools (system-initiated
# learning runs only, which always run `full`), session/infra tools (zero
# references in live-tenant transcripts), file-editing tools (skills only
# need `exec` + `read`), cron/browser (Frappe-side scheduling covers cron;
# persona forbids browser outright), web (no web feature in v1), and the
# agent's builtin skill-authoring tool (jarvis has its own path via
# jarvis__create_custom_skill). Copied verbatim from spec §3.
STANDARD_DROP_TOOLS = frozenset(
	{
		"cron",
		"browser",
		"jarvis__record_agent_run",
		"jarvis__record_app_wiki",
		"jarvis__read_app_source",
		"jarvis__finish_app_learning_run",
		"skill_workshop",
		"sessions_spawn",
		"sessions_send",
		"sessions_list",
		"sessions_history",
		"sessions_yield",
		"subagents",
		"nodes",
		"gateway",
		"process",
		"tts",
		"edit",
		"write",
		"apply_patch",
		"file_write",
		"file_fetch",
		"dir_fetch",
		"dir_list",
		"web_fetch",
		"web_search",
	}
)

# The full 94-tool universe minus STANDARD_DROP_TOOLS, hardcoded explicit and
# sorted (spec §2 evidence capture: ~/.claude/jobs/bce488ac/tmp/postfix-cap.jsonl).
# An allow list must be explicit here: deriving it at runtime from a live
# agent container is not possible bench-side.
_STANDARD_TOOLS_ALLOW = [
	"agents_list",
	"canvas",
	"create_goal",
	"exec",
	"get_goal",
	"image",
	"jarvis__add_comment",
	"jarvis__add_tag",
	"jarvis__amend_doc",
	"jarvis__apply_workflow_action",
	"jarvis__assign_to",
	"jarvis__attach_to_doc",
	"jarvis__cancel_doc",
	"jarvis__create_custom_skill",
	"jarvis__create_dashboard",
	"jarvis__create_dashboard_chart",
	"jarvis__create_doc",
	"jarvis__delete_doc",
	"jarvis__describe_customizations",
	"jarvis__download_pdf",
	"jarvis__download_vcard",
	"jarvis__export_document",
	"jarvis__export_excel",
	"jarvis__export_query",
	"jarvis__find_skills",
	"jarvis__follow_document",
	"jarvis__get_creation_context",
	"jarvis__get_doc",
	"jarvis__get_file_pages",
	"jarvis__get_linked_docs",
	"jarvis__get_list",
	"jarvis__get_naming_series_preview",
	"jarvis__get_report_filters",
	"jarvis__get_schema",
	"jarvis__get_skill",
	"jarvis__get_submitted_linked_docs",
	"jarvis__get_workflow_transitions",
	"jarvis__list_app_modules",
	"jarvis__preview_doc",
	"jarvis__preview_import",
	"jarvis__query",
	"jarvis__read_file",
	"jarvis__read_wiki",
	"jarvis__remove_tag",
	"jarvis__report_pdf",
	"jarvis__resolve_links",
	"jarvis__run_import",
	"jarvis__run_method",
	"jarvis__run_report",
	"jarvis__save_agent_dashboard",
	"jarvis__save_dashboard",
	"jarvis__send_email",
	"jarvis__share_doc",
	"jarvis__submit_doc",
	"jarvis__summarize_dataset",
	"jarvis__unassign_from",
	"jarvis__unfollow_document",
	"jarvis__unshare_doc",
	"jarvis__update_comment",
	"jarvis__update_doc",
	"jarvis__update_wiki",
	"memory_get",
	"memory_search",
	"message",
	"pdf",
	"read",
	"session_status",
	"update_goal",
]


def standard_tools_allow() -> list[str]:
	"""The 68-tool allow list for the ``standard`` tier (spec §3)."""
	return list(_STANDARD_TOOLS_ALLOW)


# --- Axis 2: skill set -------------------------------------------------------

# Everyone gets these regardless of ERPNext role (spec §3): all frappe-*,
# all jarvis-*, and the domain-neutral erpnext/utility skills that no single
# role set claims, including erpnext-bulk-transaction, which spans domains
# and so belongs to no one set.
SHARED_CORE_SKILLS = frozenset(
	{
		"frappe-automation",
		"frappe-contacts",
		"frappe-core",
		"frappe-custom",
		"frappe-desk",
		"frappe-email",
		"frappe-geo",
		"frappe-integrations",
		"frappe-printing",
		"frappe-website",
		"frappe-workflow",
		"jarvis-app-analysis",
		"jarvis-chat-blocks",
		"jarvis-dashboards",
		"jarvis-drafting",
		"jarvis-rich-pdf",
		"jarvis-site-customizations",
		"jarvis-skill-writer",
		"jarvis-triggers",
		"jarvis-wiki",
		"erpnext-setup",
		"erpnext-utilities",
		"erpnext-regional",
		"erpnext-communication",
		"erpnext-integrations",
		"erpnext-bulk-transaction",
		"ocr-data-entry",
		"macro-merge",
	}
)

# Skill-set additions on top of SHARED_CORE_SKILLS, spec §3 table.
SKILL_SETS: dict[str, frozenset[str]] = {
	"accounts": frozenset({"erpnext-accounts", "erpnext-assets", "india-compliance"}),
	"sales": frozenset({"erpnext-selling", "erpnext-crm", "erpnext-support", "erpnext-telephony"}),
	"purchase": frozenset({"erpnext-buying", "erpnext-subcontracting", "erpnext-edi"}),
	"stock-mfg": frozenset(
		{
			"erpnext-stock",
			"erpnext-manufacturing",
			"erpnext-maintenance",
			"erpnext-quality-management",
		}
	),
	"hr": frozenset({"hrms-hr", "hrms-payroll"}),
	"projects": frozenset({"erpnext-projects"}),
}

# Every triggering ERPNext role name, spec §3 table, mapped to its set key.
ROLE_TO_SET: dict[str, str] = {
	"Accounts User": "accounts",
	"Accounts Manager": "accounts",
	"Auditor": "accounts",
	"Sales User": "sales",
	"Sales Manager": "sales",
	"Purchase User": "purchase",
	"Purchase Manager": "purchase",
	"Stock User": "stock-mfg",
	"Stock Manager": "stock-mfg",
	"Manufacturing User": "stock-mfg",
	"Manufacturing Manager": "stock-mfg",
	"Maintenance User": "stock-mfg",
	"Maintenance Manager": "stock-mfg",
	"Quality Manager": "stock-mfg",
	"Delivery Manager": "stock-mfg",
	"Delivery User": "stock-mfg",
	"Fulfillment User": "stock-mfg",
	"Item Manager": "stock-mfg",
	"HR User": "hr",
	"HR Manager": "hr",
	"Employee": "hr",
	"Projects User": "projects",
	"Projects Manager": "projects",
}


# --- Resolver -----------------------------------------------------------


@dataclass(frozen=True)
class ProfileChoice:
	agent_id: str | None
	tier: str
	set_keys: tuple[str, ...]
	skills: tuple[str, ...] | None
	n_tools: int | None


# agent_id=None means "use main": full tier, no trimming. Never "main" or
# an "agent-" prefixed slug literal: those are reserved by the fleet side.
_MAIN_PROFILE = ProfileChoice(agent_id=None, tier="full", set_keys=(), skills=None, n_tools=None)


def resolve_profile(user: str) -> ProfileChoice:
	"""Resolve the chat-agent profile for ``user`` (spec §3 / §5).

	Never raises: any exception anywhere in resolution falls back to the
	main agent (spec §3 fallback rule 3). There is no partial trim on error.
	"""
	try:
		if not user:
			# frappe.get_roles(None) resolves to the *session* user, not "no
			# user" - that's a different, deterministic case here, not the
			# fallback path.
			return _MAIN_PROFILE

		roles = set(frappe.get_roles(user))

		if roles & FULL_TIER_ROLES:
			return _MAIN_PROFILE

		matched = sorted({ROLE_TO_SET[role] for role in roles if role in ROLE_TO_SET})
		if not matched:
			# No mapped ERPNext role: conservatively keep full tier / all
			# skills in v1 rather than guess-trim (spec §3 fallback rule 2).
			return _MAIN_PROFILE

		skills = tuple(sorted(SHARED_CORE_SKILLS.union(*(SKILL_SETS[key] for key in matched))))
		agent_id = "role-" + "+".join(matched)
		allow = standard_tools_allow()
		return ProfileChoice(
			agent_id=agent_id,
			tier="standard",
			set_keys=tuple(matched),
			skills=skills,
			n_tools=len(allow),
		)
	except Exception:
		return _MAIN_PROFILE


def needed_profiles() -> list[dict]:
	"""Distinct role-based profiles across every enabled ``Jarvis User`` (spec §7).

	Returns the render-ready shape fleet consumes for ``agents.list[]``.
	Skills here are persona skills only; custom/learned skill slugs are
	unioned in by fleet at apply time for every profile, main included
	(spec §7), not here.
	"""
	user_names = frappe.get_all(
		"Has Role",
		filters={"role": "Jarvis User", "parenttype": "User"},
		pluck="parent",
	)
	enabled_users = frappe.get_all(
		"User",
		filters={"name": ["in", user_names], "enabled": 1},
		pluck="name",
	)

	tools_allow = standard_tools_allow()
	profiles: dict[str, dict] = {}
	for user in enabled_users:
		choice = resolve_profile(user)
		if choice.agent_id is None or choice.agent_id in profiles:
			continue
		profiles[choice.agent_id] = {
			"slug": choice.agent_id,
			"skills": list(choice.skills or ()),
			"tools_allow": list(tools_allow),
		}
	return list(profiles.values())
