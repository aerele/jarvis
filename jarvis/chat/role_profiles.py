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

import re
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

		role_to_set = get_role_to_set()
		matched = sorted({role_to_set[role] for role in roles if role in role_to_set})
		if not matched:
			# No mapped ERPNext role: conservatively keep full tier / all
			# skills in v1 rather than guess-trim (spec §3 fallback rule 2).
			return _MAIN_PROFILE

		skill_sets = get_skill_sets()
		shared_core = get_shared_core()
		skills = tuple(sorted(shared_core.union(*(skill_sets[key] for key in matched))))
		agent_id = "role-" + "+".join(matched)
		allow = get_tools_allow()
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

	tools_allow = get_tools_allow()
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

	This is the fallback-discipline boundary for the whole sync. Unlike
	``needed_profiles`` itself (which carries no internal guard of its own -
	a real ``frappe.get_all`` failure there is meant to be visible to a
	caller that invokes it directly, rather than silently read as "no
	profiles needed"), this function DOES call ``needed_profiles`` inside its
	own try/except below, alongside the snapshot comparison, the admin push,
	and the settings stamp: a failure at ANY of those steps (compute
	included) must never raise into a caller such as a ``User.on_update``
	save or the daily scheduler tick. Logged via ``frappe.log_error`` the way
	other ``admin_client`` callers in this app do, with the title naming
	which phase failed (``compute`` vs ``push``) so an Error Log entry
	doesn't need a traceback read to tell a broken role→set mapping apart
	from an unreachable admin.

	Returns ``{"pushed": bool, "profiles": [...]}``. ``profiles`` is always
	the freshly computed desired state (even on failure, as long as
	``needed_profiles`` itself succeeded), so a caller can inspect what SHOULD
	be live without having to re-derive it.
	"""
	profiles: list[dict] = []
	phase = "compute"
	try:
		profiles = needed_profiles()
		phase = "push"

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
		frappe.log_error(title=f"Jarvis: role-profiles {phase} failed", message=frappe.get_traceback())
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


# --- Admin-owned config pull (task BJ1) -------------------------------------
#
# The fixture module constants above (SKILL_SETS, ROLE_TO_SET, SHARED_CORE_SKILLS,
# _STANDARD_TOOLS_ALLOW) remain the code-side fallback forever - fallback
# discipline is absolute (global constraints): any missing/invalid/partial
# cached config falls all the way back to the fixture, never a partial
# application. This section only pulls, validates, caches, and mirrors the
# admin-owned override; BJ2 is what actually routes fixture reads through it.

# Wire-contract charset rules (frozen, mirrored from the admin editor's own
# validation - see docs/superpowers/sdd .../global-constraints.md): a config
# that violates any of these is rejected outright, never partially applied.
_CONFIG_SKILL_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_CONFIG_SET_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_CONFIG_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_CONFIG_JARVIS_TOOL_RE = re.compile(r"^jarvis__[A-Za-z0-9_]+$")
_CONFIG_TOOL_NAME_MAX_LEN = 200

# frappe.local attribute BJ2's per-request `_active_config()` memo lives on.
# Documented here (not in role_profiles' BJ2 half, which doesn't exist yet)
# because this task defines the memo's home and the invalidation hook that
# clears it; BJ2 reads/writes the same attribute name.
_CONFIG_MEMO_ATTR = "jarvis_role_profile_config_memo"


def _invalidate_config_cache() -> None:
	"""Clear BJ2's per-request role-profile-config memo (kept on
	``frappe.local`` at :data:`_CONFIG_MEMO_ATTR`) so a same-request config
	update is never served stale to the code that reads it afterwards.

	Called by :func:`sync_role_profile_config` immediately BEFORE a
	version-changing ``sync_role_profiles(force=True)``, so that forced push
	recomputes :func:`needed_profiles` from the freshly-stored config rather
	than whatever BJ2's accessor may have already cached earlier in this same
	request/job.

	Defined here (BJ1) rather than in BJ2 because BJ1 lands first and must not
	block on BJ2's accessor layer landing later: this is a no-op-safe stub
	that only clears the attribute if present. BJ2 is free to memoize under
	this same attribute name; nothing here assumes BJ2 exists yet.
	"""
	try:
		if hasattr(frappe.local, _CONFIG_MEMO_ATTR):
			delattr(frappe.local, _CONFIG_MEMO_ATTR)
	except Exception:
		pass


def _valid_skill_slug(value) -> bool:
	return isinstance(value, str) and bool(_CONFIG_SKILL_SLUG_RE.fullmatch(value))


def _valid_set_key(value) -> bool:
	return isinstance(value, str) and bool(_CONFIG_SET_KEY_RE.fullmatch(value))


def _valid_tool_name(value) -> bool:
	if not isinstance(value, str) or not value or len(value) > _CONFIG_TOOL_NAME_MAX_LEN:
		return False
	return bool(_CONFIG_TOOL_NAME_RE.fullmatch(value) or _CONFIG_JARVIS_TOOL_RE.fullmatch(value))


def _validate_role_profile_config(data) -> bool:
	"""Defensive shape + charset validation of the admin-pulled config payload
	(wire contract: ``{version, enabled, sets, shared_core, mappings,
	tool_tiers}``). Returns False on ANY structural or charset problem - the
	caller then takes the failure path and never applies a partial config.
	Never raises: any unexpected shape (wrong type where a dict/list was
	assumed) is caught and treated as invalid rather than crashing the sync.
	"""
	try:
		if not isinstance(data, dict):
			return False
		version = data.get("version")
		if not isinstance(version, str) or not version:
			return False
		if not isinstance(data.get("enabled"), bool):
			return False

		sets = data.get("sets")
		if not isinstance(sets, dict):
			return False
		for set_key, skills in sets.items():
			# "shared-core" is a distinct top-level key (`shared_core`), never a
			# mappable set - a config that puts it in `sets` too is malformed.
			if not _valid_set_key(set_key) or set_key == "shared-core":
				return False
			if not isinstance(skills, list) or not all(_valid_skill_slug(s) for s in skills):
				return False

		shared_core = data.get("shared_core")
		if not isinstance(shared_core, list) or not all(_valid_skill_slug(s) for s in shared_core):
			return False

		mappings = data.get("mappings")
		if not isinstance(mappings, dict):
			return False
		for role_name, set_key in mappings.items():
			if not isinstance(role_name, str) or not role_name or not _valid_set_key(set_key):
				return False

		tool_tiers = data.get("tool_tiers")
		if not isinstance(tool_tiers, dict):
			return False
		standard = tool_tiers.get("standard")
		if not isinstance(standard, dict):
			return False
		allow = standard.get("allow")
		core = standard.get("core")
		if not isinstance(allow, list) or not all(_valid_tool_name(t) for t in allow):
			return False
		if not isinstance(core, list) or not all(_valid_tool_name(t) for t in core):
			return False
		# core-flagged tools are a subset of the allow list by construction
		# (global constraints: core rows can't be un-flagged/deleted); a
		# payload violating that is malformed, not a partial-truth to accept.
		if not set(core).issubset(set(allow)):
			return False

		return True
	except Exception:
		return False


# --- Config-override accessor layer (task BJ2) ------------------------------
#
# Every fixture read below routes through these accessors: cached-config
# value when a validated config is active for this request, else the module
# fixture constant. Fallback is all-or-nothing (global constraints) - a
# config that is charset-valid but structurally broken (e.g. a dangling
# mapping set_key) is rejected here just as hard as one that fails BJ1's
# type/charset check.


def _mappings_reference_known_sets(data: dict) -> bool:
	"""Referential integrity beyond BJ1's charset/shape check
	(:func:`_validate_role_profile_config`): a config whose ``mappings`` point
	at a ``set_key`` absent from ``sets`` is charset-valid but broken - left
	unchecked, ``resolve_profile`` would KeyError on the dangling reference.
	Rejected here so the caller takes the whole-config fallback path, never a
	partial application. Never raises: any unexpected shape is treated as
	invalid rather than crashing the accessor.
	"""
	try:
		known_sets = set(data["sets"].keys())
		return all(set_key in known_sets for set_key in data["mappings"].values())
	except Exception:
		return False


def _active_config() -> dict | None:
	"""The validated, cached admin config for this request, or ``None`` when
	no config is cached or it fails validation. Memoized on ``frappe.local``
	at :data:`_CONFIG_MEMO_ATTR`, keyed on the current
	``role_profiles_config_version`` so a version change mid-request - whether
	via an explicit :func:`_invalidate_config_cache` call or a same-request DB
	write this function simply notices on its next call - is never served
	stale.

	Never raises: any DB, JSON, or validation problem here is a fixture-
	fallback signal to the accessors below, not an exception a caller has to
	handle. This is what lets :func:`resolve_profile`'s try/except stay
	untouched by BJ2 - the accessors it calls can't raise.
	"""
	try:
		version = frappe.db.get_single_value(_SETTINGS, "role_profiles_config_version", cache=False)
		if hasattr(frappe.local, _CONFIG_MEMO_ATTR):
			memo_version, memo_config = getattr(frappe.local, _CONFIG_MEMO_ATTR)
			if memo_version == version:
				return memo_config

		config = None
		raw = frappe.db.get_single_value(_SETTINGS, "role_profiles_config", cache=False)
		if raw:
			data = frappe.parse_json(raw)
			if _validate_role_profile_config(data) and _mappings_reference_known_sets(data):
				config = data

		setattr(frappe.local, _CONFIG_MEMO_ATTR, (version, config))
		return config
	except Exception:
		return None


def get_skill_sets() -> dict[str, frozenset[str]]:
	"""Set-key -> skill-slug membership: the cached config's ``sets`` when a
	config is active, else the module fixture (:data:`SKILL_SETS`)."""
	config = _active_config()
	if config is None:
		return SKILL_SETS
	return {set_key: frozenset(skills) for set_key, skills in config["sets"].items()}


def get_role_to_set() -> dict[str, str]:
	"""ERPNext role name -> set key: the cached config's ``mappings`` when a
	config is active, else the module fixture (:data:`ROLE_TO_SET`)."""
	config = _active_config()
	if config is None:
		return ROLE_TO_SET
	return dict(config["mappings"])


def get_shared_core() -> frozenset[str]:
	"""Always-on skill slugs: the cached config's ``shared_core`` when a
	config is active, else the module fixture (:data:`SHARED_CORE_SKILLS`)."""
	config = _active_config()
	if config is None:
		return SHARED_CORE_SKILLS
	return frozenset(config["shared_core"])


def get_tools_allow() -> list[str]:
	"""The ``standard``-tier tool allow list: the cached config's
	``tool_tiers.standard.allow`` when a config is active, else the module
	fixture (:func:`standard_tools_allow`)."""
	config = _active_config()
	if config is None:
		return standard_tools_allow()
	return list(config["tool_tiers"]["standard"]["allow"])


def sync_role_profile_config() -> dict:
	"""Pull the admin-owned role-profile config, cache it, mirror the
	admin-decided ``enable_role_profiles`` flag, and re-push role profiles
	when the config's identity (``version``) changed since the last pull.

	Never raises: this runs from the daily scheduler tick (and can be called
	ad hoc via ``bench execute``), so every failure mode degrades rather than
	propagates - logged once via ``frappe.log_error`` with a phase-tagged
	title (mirrors :func:`sync_role_profiles`'s own convention) so an Error
	Log entry doesn't need a traceback read to tell "admin unreachable" apart
	from "admin sent a malformed payload".

	Last-known-good rule (plan-binding): a transient failure (pull OR
	validation) with an EXISTING cached config leaves BOTH the cache and the
	``enable_role_profiles`` mirror untouched - a blip in admin reachability
	must never silently disable a previously-synced tenant. A failure with NO
	cache yet mirrors the flag OFF - nothing has ever been confirmed, so
	there is no last-known-good state to preserve. This also covers an old
	bench against an admin that predates this endpoint (method-not-found):
	compat rule says that must leave the cache absent and the mirror OFF.

	On success (version changed): invalidates BJ2's per-request config memo
	BEFORE forcing a re-push, so the push recomputes :func:`needed_profiles`
	from the NEW config rather than a value memoized earlier this request.

	Returns ``{"synced": bool}``.
	"""
	phase = "pull"
	try:
		from jarvis import admin_client

		response = admin_client.get_role_profile_config()
		phase = "validate"
		data = response.get("data") if isinstance(response, dict) else None
		if not _validate_role_profile_config(data):
			raise ValueError("invalid or malformed role-profile config payload")

		phase = "store"
		version = data["version"]
		previous_version = frappe.db.get_single_value(_SETTINGS, "role_profiles_config_version", cache=False)

		frappe.db.set_value(
			_SETTINGS,
			_SETTINGS,
			{
				"role_profiles_config": frappe.as_json(data),
				"role_profiles_config_version": version,
				"role_profiles_config_synced_at": frappe.utils.now(),
			},
		)
		frappe.db.set_single_value(_SETTINGS, "enable_role_profiles", 1 if data["enabled"] else 0)

		if version != previous_version:
			phase = "push"
			_invalidate_config_cache()
			sync_role_profiles(force=True)

		return {"synced": True}
	except Exception:
		try:
			has_cache = bool(frappe.db.get_single_value(_SETTINGS, "role_profiles_config", cache=False))
		except Exception:
			has_cache = False
		if not has_cache:
			try:
				frappe.db.set_single_value(_SETTINGS, "enable_role_profiles", 0)
			except Exception:
				pass
		frappe.log_error(
			title=f"Jarvis: role-profile-config {phase} failed",
			message=frappe.get_traceback(),
		)
		return {"synced": False}
