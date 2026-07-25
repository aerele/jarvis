app_name = "jarvis"
app_title = "Jarvis"
app_publisher = "Aerele"
app_description = "AI superpowers for Frappe/ERPNext"
app_email = "navin@aerele.in"
app_license = "MIT"


# ---------------------------------------------------------------------------
# Per-session bootinfo
# ---------------------------------------------------------------------------
# Frappe calls this once per page load. We use it to expose onboarding /
# access state (see jarvis/boot.py) so JS can branch on it without a
# round-trip back to the server.
boot_session = "jarvis.boot.set_jarvis_boot"

# ---------------------------------------------------------------------------
# Deployment constants
# ---------------------------------------------------------------------------
# Default URL of the Jarvis Cloud control plane the customer bench targets
# for signup, billing, plan list, container connection, and account summary.
#
# Resolution order (highest precedence first):
#   1. ``jarvis_admin_url`` in site_config.json (or common_site_config.json) -
#      the deployment's source of truth (resolved in admin_client, not here)
#   2. ``Jarvis Settings.jarvis_admin_url`` per-customer override
#   3. this hardcoded fallback for fresh installs
# Site config outranks the doctype field so a stale value left in Jarvis
# Settings by a reinstall cannot mask a correctly-configured control plane.
#
# Rebranding the deployment? Set ``jarvis_admin_url`` in site config, or
# change this string + ship a new release.
# (``DEFAULT_ADMIN_URL`` is re-exported by ``jarvis.admin_client`` so existing
# imports keep working - resolved lazily via module __getattr__ below.)
_DEFAULT_ADMIN_URL_FALLBACK = "https://admin.klerk.in"


def get_default_admin_url() -> str:
	"""Bench-wide default admin URL.

	Reads ``jarvis_admin_url`` from site config via ``frappe.conf``; falls
	back to the hardcoded default when the key is absent or empty. Resolve
	this lazily at call time - ``frappe.conf`` is only populated once a site
	is initialized, which is not guaranteed at hooks.py import time."""
	import frappe

	val = (frappe.conf.get("jarvis_admin_url") or "").strip()
	return val or _DEFAULT_ADMIN_URL_FALLBACK


def __getattr__(name: str):
	# Lazy resolution so ``jarvis.hooks.DEFAULT_ADMIN_URL`` reads site config
	# on access instead of being frozen at import time. Note: a bare
	# ``from jarvis.hooks import DEFAULT_ADMIN_URL`` still binds once at the
	# importer's import time, so prefer calling ``get_default_admin_url()``
	# directly where freshness matters.
	if name == "DEFAULT_ADMIN_URL":
		return get_default_admin_url()
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# OAuth client IDs - one per supported chat-subscription provider
# ---------------------------------------------------------------------------
# These are openclaw's hardcoded CLI client IDs. We use them (not Aerele-owned
# PKCE clients) so the refresh tokens our device flow issues are compatible
# with pi-ai's refresh path inside the container - openclaw's codex provider
# refreshes against the same client_id we used to mint.
#
# Source:
#   OpenAI: openclaw/extensions/openai/openai-codex-device-code.ts:5
#   Google Gemini: bundled with @google/gemini-cli; override via env if it drifts.
#
# Anthropic Claude is deliberately absent - openclaw has no compatible
# adapter for Claude Pro/Max subscriptions.
def _env_or_default(name: str, default: str) -> str:
	import os

	return (os.environ.get(name, "") or "").strip() or default


OAUTH_CLIENT_IDS = {
	"OpenAI": _env_or_default(
		"JARVIS_OPENAI_CODEX_OAUTH_CLIENT_ID",
		"app_EMoamEEZ73f0CkXaXp7hrann",
	),
	"Google Gemini": _env_or_default(
		"JARVIS_GEMINI_CLI_OAUTH_CLIENT_ID",
		# Bundled gemini-cli client_id (extract from upstream if drift detected).
		# Operators set the env var if our embedded default goes stale.
		"681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com",
	),
	# xAI Grok — public/PKCE client id used by cli-proxy-api's --xai-login.
	"xAI Grok": _env_or_default(
		"JARVIS_XAI_OAUTH_CLIENT_ID",
		"b1a00492-073a-47ea-816f-4c329264a828",
	),
	# Kimi (Moonshot) — public device-flow client id used by --kimi-login.
	"Kimi (Moonshot)": _env_or_default(
		"JARVIS_KIMI_OAUTH_CLIENT_ID",
		"17e5f671-d194-4dfb-9706-5516cb48c098",
	),
}


def get_oauth_client_id(provider: str) -> str:
	"""Lookup helper for jarvis.oauth code paths."""
	if provider not in OAUTH_CLIENT_IDS:
		raise ValueError(f"No OAuth client registered for provider {provider!r}")
	return OAUTH_CLIENT_IDS[provider]


# OAuth client_secrets. Required for "confidential client" flows even when
# PKCE is in play - Google's gemini-cli is one (its /token endpoint returns
# `HTTP 400: client_secret is missing` without it). OpenAI's codex CLI flow
# is pure PKCE so the secret stays empty there. Treat these as public-by-
# design (distributed with the upstream CLI), not as real secrets. Operators
# override via env when upstream rotates.
#
# Resolution order for "Google Gemini":
#   1. JARVIS_GEMINI_CLI_OAUTH_CLIENT_SECRET env var (operator override)
#   2. Extract from the gemini-cli npm package shipped as a runtime dep of
#      this app (see apps/jarvis/package.json - install with `npm install`
#      inside that dir after bench-getting the app). NOTE: the dep is pinned
#      to an EXACT version (SEC-008, Aerele-RnD/jarvis-admin#192), so this
#      no longer auto-tracks upstream rotations - when Google rotates the
#      embedded secret, either set the env override (step 1) or deliberately
#      bump the pin in package.json + package-lock.json (keep it in lockstep
#      with the fleet-agent entrypoint's GEMINI_CLI_VERSION pin).
#   3. Empty -> the token exchange fails with the explicit
#      `client_secret is missing` so the operator knows to install + restart.
OAUTH_CLIENT_SECRETS = {
	"OpenAI": _env_or_default("JARVIS_OPENAI_CODEX_OAUTH_CLIENT_SECRET", ""),
	"Google Gemini": _env_or_default("JARVIS_GEMINI_CLI_OAUTH_CLIENT_SECRET", ""),
	# xAI (PKCE) + Kimi (device-flow) are public clients — no secret.
	"xAI Grok": _env_or_default("JARVIS_XAI_OAUTH_CLIENT_SECRET", ""),
	"Kimi (Moonshot)": _env_or_default("JARVIS_KIMI_OAUTH_CLIENT_SECRET", ""),
}


def get_oauth_client_secret(provider: str) -> str:
	"""Return the client_secret for confidential-client OAuth flows. Empty
	string for PKCE-only providers (OpenAI codex). For Google Gemini falls
	back to extracting from the installed @google/gemini-cli package if no
	env var override was supplied."""
	val = OAUTH_CLIENT_SECRETS.get(provider, "")
	if val:
		return val
	if provider == "Google Gemini":
		# Imported lazily so the node_modules walk only runs when a
		# Google OAuth call asks for it.
		from jarvis.oauth.gemini_cli_secret import extract_gemini_cli_secret

		return extract_gemini_cli_secret()
	return ""


# Includes in <head>
# ------------------
# Brand styling for the Desk shell (loaded on every desk page).
app_include_css = ["/assets/jarvis/css/jarvis-brand.css"]
app_include_js = [
	"jarvis_immersive.bundle.js",
	"jarvis_widget.bundle.js",
	"jarvis_onboarding_llm.bundle.js",
	"jarvis_onboarding_banner.bundle.js",
]

# Separate frappe-ui Vue SPA (apps/jarvis/frontend) served at /jarvis. The
# catch-all routes every /jarvis/* deep link to the www/jarvis page so the
# SPA's vue-router (history mode) can handle them.
#
# The mobile PWA (apps/jarvis/pwa) gets the same treatment at /jarvis-mobile.
# Both rules are needed: the bare route serves the shell, the catch-all keeps a
# refresh on a deep link (e.g. /jarvis-mobile/c/<id>) from 404ing. The page
# itself is www/jarvis_mobile.html — underscored, because a hyphen is not a
# legal Python module name and the page has a .py controller.
#
# /jarvis-no-access is the branded landing page www/jarvis.py + www/jarvis_mobile.py
# redirect an authenticated-but-unauthorized user to, instead of opening chat.
website_route_rules = [
	{"from_route": "/jarvis/<path:app_path>", "to_route": "jarvis"},
	{"from_route": "/jarvis-mobile", "to_route": "jarvis_mobile"},
	{"from_route": "/jarvis-mobile/<path:app_path>", "to_route": "jarvis_mobile"},
	{"from_route": "/jarvis-no-access", "to_route": "jarvis_no_access"},
]

# Serves the PWA's service worker at the root-level /jarvis-mobile.sw.js, which
# is the only way it can claim a scope covering the app. See jarvis/pwa.py — the
# route is deliberately outside the /jarvis-mobile/ catch-all above.
# ManifestRenderer serves the per-tenant whitelabel manifest at the root-level
# /jarvis-mobile.webmanifest (same catch-all reasoning).
page_renderer = [
	"jarvis.pwa.ServiceWorkerRenderer",
	"jarvis.pwa.ManifestRenderer",
]

# The agents marketplace lives at the SPA route /jarvis/agents; keep the
# friendlier top-level /jarvis-agents spelling working as a redirect.
website_redirects = [
	{"source": "/jarvis-agents", "target": "/jarvis/agents"},
]

# Session hooks
# --------------

# 2026-07 latency plan, Phase 1.4: kick off a (debounced) prefix warm-up on
# login so the provider prompt cache is warm before the chat page even loads.
on_session_creation = ["jarvis.chat.prewarm.warm_on_login"]

# A fresh install runs THIS and never after_migrate, so anything a day-1 site
# needs that DocType sync does not produce has to be seeded here: the roles no
# DocType names ("Knowledge Wiki Manager", the two support roles) plus the
# Personalisation Settings defaults. See jarvis/install.py.
after_install = "jarvis.install.after_install"

# Keep the Agents Marketplace catalog (Jarvis Agent Listing) in lockstep with
# the BUNDLED jarvis/agents/registry.json on every migrate (never a runtime
# fetch — bundles are reviewed deploy artifacts, adversarial S2).
after_migrate = [
	"jarvis.chat.agent_catalog.after_migrate",
	# Behavioural pattern learning: seed Jarvis Pattern Detector State rows
	# from the detector registry (best-effort; never blocks a migrate).
	"jarvis.learning.bootstrap.after_migrate",
	# Voice & Wiki: seed the Settings Check defaults (row-existence probe;
	# an unset Check reads 0 on v16, so defaults must be materialized).
	"jarvis.learning.voice_facts.after_migrate",
	# Wiki v2 + Skills-area roles and the Personalisation Settings defaults
	# (idempotent; best-effort). NOT reached by a fresh install: install_app
	# runs after_install, never after_migrate. The Jarvis* roles survive that
	# anyway because DocType sync auto-creates any role named in a permission
	# row, but "Knowledge Wiki Manager" is named in no DocType, so it appears
	# only once a migrate has run.
	"jarvis.learning.roles.after_migrate",
	# Customizations clause: installed apps only change via migrate.
	"jarvis.chat.customizations_clause.after_migrate",
	# Re-send installed_apps to admin when the app set changed (the fleet
	# gates skills/tool-denies off the admin-persisted list).
	"jarvis.installed_apps_sync.after_migrate",
]

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"jarvis.chat.stale_scan.scan_and_mark_errored",
			# Phase-0 admission backstop (chat concurrency, WP-0): reclaim lost
			# reservations, reconcile dispatching Turn rows against Message truth,
			# age-out stale queued turns, then re-promote. Cheap no-op when no
			# non-terminal Turn rows exist (so it costs one COUNT when the feature
			# is off or idle).
			"jarvis.chat.admission.sweep",
			# Relay Pump backstop (chat concurrency, WP-1c): the LAST-RESORT
			# recovery path for the one gap the sender-driven ensure_pump cannot
			# cover — a turn committed, the pump then died, and no new send arrives
			# to revive it. Scans ALL nonterminal Turn states per-shard (age-out,
			# prepare-deadline reclaim, deadline park, finalize re-enqueue) then
			# ensure_pump for any shard with live work. Cheap no-op (one DISTINCT)
			# when no non-terminal Turn rows exist.
			"jarvis.chat.pump.watchdog",
			# Onboarding convergence safety net (review P0-2): when an LLM
			# apply parked at "pending: admin applying config" (admin accepted
			# it and its reconcile is converging server-side), probe
			# get_connection once per tick and stamp llm_pool_synced_at the
			# moment chat_readiness reads Ready. Without this, a pending apply
			# that outlives the in-job 120s poll is a permanent dead-end.
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.reconcile_pending_llm_sync",
			# 2026-07 latency plan, Phase 1.4: was */30, which left the
			# provider prompt cache (5-10 min retention) cold for most of
			# each half-hour. Every 5 min + a 4-min cooldown in prewarm.py
			# keeps the prefix warm continuously while there is recent chat
			# activity (the function itself gates on activity).
			"jarvis.chat.prewarm.keep_warm_if_active",
			# Chat-concurrency CDX-19 backstop: re-attempt macro runs parked in
			# `waiting_capacity` (a step could not be admitted because the site's turn
			# queue was momentarily full). A deferred step dispatches no turn, so the
			# turn-end chaining hook never fires for it — this cron is its ONLY resume
			# path. Bounded per run (capacity_attempts), then the run fails honestly.
			# Cheap no-op (one indexed status query) when nothing is parked.
			"jarvis.chat.macros.resume_waiting_capacity_runs",
		],
		"*/2 * * * *": [
			"jarvis.chat.turn_recovery.recover_pending_turns",
		],
		"*/10 * * * *": [
			# Learn-from-custom-apps tick: starts due Queued runs (one active
			# run bench-wide), recovers stale ones and cleans up old snapshot
			# zips. Self-gating + never raises; the real work runs on queue
			# "long" (see jarvis/learning/app_analysis.py).
			"jarvis.learning.app_analysis.tick",
		],
		"*/15 * * * *": [
			# Behavioural pattern learning tick. Hooks cron is app-static
			# (per-site rows are reset on migrate), so the window is
			# self-enforced: the tick bails on the site_config kill switch,
			# the enabled flag, self-host and outside-window times.
			"jarvis.learning.orchestrator.tick",
		],
	},
	"hourly": [
		# Session lifecycle: free dormant conversations' openclaw sessions, reap
		# abandoned empty chats, and reap orphaned throwaway sessions
		# (title/prewarm/polish, deleted conversations). Batch-capped; bench
		# history untouched.
		#
		# Hourly, not daily: this sweep is the backstop for the throwaway session
		# kinds, and one pass a day capped at 25 could never keep up with a bench
		# minting them every few minutes. Parts 1 + 2 keep their own multi-day
		# grace windows and are cheap no-ops when nothing is due, so the extra
		# ticks cost one gateway connect.
		"jarvis.chat.session_lifecycle.rotate_dormant_sessions",
		# Sprint-3 (2026-06-16 review): bench has no way to know if the
		# container's OAuth profile silently went away (refresh-token
		# failure, operator clear, re-provision without re-push). Hourly
		# reconciliation keeps last_sync_status honest so the UI can
		# render a "reconnect" banner instead of "Connected" until the
		# user hits a ProviderAuthError mid-chat.
		"jarvis.oauth.cron.poll_oauth_refresh_status",
		# Fire any scheduled macros whose next_run_at has passed.
		"jarvis.chat.macro_scheduler.run_due_macros",
		# Fire any due scheduled auditor agents. Identity-safe (runs each audit
		# as its owner, never Administrator); budget-capped; advances only on a
		# successful enqueue. See jarvis/chat/agent_scheduler.py.
		"jarvis.chat.agent_scheduler.run_due_agent_audits",
		# A8 backstop: fail agent runs stuck `running` past the max duration and
		# tear down their orphaned per-run session bearers (a crashed delegate
		# would otherwise leave a live session credential forever).
		"jarvis.chat.agent_scheduler.reap_stale_agent_runs",
		# Recovery-completeness batch: spike alarm if the 24h recovered-turn
		# rate is high enough to suggest a sick gateway (deduped to roughly
		# once a day inside the function).
		"jarvis.chat.turn_recovery.recovery_rate_watch",
	],
	"daily": [
		"jarvis.onboarding.sync_connection",
		# C2 (2026-06-16 review): nudge operators when the bench's
		# agent_token is approaching or past its configured max age.
		# Daily is plenty - the warning window is 7 days.
		"jarvis.oauth.cron.check_agent_token_age",
		# Daily voice-note sweep: mine New voice notes into learned-pattern
		# candidates + wiki updates (self-gating; see jarvis/learning/voice_facts.py).
		"jarvis.learning.voice_facts.process_daily",
		# Skills-area rework (DESIGN.md section 3): daily backstops for the
		# Personalise question bank. materialize_questions_daily mints questions
		# for Proposed learned-pattern findings that missed the immediate
		# lifecycle hook (per-user daily cap + dedupe); materialize_rule_questions
		# fans admin-authored question rules out to in-scope users (uncapped).
		"jarvis.learning.questions.materialize_questions_daily",
		"jarvis.learning.questions.materialize_rule_questions",
		# Daily chat-transcript question mining: mine recent conversations into
		# learned-pattern candidates whose Personalise questions confirm the
		# finding; answers ride the existing note -> wiki/skill pipeline
		# (self-gating; see jarvis/learning/chat_mining.py).
		"jarvis.learning.chat_mining.process_daily",
		# Daily push of the User/Role/Org wiki-utilization graph to admin (the
		# DB-only scope/role tiers; admin overlays telemetry activity). Not on
		# every wiki save — too chatty for an analytics view.
		"jarvis.chat.wiki_graph.sync",
		# Daily append of org-wide graph totals to Jarvis Wiki Graph History (one
		# row/day) — the measured Knowledge-Evolution series for the Evolution tab.
		"jarvis.chat.wiki_graph.record_history_snapshot",
		# Architecture A (fleet usage spec §3/§5): best-effort daily push of the
		# bench's month-to-date per-user + per-model usage rollup to admin. Self-
		# gating (skips self-hosted / unconfigured / not-onboarded); never raises.
		"jarvis.chat.usage_push.push_usage_rollup",
	],
	"weekly": [
		# Wiki v2 health check: deterministic lint over Active pages
		# (contradictions, staleness, orphans, near-duplicate titles);
		# summary lands on the Jarvis Settings RO fields. Swallows errors.
		"jarvis.learning.wiki_lint.scheduled_lint",
	],
}

# Python type annotations on whitelisted endpoints
# ------------------------------------------------
# Auto-update controller files with type annotations + require all
# whitelisted methods to be type-annotated. Both are required by the
# Sprint-1 type-safety pass on the customer-facing endpoints.
export_python_type_annotations = True
require_type_annotated_api_methods = True

# ---------------------------------------------------------------------------
# Schema-cache invalidation
# ---------------------------------------------------------------------------
# get_schema caches each DocType's schema in Redis with a short TTL. Bust that
# cache the moment a schema-defining doc changes, so the agent never builds a
# write off a stale field list inside the TTL window (a Custom Field added via
# Customize Form must show up immediately, not 5 minutes later). Client Script is
# included because get_schema's `actions` hint is scraped from the DocType's form
# JS, which includes enabled Form Client Scripts - a newly-added button must
# surface without waiting out the TTL.
_CLEAR_SCHEMA_CACHE = "jarvis.tools.get_schema.clear_schema_cache"
# The customizations clause counts the same schema doctypes, so it rides
# the same events (Frappe runs every handler in a list value).
_CLEAR_CUSTOMIZATIONS_CLAUSE = "jarvis.chat.customizations_clause.clear_clause_cache"
doc_events = {
	dt: {
		"on_update": [_CLEAR_SCHEMA_CACHE, _CLEAR_CUSTOMIZATIONS_CLAUSE],
		"on_trash": [_CLEAR_SCHEMA_CACHE, _CLEAR_CUSTOMIZATIONS_CLAUSE],
	}
	for dt in ("DocType", "Custom Field", "Property Setter", "Workflow", "Client Script")
}

# Org-scope wiki pages are mirrored as markdown into the tenant container
# workspace; every page write/delete enqueues a debounced mirror sync (the
# handler filters to Org scope itself and no-ops when the tenant is
# unreachable — it never raises into the save path).
_WIKI_MIRROR_SYNC = "jarvis.chat.wiki_mirror.on_wiki_page_change"
doc_events["Jarvis Wiki Page"] = {
	"after_insert": _WIKI_MIRROR_SYNC,
	"on_update": _WIKI_MIRROR_SYNC,
	"on_trash": _WIKI_MIRROR_SYNC,
}

# Skills-area rework (DESIGN.md section 3): an active Personalise question rule
# (re)materializes its questions to in-scope users on save, in addition to the
# daily sweep. The handler enqueues a deduped after-commit job (never blocks the
# save, inactive rules no-op).
doc_events["Jarvis Personalise Question Rule"] = {
	"on_update": "jarvis.learning.questions.on_rule_update",
}

# Phase-0 admission (chat concurrency, WP-0): a deleted conversation must not
# leave orphaned Jarvis Chat Turn rows behind (they would linger as ghost queued/
# dispatching rows in the admission shard). Cascade-delete them on trash.
doc_events["Jarvis Conversation"] = {
	"on_trash": "jarvis.chat.admission.on_conversation_trash",
}

# ---------------------------------------------------------------------------
# Jarvis Triggers (user-defined doc-event automations)
# ---------------------------------------------------------------------------
# One wildcard dispatcher for the eight supported events. Hot-path cost when a
# doctype has no triggers: one cached-map read + a dict lookup, no DB (see
# jarvis.triggers.engine.dispatch). Merged into any existing "*" entry so
# other wildcard handlers are preserved.
_TRIGGER_DISPATCH = "jarvis.triggers.engine.dispatch"
_star_doc_events = doc_events.setdefault("*", {})
for _trigger_event in (
	"validate",
	"before_submit",
	"after_insert",
	"on_update",
	"on_submit",
	"on_cancel",
	"on_trash",
	"on_update_after_submit",
):
	_existing_handlers = _star_doc_events.get(_trigger_event)
	if _existing_handlers is None:
		_star_doc_events[_trigger_event] = [_TRIGGER_DISPATCH]
	elif isinstance(_existing_handlers, str):
		_star_doc_events[_trigger_event] = [_existing_handlers, _TRIGGER_DISPATCH]
	else:
		_existing_handlers.append(_TRIGGER_DISPATCH)

# Jarvis Trigger Activity is an append-only log; frappe's standard Log
# Settings clearing reaps rows older than 90 days (the controller's
# clear_old_logs mirrors core's WebhookRequestLog).
default_log_clearing_doctypes = {
	"Jarvis Trigger Activity": 90,
}

# ---------------------------------------------------------------------------
# Wiki page scoping (wiki v2)
# ---------------------------------------------------------------------------
# Org/Role/User visibility for Jarvis Wiki Page, enforced at the ORM: list
# queries via permission_query_conditions, per-doc access via has_permission.
# Matrix + SQL fragments live in jarvis/chat/wiki_permissions.py.
permission_query_conditions = {
	"Jarvis Wiki Page": "jarvis.chat.wiki_permissions.wiki_page_query_conditions",
}
has_permission = {
	"Jarvis Wiki Page": "jarvis.chat.wiki_permissions.has_wiki_page_permission",
}

# ---------------------------------------------------------------------------
# Chat doctype ownership scoping (security review PART 1, TASK 7)
# ---------------------------------------------------------------------------
# Row-level owner scoping for the chat doctypes at the ORM, so generic REST
# (/api/resource, frappe.client.*, reportview) and every future endpoint
# inherit it automatically instead of relying on a hand-rolled owner check in
# each whitelisted function. Conversation/Voice scope by the row owner;
# Message/Approval scope by the LINKED conversation's owner (+ DocShare for
# Approval). Matrix + SQL fragments live in jarvis/chat/chat_permissions.py.
# The doctype permission rows carry role "Jarvis User" (not "All"), so the role
# is genuinely load-bearing: revoking it denies all four via REST.
permission_query_conditions.update(
	{
		"Jarvis Conversation": "jarvis.chat.chat_permissions.conversation_query_conditions",
		"Jarvis Chat Message": "jarvis.chat.chat_permissions.message_query_conditions",
		"Jarvis Approval Request": "jarvis.chat.chat_permissions.approval_query_conditions",
		"Jarvis Voice Note": "jarvis.chat.chat_permissions.voice_note_query_conditions",
	}
)
has_permission.update(
	{
		"Jarvis Conversation": "jarvis.chat.chat_permissions.has_conversation_permission",
		"Jarvis Chat Message": "jarvis.chat.chat_permissions.has_message_permission",
		"Jarvis Approval Request": "jarvis.chat.chat_permissions.has_approval_permission",
		"Jarvis Voice Note": "jarvis.chat.chat_permissions.has_voice_note_permission",
	}
)

# ---------------------------------------------------------------------------
# Skills / Personalise scoping (security review PART 2)
# ---------------------------------------------------------------------------
# TASK 13: Jarvis Custom Skill visibility (owner OR scope=Org OR scope=Role
# role-match OR shared-with) at the ORM, so the four hand-rolled read surfaces
# (generic REST, SPA list/get, plugin find/get) can never disagree. Matrix +
# SQL fragment live in jarvis/chat/skill_permissions.py (reuses the controller's
# user_can_use_skill rule).
# TASK 17: Jarvis Personalise Question scoped on the `user` field (not `owner`)
# so generic REST matches the API's user-based scoping and survives drift.
permission_query_conditions.update(
	{
		"Jarvis Custom Skill": "jarvis.chat.skill_permissions.skill_query_conditions",
		"Jarvis Personalise Question": "jarvis.chat.personalise_permissions.personalise_question_query_conditions",
	}
)
has_permission.update(
	{
		"Jarvis Custom Skill": "jarvis.chat.skill_permissions.has_skill_permission",
		"Jarvis Personalise Question": "jarvis.chat.personalise_permissions.has_personalise_question_permission",
	}
)

# ---------------------------------------------------------------------------
# File Box / Macros / Agents scoping (security review PART 3)
# ---------------------------------------------------------------------------
# TASK 22: File is a GLOBAL doctype. Core already registers a permissive
# permission_query_conditions["File"]; Frappe ANDs the hooks, so the Jarvis
# fragment ONLY restricts rows attached to Jarvis Conversation (owner / linked-
# conversation-owner) and lets every non-Jarvis file (avatars, print formats,
# other doctypes' attachments) through. NO has_permission["File"] hook — core's
# per-doc byte deferral already protects the bytes; a second hook could break it.
# TASK 23/24: Jarvis Macro / Macro Run scoped by the row owner at the ORM (Run's
# owner is the macro owner by construction); the create arm of the Macro Run hook
# also blocks attaching a run to another user's macro/conversation.
# TASK 29: the four owner/installer-scoped agent doctypes (Installation, Run,
# Finding, Activity). The Listing catalog stays All-readable (no owner hook); its
# skill_bundle IP is permlevel-guarded (TASK 33) instead.
permission_query_conditions.update(
	{
		"File": "jarvis.chat.file_permissions.file_query_conditions",
		"Jarvis Macro": "jarvis.chat.macro_permissions.macro_query_conditions",
		"Jarvis Macro Run": "jarvis.chat.macro_permissions.macro_run_query_conditions",
		"Jarvis Agent Installation": "jarvis.chat.agent_permissions.installation_query_conditions",
		"Jarvis Agent Run": "jarvis.chat.agent_permissions.run_query_conditions",
		"Jarvis Agent Finding": "jarvis.chat.agent_permissions.finding_query_conditions",
		"Jarvis Agent Activity": "jarvis.chat.agent_permissions.activity_query_conditions",
	}
)
has_permission.update(
	{
		"Jarvis Macro": "jarvis.chat.macro_permissions.has_macro_permission",
		"Jarvis Macro Run": "jarvis.chat.macro_permissions.has_macro_run_permission",
		"Jarvis Agent Installation": "jarvis.chat.agent_permissions.has_installation_permission",
		"Jarvis Agent Run": "jarvis.chat.agent_permissions.has_run_permission",
		"Jarvis Agent Finding": "jarvis.chat.agent_permissions.has_finding_permission",
		"Jarvis Agent Activity": "jarvis.chat.agent_permissions.has_activity_permission",
	}
)

# ---------------------------------------------------------------------------
# Per-user settings / usage scoping (security review PART 4 REVISED, TASK 52)
# ---------------------------------------------------------------------------
# Jarvis User Settings (#278) was role:"All" + if_owner with no ORM hook — the
# anti-pattern Parts 1-3 eliminated. The doctype rows are now "Jarvis User"
# (role load-bearing, no portal-user reach), and this hook keys row scoping on
# the `user` field (matching the API's user-based scoping, surviving owner/user
# drift). The admin tier (SM / Jarvis Admin / Administrator) is unrestricted so
# generic REST (frappe.get_list / /api/resource) of Jarvis User Settings returns
# every row for an admin. (The admin usage board's admin_list_user_usage uses
# frappe.get_all, which hardcodes ignore_permissions=True and therefore sees all
# rows regardless of this hook — the admin bypass here matters for the
# permission-checked frappe.get_list / generic-REST surface, not for that fn.)
permission_query_conditions.update(
	{
		"Jarvis User Settings": "jarvis.chat.user_settings_permissions.user_settings_query_conditions",
	}
)
has_permission.update(
	{
		"Jarvis User Settings": "jarvis.chat.user_settings_permissions.has_user_settings_permission",
	}
)

# ---------------------------------------------------------------------------
# Jarvis Trigger scoping
# ---------------------------------------------------------------------------
# Read is org-wide for jarvis users (the doctype rows grant Jarvis User read;
# no row scoping), but write/create/delete are denied at the ORM for everyone
# below System Manager / Jarvis Admin. Jarvis Trigger Activity deliberately
# has NO hooks: its doctype perms are SM-only and
# jarvis.chat.triggers_api.list_activity_page serves visibility-filtered rows
# (read access on the target doc) to everyone else.
permission_query_conditions.update(
	{
		"Jarvis Trigger": "jarvis.chat.trigger_permissions.trigger_query_conditions",
	}
)
has_permission.update(
	{
		"Jarvis Trigger": "jarvis.chat.trigger_permissions.has_trigger_permission",
	}
)

# ---------------------------------------------------------------------------
# Jarvis Dashboard scoping
# ---------------------------------------------------------------------------
# The doctype rows grant Jarvis User r/w/c/d broadly; these hooks scope reads
# to the dashboard's audience (Org / Role / User / owner — blank scope is
# PRIVATE) and deny writes/deletes to everyone but the owner and the admin
# tier. The create-time scope-widening gate (plain users may not create
# Org/Role dashboards) lives in the DocType controller — "create" reaches the
# hook without a doc.
permission_query_conditions.update(
	{
		"Jarvis Dashboard": "jarvis.chat.dashboard_permissions.dashboard_query_conditions",
	}
)
has_permission.update(
	{
		"Jarvis Dashboard": "jarvis.chat.dashboard_permissions.has_dashboard_permission",
	}
)
