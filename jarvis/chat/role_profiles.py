"""Role -> agent-profile fixture and resolver.

Spec: ``docs/superpowers/specs/2026-08-16-role-profile-agents-design.md``.

Two independent axes, both curated data (spec §5), never runtime discovery:

* **Tool tier**: the jarvis-plane role decides ``full`` (today's 94 tools)
  vs ``standard`` (68 tools; ``STANDARD_DROP_TOOLS`` is the 26-tool drop
  list, spec §3).
* **Skill set**: ERPNext roles decide which of the 6 named skill sets
  (``SKILL_SETS``), plus the always-on ``SHARED_CORE_SKILLS``, a user's
  profile includes.

:func:`resolve_profile` is the per-user resolver other modules call to decide
one user's profile; it never raises: any failure anywhere falls back to the
main agent (spec §3 fallback rule 3), so a bad role lookup can never leave a
user with a broken or over-trimmed agent. The mapping itself is fixed data
reviewed with the app, not something resolved against a live agent
container. :func:`sync_role_profiles` is the tenant-wide push boundary: it
computes :func:`needed_profiles` and reconciles it to admin, and is likewise
built to never raise into a caller.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe

_SETTINGS = "Jarvis Settings"

# --- Axis 1: tool tier -----------------------------------------------------

FULL_TIER_ROLES: frozenset[str] = frozenset({"Jarvis Admin", "System Manager"})

# Verified drop list (26 tools, spec §3): app-learning tools (system-initiated
# learning runs only, which always run `full`), session/infra tools (zero
# references in live-tenant transcripts), file-editing tools (skills only
# need `exec` + `read`), cron/browser (Frappe-side scheduling covers cron;
# persona forbids browser outright), web (no web feature in v1), and the
# agent's builtin skill-authoring tool (jarvis has its own path via
# jarvis__create_custom_skill). Copied verbatim from spec §3.
STANDARD_DROP_TOOLS: frozenset[str] = frozenset(
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
SHARED_CORE_SKILLS: frozenset[str] = frozenset(
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
	# One frappe.get_roles call per user (via resolve_profile): N+1, but N is
	# a tenant's enabled-user headcount, not a global scan, so this is fine.
	for user in enabled_users:
		choice = resolve_profile(user)
		if choice.agent_id is None or choice.agent_id in profiles:
			continue
		profiles[choice.agent_id] = {
			"slug": choice.agent_id,
			"skills": list(choice.skills or ()),
			"tools_allow": list(tools_allow),
		}
	# Sorted by slug: frappe.get_all("User", ...) above carries no order_by, so
	# it follows Frappe's default `modified desc` and reorders on any User
	# save. Without a deterministic output order here, sync_role_profiles's
	# snapshot comparison (a plain `==` against the last-pushed list) would
	# read an unchanged profile SET as "changed" on ordering alone, forcing a
	# spurious re-push.
	return sorted(profiles.values(), key=lambda p: p["slug"])


# --- Push to admin ---------------------------------------------------------


def sync_role_profiles(force: bool = False) -> dict:
	"""Compute :func:`needed_profiles` and push it to admin only when the set
	actually changed since the last push (or ``force=True``).

	This is the fallback-discipline boundary for the whole sync (unlike
	``needed_profiles`` itself, which is deliberately left unwrapped so a real
	``frappe.get_all`` failure is visible rather than silently read as "no
	profiles needed"). Everything downstream of that computation - the
	snapshot comparison, the admin push, the settings stamp - is wrapped in
	one try/except: a push failure (admin down, rejected, a bug in this
	function) must never raise into a caller such as a ``User.on_update`` save
	or the daily scheduler tick. Logged via ``frappe.log_error`` the way other
	``admin_client`` callers in this app do.

	Returns ``{"pushed": bool, "profiles": [...]}``. ``profiles`` is always
	the freshly computed desired state (even on failure, as long as
	``needed_profiles`` itself succeeded), so a caller can inspect what SHOULD
	be live without having to re-derive it.
	"""
	profiles: list[dict] = []
	try:
		profiles = needed_profiles()

		last_raw = frappe.db.get_single_value(_SETTINGS, "role_profiles_pushed", cache=False)
		last = frappe.parse_json(last_raw) if last_raw else None
		if not force and last == profiles:
			return {"pushed": False, "profiles": profiles}

		from jarvis import admin_client

		admin_client.post_push_role_profiles(role_profiles=profiles)

		frappe.db.set_value(
			_SETTINGS,
			_SETTINGS,
			{
				"role_profiles_pushed": frappe.as_json(profiles),
				"role_profiles_pushed_at": frappe.utils.now(),
			},
		)
		return {"pushed": True, "profiles": profiles}
	except Exception:
		frappe.log_error(title="Jarvis: role-profiles push failed", message=frappe.get_traceback())
		return {"pushed": False, "profiles": profiles}


def enqueue_sync(after_commit: bool = True) -> bool:
	"""Debounced queue of a role-profiles resync.

	``after_commit`` defaults True: this is called from the ``User.on_update``
	doc_event, and the enqueued job opens its own DB connection, so it must
	not run before the role change that triggered it has actually committed -
	the same reasoning as ``jarvis.chat.wiki_mirror.enqueue_sync``.

	Suppressed under tests unless ``frappe.flags.jarvis_test_role_profiles_enqueue``
	is set, also mirroring ``wiki_mirror``: ``frappe.enqueue`` runs its job
	INLINE under ``frappe.flags.in_test``, so an un-suppressed enqueue here
	would fire an unmocked admin push on every fixture user insert/update in
	the test suite.

	Swallows and logs enqueue failures (e.g. Redis down): this is reached from
	a doc_event, which must never fail a save.

	Returns whether a job was really queued.
	"""
	if frappe.flags.in_test and not frappe.flags.jarvis_test_role_profiles_enqueue:
		return False
	try:
		frappe.enqueue(
			"jarvis.chat.role_profiles.sync_role_profiles",
			queue="short",
			job_id="role-profiles-sync",
			deduplicate=True,
			enqueue_after_commit=bool(after_commit),
		)
		return True
	except Exception:
		frappe.log_error(title="Jarvis: role-profiles enqueue failed", message=frappe.get_traceback())
		return False


def on_user_update(doc, method: str | None = None) -> None:
	"""doc_events hook (``User.on_update``): a role change may change which
	role-based profile a user needs, so debounce-enqueue a resync. Thin and
	cheap by design - ``enqueue_sync`` already swallows and logs its own
	errors, so this never raises into the save path."""
	enqueue_sync()
