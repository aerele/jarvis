"""Server-owned registry of every active Jarvis document-collection surface.

Plan 08 §10 Phase 0. The universal-filter mandate is only enforceable if the
set of "document lists" is *data*, not folklore: a page that ships without a
``view_key`` must fail a test, and a collection that is deliberately NOT a
document list must say so, in writing, with a reason. This module is that
registry; :mod:`jarvis.tests.test_list_registry` is the audit that reads it.

What lives here
---------------
* the 8 shared-``ListPage`` consumers,
* every bespoke collection surface (cards / rails / feeds / split panes) named
  in plan 08 §3.3,
* the surfaces the SPA's Settings area renders as tables/feeds,
* explicit EXCLUSIONS (navigation lists, per-document detail feeds, dormant
  UIs) each carrying a ``reason``.

``view_key`` — never a client-supplied table or doctype — is what
:func:`jarvis.chat.list_filters.get_list_filter_schema` and
:func:`jarvis.chat.list_filters.compile_filters_v2` resolve. A caller can only
ever name a key that already exists in this file.

Registering a surface does NOT grant it filters: ``status`` says whether the
endpoint speaks the ``filters_v2`` contract yet. Skills and Macros are the
contract's pilots; each further surface flips to ``MIGRATED`` as it adopts the
contract, and the rest stay ``PENDING`` on their curated filters until their wave.
The exact migrated set and its counts are the registry's own data — asserted by
:mod:`jarvis.tests.test_list_registry`, never restated as a number in prose here
(which is how the stale "exactly two" crept in and misreported the split).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
# A list of rows of ONE local DocType (the fixed visibility predicate may scope
# it, e.g. owner-only, but every row is a document of ``root_doctype``).
DOCUMENT_LIST = "document_list"
# A list whose rows are computed over a root document plus joins/derived state
# (File Box), or served by a remote authority (Support). Needs a declared
# projection schema (plan §4.4) before it can offer metadata filters.
PROJECTION = "projection"
# Not a document list view: navigation, a single-document detail feed, or a
# configuration editor. Carries a ``reason``.
EXCLUDED_NOT_A_LIST = "excluded_not_a_list"
# A real document list whose UI is not reachable today. Reactivation must flip
# it to DOCUMENT_LIST *and* adopt the contract (plan §7).
EXCLUDED_DORMANT = "excluded_dormant"

CLASSIFICATIONS = frozenset({DOCUMENT_LIST, PROJECTION, EXCLUDED_NOT_A_LIST, EXCLUDED_DORMANT})
#: Classifications that must carry a ``reason``.
EXCLUDED_CLASSIFICATIONS = frozenset({EXCLUDED_NOT_A_LIST, EXCLUDED_DORMANT})

#: Speaks ``filters_v2`` today.
MIGRATED = "migrated"
#: Registered, still on its curated filter whitelist.
PENDING = "pending"
#: Not applicable (excluded surfaces).
NOT_APPLICABLE = "n/a"

STATUSES = frozenset({MIGRATED, PENDING, NOT_APPLICABLE})


@dataclass(frozen=True)
class ListView:
	"""One registered surface.

	``curated_filters`` maps the endpoint's CURRENT filter argument/key names to
	the ``root_doctype`` fieldname each one actually filters, or ``None`` when
	the key is a view-level pseudo control with no single backing field (e.g.
	Skills' ``scope``, which means "mine | shared with me" — NOT the doctype's
	``scope`` field, a genuinely confusable collision). The Phase-0 audit needs
	that mapping to compare curated width against the metadata catalog without
	guessing.

	``excluded_fields`` is the view's own withholding list, and it is a SECURITY
	control, not a taste one. Permlevel (C08-1) stops a field the *DocType* hides;
	it cannot stop a field the *endpoint* hides. Triggers is the live example: the
	rows it returns redact ``condition`` / ``script_body`` / ``llm_instruction``
	for non-managers (``triggers_api._trigger_detail``), so offering those as
	filters would rebuild the redacted logic one LIKE at a time. Anything a view's
	projection withholds from its rows MUST appear here before that view is
	flipped to ``MIGRATED``. Entries are plain fieldnames of ``root_doctype``, or
	``"Child DocType.fieldname"`` to withhold one child field.
	"""

	view_key: str
	label: str
	classification: str
	endpoints: tuple[str, ...]
	root_doctype: str | None = None
	#: For PROJECTION rows: the local root document the projection starts from.
	projection_root: str | None = None
	#: For PROJECTION rows served elsewhere: who owns the schema.
	projection_authority: str = ""
	clients: tuple[str, ...] = ("spa",)
	surface: str = ""
	wave: int | None = None
	status: str = PENDING
	curated_filters: dict[str, str | None] = field(default_factory=dict)
	excluded_fields: tuple[str, ...] = ()
	#: Distinguishes two views served by one endpoint (learning queue/decided).
	variant: str = ""
	reason: str = ""
	notes: str = ""

	@property
	def is_document_list(self) -> bool:
		return self.classification == DOCUMENT_LIST

	@property
	def is_excluded(self) -> bool:
		return self.classification in EXCLUDED_CLASSIFICATIONS


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
_VIEWS: tuple[ListView, ...] = (
	# ---------------------------------------------------------------- wave 1 --
	# The 8 shared-ListPage consumers.
	ListView(
		view_key="skills",
		label="Skills",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Custom Skill",
		endpoints=("jarvis.chat.custom_skills_api.list_custom_skills_page",),
		surface="frontend/src/pages/skills/SkillsList.vue",
		wave=1,
		status=MIGRATED,
		curated_filters={"scope": None, "enabled": "enabled", "user_invocable": "user_invocable"},
		notes="filters_v2 pilot. `scope` is an ownership pseudo-filter (mine|shared), NOT the doctype's scope field.",
	),
	ListView(
		view_key="macros",
		label="Macros",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Macro",
		endpoints=("jarvis.chat.macros_api.list_macros_page",),
		surface="frontend/src/pages/macros/MacrosList.vue",
		wave=1,
		status=MIGRATED,
		curated_filters={
			"enabled": "enabled",
			"schedule_enabled": "schedule_enabled",
			"schedule_frequency": "schedule_frequency",
		},
		notes="filters_v2 pilot. Child table Jarvis Macro Step is the EXISTS-compilation case.",
	),
	ListView(
		view_key="file_box",
		label="File Box",
		classification=PROJECTION,
		projection_root="Jarvis Conversation",
		endpoints=("jarvis.chat.filebox.list_inbound_page",),
		clients=("spa", "pwa"),
		surface="frontend/src/pages/files/FilesList.vue",
		wave=1,
		curated_filters={"status": None, "from_date": None, "to_date": None},
		notes="Rows join approvals/messages/File and add a derived status; needs a declared projection schema (plan §4.4). Second client: pwa/src/api.js.",
	),
	ListView(
		view_key="saved_dashboards",
		label="Saved dashboards",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Dashboard",
		endpoints=("jarvis.chat.dashboards_api.list_dashboards_page",),
		surface="frontend/src/pages/dashboards/SavedDashboardsTab.vue",
		wave=1,
		status=MIGRATED,
		curated_filters={"scope": "scope", "dashboard_type": "dashboard_type", "owner": "owner"},
	),
	ListView(
		view_key="wiki_pages",
		label="Wiki",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Wiki Page",
		endpoints=("jarvis.chat.wiki.list_wiki_pages_page",),
		surface="frontend/src/pages/skills/WikiTab.vue",
		wave=1,
		status=MIGRATED,
		curated_filters={
			"page_type": "page_type",
			"scope_filter": None,
			"attention": None,
			"archived": "status",
		},
		notes="`scope_filter` (all|org|role|mine) and `attention` are view predicates, not single fields; `archived` is a two-valued view switch over `status` (Active vs Archived).",
	),
	ListView(
		view_key="support_tickets",
		label="Support",
		classification=PROJECTION,
		projection_authority="remote Helpdesk via jarvis_admin proxy",
		endpoints=("jarvis.support.api.list_tickets",),
		surface="frontend/src/pages/support/SupportListPage.vue",
		wave=1,
		curated_filters={},
		notes="C08-4: filtering is client-side over a capped fetch today; server-authoritative filtering needs an admin-repo contract change, so Support is its own cross-repo milestone.",
	),
	ListView(
		view_key="triggers",
		label="Triggers",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Trigger",
		endpoints=("jarvis.chat.triggers_api.list_triggers_page",),
		surface="frontend/src/pages/triggers/TriggersListPane.vue",
		wave=1,
		status=MIGRATED,
		curated_filters={
			"enabled": "enabled",
			"action_type": "action_type",
			"target_doctype": "target_doctype",
			"doc_event": "doc_event",
		},
		excluded_fields=("condition", "script_body", "llm_instruction"),
		notes=(
			"Manager/non-manager row redaction must survive migration: "
			"triggers_api._trigger_detail blanks condition/script_body/"
			"llm_instruction for non-managers, so those three are withheld from "
			"the filter catalog too — a LIKE oracle would rebuild them. Withheld "
			"unconditionally (for managers as well): a per-role exclusion needs a "
			"view-declared predicate, which is deliberately deferred."
		),
	),
	ListView(
		view_key="trigger_activity",
		label="Trigger activity",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Trigger Activity",
		endpoints=("jarvis.chat.triggers_api.list_activity_page",),
		surface="frontend/src/pages/triggers/ActivityTab.vue",
		wave=1,
		curated_filters={
			"trigger": "trigger",
			"status": "status",
			"action_type": "action_type",
			"target_doctype": "target_doctype",
			"doc_event": "doc_event",
			"from_date": "creation",
			"to_date": "creation",
		},
		notes="C08-6: the per-row target-permission scan caps the count, so totals stay `approximate: True` — a declared exception, surfaced in the UI.",
	),
	# ---------------------------------------------------------------- wave 2 --
	ListView(
		view_key="agents_catalog",
		label="Agents catalog",
		classification=PROJECTION,
		projection_root="Jarvis Agent Listing",
		endpoints=("jarvis.chat.agents_api.list_agents_page",),
		surface="frontend/src/pages/agents/AgentsList.vue",
		wave=2,
		curated_filters={"tab": None, "category": "category"},
		notes="Computed install state / install count ride on the listing; `tab` is a fixed cohort. C08-1's canonical leak target lives here (Jarvis Agent Listing.skill_bundle, permlevel 1).",
	),
	ListView(
		view_key="agent_activity",
		label="Agent activity",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Agent Activity",
		endpoints=("jarvis.chat.agents_api.list_agent_activity_page",),
		surface="frontend/src/pages/agents/AgentActivityTab.vue",
		wave=2,
		curated_filters={"agent": "agent", "action": "action"},
	),
	ListView(
		view_key="agent_runs",
		label="Agent runs",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Agent Run",
		endpoints=("jarvis.chat.agents_api.list_runs_page",),
		surface="frontend/src/pages/agents/AgentRunsBoard.vue",
		wave=2,
		curated_filters={"agent": "agent", "status": "status"},
	),
	ListView(
		view_key="agent_findings",
		label="Agent findings",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Agent Finding",
		endpoints=("jarvis.chat.agents_api.list_findings",),
		surface="frontend/src/pages/agents/AgentRunsBoard.vue",
		wave=2,
		curated_filters={"run": None, "state": "state"},
		notes="`run` means 'findings this run OBSERVED' (dedupe bumps last_seen_run), a fixed relation — not a plain equality on the run column.",
	),
	ListView(
		view_key="agent_admin_overview",
		label="Agents admin overview (System Manager)",
		classification=EXCLUDED_NOT_A_LIST,
		endpoints=("jarvis.chat.agents_api.get_agent_admin_overview",),
		surface="frontend/src/pages/agents/AgentsList.vue",
		status=NOT_APPLICABLE,
		reason=(
			"C08-4: not a list surface today. The endpoint returns a configuration "
			"editor payload — the selectable Role set plus, per listing, its "
			"allowed_roles and a rollup of every owner's installs. There is no "
			"independent row collection to page, sort or filter; installs appear as "
			"an attribute of a listing. Plan §7 wave 2's 'Agent admin installs' row "
			"anticipates a real Jarvis Agent Installation list; if that is ever built "
			"it registers as DOCUMENT_LIST before it becomes reachable."
		),
	),
	ListView(
		view_key="approvals",
		label="Approvals",
		classification=PROJECTION,
		projection_root="Jarvis Approval Request",
		endpoints=("jarvis.chat.approvals_api.list_approvals_page",),
		surface="frontend/src/pages/approvals/ApprovalsBoard.vue",
		wave=2,
		curated_filters={
			"status": "status",
			"document_type": "document_type",
			"conversation": "conversation",
		},
		notes="Computed `shared` flag + DocShare/owner visibility. Its document_type facet deliberately drops its own dimension — the C08-3 rule this module implements.",
	),
	ListView(
		view_key="macro_runs",
		label="Macro runs",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Macro Run",
		endpoints=("jarvis.chat.macros_api.list_macro_runs",),
		surface="frontend/src/pages/macros/RunsTab.vue",
		wave=2,
		curated_filters={"status": "status", "macro": "macro"},
	),
	# ---------------------------------------------------------------- wave 3 --
	ListView(
		view_key="personalise_questions",
		label="Personalise questions",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Personalise Question",
		endpoints=("jarvis.chat.personalise_api.list_questions_page",),
		surface="frontend/src/components/personalise/QuestionsView.vue",
		wave=3,
		curated_filters={"status": "status"},
	),
	ListView(
		view_key="personalise_notes",
		label="Notes",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Voice Note",
		endpoints=(
			"jarvis.chat.personalise_api.list_notes_page",
			"jarvis.chat.voice_notes_api.list_my_voice_notes_page",
		),
		clients=("spa", "pwa"),
		surface="frontend/src/components/personalise/NotesView.vue",
		wave=3,
		curated_filters={"kind": "kind", "status": "status"},
		notes="C08-4: TWO endpoints over one doctype. The SPA Notes pane calls list_notes_page; the PWA calls voice_notes_api.list_my_voice_notes_page. The compatibility window covers both.",
	),
	ListView(
		view_key="learning_queue",
		label="Learning queues (proposed / stale / approved)",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Learned Pattern",
		endpoints=("jarvis.chat.learned_api.list_learned_patterns_page",),
		surface="frontend/src/pages/skills/ReviewTab.vue",
		wave=3,
		curated_filters={
			"status": "status",
			"domain": "domain",
			"strength": "strength_band",
			"surfaced": "surfaced",
			"disposition": None,
		},
		notes="Its domain facet drops the domain filter itself (C08-3). `disposition` is a compound predicate over status + review columns, not a single field.",
	),
	ListView(
		view_key="learning_decided",
		label="Learning decided log",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Learned Pattern",
		endpoints=("jarvis.chat.learned_api.list_learned_patterns_page",),
		variant="view=decided",
		surface="frontend/src/pages/skills/ReviewTab.vue",
		wave=3,
		curated_filters={"domain": "domain", "strength": "strength_band", "disposition": None},
		notes="Same endpoint, different view predicate: status is overridden with the human-touched terminal states and `surfaced` is ignored.",
	),
	ListView(
		view_key="wiki_promotions",
		label="Wiki promotion requests",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Wiki Promotion Request",
		endpoints=("jarvis.chat.learned_api.list_promotion_requests_page",),
		surface="frontend/src/pages/skills/ReviewTab.vue",
		wave=3,
		curated_filters={"status": "status"},
	),
	ListView(
		view_key="skill_promotions",
		label="Skill promotion requests",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis Skill Promotion Request",
		endpoints=("jarvis.chat.custom_skills_api.list_skill_promotion_requests",),
		surface="frontend/src/pages/skills/ReviewTab.vue",
		wave=3,
		curated_filters={"status": "status"},
	),
	# ------------------------------------------------------- Settings area --
	ListView(
		view_key="settings_user_usage_admin",
		label="Settings → Usage (admin per-user table)",
		classification=DOCUMENT_LIST,
		root_doctype="Jarvis User Settings",
		endpoints=("jarvis.chat.user_settings_api.admin_list_user_usage",),
		surface="frontend/src/components/settings/UsageAdminPane.vue",
		wave=3,
		curated_filters={},
		notes=(
			"A real cross-user document collection (one row per Jarvis User Settings "
			"row, Jarvis-Admin gated) rendered as a table. It has NO pagination, "
			"search, sort or filter today, so migrating it is a widening, not a "
			"port: it needs the paginated envelope first. Registered PENDING rather "
			"than excluded so it cannot quietly stay filter-less."
		),
	),
	ListView(
		view_key="settings_tool_activity",
		label="Settings → Activity (tool-run feed)",
		classification=EXCLUDED_NOT_A_LIST,
		endpoints=("jarvis.chat.api.get_tool_activity",),
		surface="frontend/src/components/settings/ActivityPane.vue",
		status=NOT_APPLICABLE,
		reason=(
			"A per-conversation detail feed, not a collection: rows are computed "
			"groupings of tool calls within the ONE open conversation (turn → "
			"{tools, ms, names}) and have no document identity, no stable key and "
			"no existence outside that conversation's detail pane. It is the chat "
			"equivalent of a form's timeline, which Frappe does not filter either."
		),
	),
	# ------------------------------------------------ explicit non-lists ----
	ListView(
		view_key="chat_conversation_sidebar",
		label="Chat conversation sidebar",
		classification=EXCLUDED_NOT_A_LIST,
		endpoints=("jarvis.chat.api.list_conversations",),
		surface="frontend/src/pages/chat/ChatView.vue",
		status=NOT_APPLICABLE,
		reason=(
			"Navigation, not a document list view (plan §3.3): a fixed-length "
			"most-recent rail that exists to switch the primary surface. It has no "
			"columns, no sort control and no page. Recorded here so the "
			"navigation-vs-list boundary is auditable rather than folklore."
		),
	),
	ListView(
		view_key="app_learning_runs",
		label="App learning runs (legacy)",
		classification=EXCLUDED_DORMANT,
		root_doctype="Jarvis App Learning Run",
		endpoints=("jarvis.chat.app_learning_api.list_app_learning_runs_page",),
		status=NOT_APPLICABLE,
		reason=(
			"Plan §7: the legacy App Learning Run UI is not reachable from the "
			"shipped SPA. The endpoint still exists, so it is registered rather than "
			"omitted; any reactivation must flip this row to DOCUMENT_LIST and adopt "
			"the filters_v2 contract before the surface is made reachable."
		),
	),
)

_BY_KEY: dict[str, ListView] = {v.view_key: v for v in _VIEWS}


#: Whitelisted COLLECTION-SHAPED endpoints under ``jarvis/chat`` / ``jarvis/support``
#: that are deliberately NOT document-list views, each with its classification. The
#: new-surface audit sweeps every such callable — matched by the whole family of
#: collection name patterns, not ``list_*`` alone (S7) — and fails unless it appears
#: in a registered view's ``endpoints`` or here, so a list endpoint cannot ship
#: unclassified whatever it is named (the round-2 loophole: discovery keyed only on
#: ``list_*``/``admin_list_*``, so a ``search_*`` or ``*_feed`` collection evaded it).
NON_LIST_ENDPOINTS: dict[str, str] = {
	# Unpaginated companions of a registered paginated list. They exist for a
	# dropdown / autocomplete / first-paint and return a capped slice with no
	# filter, sort or page contract. The registered *_page endpoint is the
	# document-list view; these are its accelerators and migrate with it.
	"jarvis.chat.agents_api.list_agents": "unpaginated companion of agents_catalog (list_agents_page)",
	"jarvis.chat.agents_api.list_runs": "unpaginated companion of agent_runs (list_runs_page)",
	# The Dashboard Builder's own history MENU: owner-scoped origin_page="dashboards"
	# threads, newest first, capped at 100, no filter/sort/page contract. It is the
	# in-pane counterpart of the general chat sidebar (which deliberately excludes
	# this namespace), not a browsable document list, so there is no view to register.
	"jarvis.chat.dashboards_api.list_dashboard_conversations": (
		"Dashboard Builder history menu: a capped, owner-scoped slice of dashboard-origin "
		"threads for the pane's own dropdown, not a browsable document list"
	),
	# jarvis#1062: a type-ahead SOURCE for the admin Access editor's people picker,
	# not a document list. It returns at most 20 {name, full_name} pairs for a
	# substring, has no filter/sort/page contract, and is gated require_jarvis_admin
	# — there is no list VIEW here to register, and giving it one would imply a
	# browsable directory of the tenant's users that the product deliberately lacks.
	"jarvis.chat.agents_api.search_users": (
		"admin Access-editor people picker: a capped type-ahead over enabled users, "
		"not a browsable document list"
	),
	"jarvis.chat.approvals_api.list_approvals": "unpaginated companion of approvals (list_approvals_page)",
	"jarvis.chat.custom_skills_api.list_custom_skills": (
		"composer '/' autocomplete feed for skills; unpaginated companion of the skills view"
	),
	"jarvis.chat.filebox.list_inbound": "unpaginated companion of file_box (list_inbound_page)",
	"jarvis.chat.macros_api.list_macros": (
		"Settings macro-runs dropdown feed; unpaginated companion of the macros view"
	),
	# The filters_v2 runtime-capability probe (P1-05): returns the per-view
	# {view_key: enabled} map so a surface can pick transport at mount. It is
	# metadata ABOUT the list views, not a list of documents.
	"jarvis.chat.list_filters.list_filters_capabilities": (
		"the filters_v2 per-view capability map (P1-05) — a config probe, not a document list"
	),
	"jarvis.chat.list_filters.get_list_filter_schema": (
		"the filters_v2 field-catalog probe for ONE view — schema metadata about a "
		"list, not a list of documents (its name carries the 'list' token)"
	),
	# Search surfaces: collection-SHAPED (they return matches), but they are search
	# results over a query, not browsable, paginated, filterable document-list views.
	# Registered here so the S7 sweep forces the distinction rather than letting a
	# search endpoint pass unclassified.
	"jarvis.chat.api.search_conversations": (
		"full-text search over the caller's conversations — a ranked search-results "
		"feed for the command palette, not a filterable document-list view"
	),
	"jarvis.chat.api.search_workspace": (
		"global command-palette search across entities — a ranked hit list, not a "
		"single-DocType browsable list view"
	),
	# Single-document `*_page` operations. Collection-shaped ONLY by the `_page`
	# suffix the S7 sweep now recognises; each acts on ONE wiki page by slug and has
	# no pageable row collection of its own. Registered so the suffix widening does
	# not let a genuine `get_<thing>_page` list slip past unargued.
	"jarvis.chat.wiki.get_wiki_page": "reads ONE wiki page by slug — a document getter, not a list",
	"jarvis.chat.wiki.create_wiki_page": "creates ONE wiki page — a mutation, not a list",
	"jarvis.chat.wiki.save_wiki_page": "saves ONE wiki page — a mutation, not a list",
	"jarvis.chat.wiki.archive_wiki_page": "archives ONE wiki page by slug — a mutation, not a list",
	"jarvis.chat.wiki.delete_wiki_page": "deletes ONE wiki page by slug — a mutation, not a list",
	"jarvis.chat.wiki.restore_wiki_page": "restores ONE wiki page by slug — a mutation, not a list",
	# Per-document detail feeds and aggregates: no independent, pageable row
	# collection of their own.
	"jarvis.chat.wiki.get_wiki_graph_history": (
		"the edit-history feed of ONE wiki page — a per-document timeline, not a "
		"collection (the chat equivalent of a form's version history)"
	),
	# jarvis#1062: the live step timeline of ONE agent run, rendered inside that
	# run's own panel. The same shape as get_wiki_graph_history above — a
	# per-document timeline read whole (capped at RUN_STEPS_CAP, ordered by the
	# run's own `seq`), with no filter, sort or page contract to migrate. The
	# browsable agent surfaces are the registered agent_runs / agent_findings /
	# agent_activity views.
	"jarvis.chat.agents_api.list_run_steps": (
		"the step timeline of ONE agent run — a per-document timeline read whole, "
		"not a filterable document-list view"
	),
	"jarvis.chat.triggers_api.activity_stats": (
		"aggregate counters over Trigger Activity (a stats rollup), not a row "
		"collection — the browsable feed is the registered trigger_activity view"
	),
	"jarvis.chat.api.clear_chat_history": (
		"a MUTATION that deletes the caller's conversations — collection-shaped by "
		"name (the 'history' token) but it returns nothing to page, sort or filter"
	),
	# Not document collections at all: option/name strings for a control.
	"jarvis.chat.api.list_tools": "tool NAMES available to the caller — strings, not documents",
	"jarvis.chat.personalise_api.list_role_options": "role name strings for a picker",
	"jarvis.chat.custom_skills_api.list_shareable_users": "user picker feed for the share dialog",
	"jarvis.chat.app_learning_api.list_custom_apps": "installed app names on the bench — not documents",
	# Configuration, not a browsable collection.
	"jarvis.chat.personalise_api.list_question_rules": (
		"the question-bank rule configuration rendered as a settings form, not a list view"
	),
	# Transient per-session state, keyed by token and scoped to the caller's own
	# live turns. It has no identity that survives the turn, no page and no sort.
	"jarvis.chat.actions_api.list_pending_confirmations": (
		"re-surfaces the caller's OWN currently-parked confirmation cards after a "
		"reload; items are live in-flight tokens, not stored documents — they "
		"vanish when the turn resolves, so there is nothing to page, sort or filter"
	),
}


# --------------------------------------------------------------------------- #
# Accessors
# --------------------------------------------------------------------------- #
def all_views() -> tuple[ListView, ...]:
	"""Every registered surface, in declaration order."""
	return _VIEWS


def get_view(view_key: str) -> ListView | None:
	"""The registered surface for ``view_key``, or ``None``. Callers that must
	fail closed use :func:`jarvis.chat.list_filters.resolve_view`."""
	return _BY_KEY.get(view_key or "")


def document_list_views() -> tuple[ListView, ...]:
	"""Registered surfaces backed directly by one local DocType — the rows the
	Phase-0 metadata audit and the schema builder can reason about today."""
	return tuple(v for v in _VIEWS if v.is_document_list and v.root_doctype)


def filterable_views() -> tuple[ListView, ...]:
	"""Surfaces whose endpoints speak ``filters_v2`` today."""
	return tuple(v for v in _VIEWS if v.status == MIGRATED)


def endpoints_by_view() -> dict[str, tuple[str, ...]]:
	return {v.view_key: v.endpoints for v in _VIEWS}


# --------------------------------------------------------------------------- #
# Discovery annotation (P0-04)
# --------------------------------------------------------------------------- #
#: Attribute a whitelisted callable can carry to declare itself a document-list
#: endpoint for the Phase-0 discovery sweep, REGARDLESS of its name. The sweep
#: matches the ``list_*`` / ``admin_list_*`` naming families across the customer
#: API packages, but a differently-named collection endpoint (``search_records``,
#: a bespoke feed) would otherwise evade discovery — decorate it with
#: :func:`list_surface_endpoint` and it is caught and must be classified.
LIST_SURFACE_ATTR = "_jarvis_list_surface"


def list_surface_endpoint(fn):
	"""Mark a whitelisted callable as a document-list endpoint for the registry
	audit (see :data:`LIST_SURFACE_ATTR`). Purely declarative — it changes no
	runtime behaviour, it only makes the endpoint visible to the completeness
	sweep so it cannot ship unclassified."""
	setattr(fn, LIST_SURFACE_ATTR, True)
	return fn


def is_list_surface_endpoint(fn) -> bool:
	return bool(getattr(fn, LIST_SURFACE_ATTR, False))
