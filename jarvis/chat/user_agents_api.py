"""SPA endpoints for "My agents" (user agents v1).

Every endpoint is POST, needs Jarvis access, and acts only on the CALLER's own
agents (System Managers included: agents are owner-only in v1). Every endpoint but
``list_user_agents`` refuses while ``Jarvis Settings.enable_user_agents`` is off;
``list_user_agents`` answers ``enabled: False`` instead, so the SPA can hide the page.

The ``Agent`` shape every write returns (and ``list_user_agents`` lists)::

    {
        "name": str,  # row id, pass it back to the other endpoints
        "agent_id": str,  # "agent-u-<10 hex>"
        "agent_name": str,
        "description": str,
        "instructions": str,
        "enabled": bool,
        "allow_shell": bool,
        "tools": [str],  # picked tool ids, see list_pickable_tools
        "browser": {
            "enabled": bool,  # the agent has a web browser
            "mode": "listed" | "public",  # listed sites only, or any public site
            "sites": [str],  # normalised host names, [] when none are stored
        },
        "sync": {
            "status": "Pending" | "Synced" | "Failed",
            "error": str,  # machine code, "" when fine
            "message": str,  # sentence for the user, "" when fine
            "synced_at": str | None,
        },
        "schedule": {
            "enabled": bool,
            "frequency": "daily" | "weekly" | "monthly",
            "weekday": str,
            "day_of_month": int | None,
            "time": str | None,  # "HH:MM:SS"
            "prompt": str,
            "next_run_at": str | None,
        },
        "last_run": {
            "at": str | None,
            "status": "" | "Started" | "Failed",
            "error": str,
            "message": str,
            "conversation": str | None,
        },
        "ready": bool,  # enabled and sync.status Synced: Chat / Run now work
    }
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from jarvis.chat import user_agent_scheduler, user_agent_sync, user_agents
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_user

DOCTYPE = user_agents.DOCTYPE
_RETRY_LIMIT_KEY = "jarvis_user_agent_retry:{}"
_RUN_SENTENCES = {
	"usage_limit": "Your monthly usage limit is reached, so the agent cannot run now.",
	"subscription_suspended": "The subscription does not include chat right now, so the agent cannot run.",
	"llm_not_configured": "No AI model connection is set up. Connect a model first.",
	"overloaded": "Jarvis is too busy to start this run. Try again in a minute.",
	"release_update_required": "A Jarvis update is rolling out on this workspace. Try again shortly.",
	"workspace_resetting": "The workspace is being rebuilt. Try again shortly.",
	"maintenance": "This workspace is being upgraded. Try again shortly.",
}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def list_user_agents() -> dict:
	"""The caller's agents, oldest first.

	Returns ``{"enabled": bool, "max_agents": int, "browser_enabled": bool,
	"max_browser_agents": int, "browser_agents_in_use": int, "agents": [Agent]}``.
	``browser_enabled`` is the site's browser switch (hide the option while False);
	``max_browser_agents`` (3) caps ENABLED agents with a browser across the whole
	workspace, every owner included, and ``browser_agents_in_use`` is how many there
	are now. With the site flag off: ``enabled`` and ``browser_enabled`` False,
	``agents`` [] and ``browser_agents_in_use`` 0."""
	out = {
		"enabled": False,
		"max_agents": user_agents.MAX_PER_OWNER,
		"browser_enabled": False,
		"max_browser_agents": user_agents.MAX_BROWSER_PER_SITE,
		"browser_agents_in_use": 0,
		"agents": [],
	}
	if not user_agents.is_enabled():
		return out
	rows = frappe.get_list(
		DOCTYPE, filters={"owner": frappe.session.user}, fields=["*"], order_by="creation asc"
	)
	# A count across owners reveals no row; the editor needs it to cap the option.
	in_use = frappe.db.count(DOCTYPE, {"enabled": 1, "allow_browser": 1})
	return {
		**out,
		"enabled": True,
		"browser_enabled": user_agents.browser_switch_on(),
		"browser_agents_in_use": in_use,
		"agents": [_shape(r) for r in rows],
	}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def get_user_agent(name: str) -> dict:
	"""One of the caller's agents. Args: ``name`` (row id). Returns ``Agent``."""
	user_agents.require_enabled()
	return _shape(user_agents.get_owned(name))


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def create_user_agent(
	agent_name: str,
	instructions: str,
	tools: list | str | None = None,
	description: str | None = None,
	allow_shell: int | str | bool = 0,
	enabled: int | str | bool = 1,
	allow_browser: int | str | bool = 0,
	browser_mode: str | None = None,
	browser_sites: list | str | None = None,
) -> dict:
	"""Create an agent owned by the caller and start its sync.

	Args: ``agent_name`` (max 140), ``instructions`` (max 12,000), ``tools`` (list of
	tool ids, or its JSON string), ``description`` (max 1,000), ``allow_shell`` and
	``enabled`` (0/1). At most 5 agents per user. Returns ``Agent`` (sync Pending).

	Browser (only while ``list_user_agents().browser_enabled``): ``allow_browser``
	(0/1); ``browser_mode`` ``"listed"`` (default) or ``"public"``; ``browser_sites``
	as a list of strings (or that list as a JSON string) or one text with one site
	per line. Sites are normalised
	(scheme, path, query and port dropped, lower-cased, repeats dropped); "listed"
	needs 1 to 30. Refused: the switch off, a line that is not a host (the message
	quotes it), more than 3 enabled browser agents on the workspace."""
	refuse_in_tool_dispatch()
	user_agents.require_enabled()
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"agent_name": agent_name,
			"instructions": instructions,
			"description": description or "",
			"tools_json": _tools_value(tools),
			"allow_shell": cint(allow_shell),
			"enabled": cint(enabled),
			"allow_browser": cint(allow_browser),
			"browser_mode": _browser_mode(browser_mode or "listed"),
			"browser_sites": _sites_text(browser_sites),
		}
	)
	doc.insert()
	return _fresh(doc.name)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def update_user_agent(
	name: str,
	agent_name: str | None = None,
	instructions: str | None = None,
	tools: list | str | None = None,
	description: str | None = None,
	allow_shell: int | str | bool | None = None,
	allow_browser: int | str | bool | None = None,
	browser_mode: str | None = None,
	browser_sites: list | str | None = None,
) -> dict:
	"""Change an agent's definition. Only the arguments passed change; any change
	starts a sync and makes the agent not ready until it lands. Args: ``name`` plus
	any of ``agent_name``, ``instructions``, ``tools``, ``description``,
	``allow_shell``, ``allow_browser``, ``browser_mode``, ``browser_sites`` (same
	forms and rules as ``create_user_agent``). Returns ``Agent``."""
	refuse_in_tool_dispatch()
	user_agents.require_enabled()
	doc = _owned_doc(name)
	for field, value in (
		("agent_name", agent_name),
		("instructions", instructions),
		("description", description),
	):
		if value is not None:
			doc.set(field, value)
	if tools is not None:
		doc.tools_json = _tools_value(tools)
	if allow_shell is not None:
		doc.allow_shell = cint(allow_shell)
	if allow_browser is not None:
		doc.allow_browser = cint(allow_browser)
	if browser_mode is not None:
		doc.browser_mode = _browser_mode(browser_mode)
	if browser_sites is not None:
		doc.browser_sites = _sites_text(browser_sites)
	doc.save()
	return _fresh(name)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def delete_user_agent(name: str) -> dict:
	"""Delete an agent and remove it from the runtime. Its past chats stay but
	can no longer send. Args: ``name``. Returns ``{"ok": True, "name": str}``."""
	refuse_in_tool_dispatch()
	user_agents.require_enabled()
	_owned_doc(name).delete()
	return {"ok": True, "name": name}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def set_user_agent_enabled(name: str, enabled: int | str | bool) -> dict:
	"""Switch an agent on or off (off removes it from the runtime and stops its
	schedule). Args: ``name``, ``enabled`` (0/1). Returns ``Agent``."""
	refuse_in_tool_dispatch()
	user_agents.require_enabled()
	doc = _owned_doc(name)
	doc.enabled = cint(enabled)
	doc.save()
	return _fresh(name)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def set_user_agent_schedule(
	name: str,
	schedule_enabled: int | str | bool,
	schedule_frequency: str | None = None,
	schedule_time: str | None = None,
	schedule_weekday: str | None = None,
	schedule_day_of_month: int | None = None,
	schedule_prompt: str | None = None,
) -> dict:
	"""Set when the agent runs on its own. Args: ``name``, ``schedule_enabled`` (0/1),
	``schedule_frequency`` (daily / weekly / monthly), ``schedule_time`` ("HH:MM[:SS]"),
	``schedule_weekday`` (Monday..Sunday, weekly), ``schedule_day_of_month`` (1..31,
	monthly), ``schedule_prompt`` (the message each run sends, required when on, max
	4,000). Omitted arguments keep their value. No sync. Returns ``Agent`` with
	``schedule.next_run_at`` recomputed (None when off)."""
	refuse_in_tool_dispatch()
	user_agents.require_enabled()
	doc = _owned_doc(name)
	doc.schedule_enabled = cint(schedule_enabled)
	for field, value in (
		("schedule_frequency", schedule_frequency),
		("schedule_time", schedule_time),
		("schedule_weekday", schedule_weekday),
		("schedule_day_of_month", schedule_day_of_month),
		("schedule_prompt", schedule_prompt),
	):
		if value is not None:
			doc.set(field, value)
	doc.save()
	return _fresh(name)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def list_pickable_tools() -> dict:
	"""The tools the caller may give an agent on this site, grouped by app.

	Returns ``{"max_tools": 64, "groups": [{"app": str, "label": str, "tools":
	[{"id": str, "label": str, "kind": "read" | "write"}]}]}``. ``app`` is one of
	frappe, erpnext, hrms, india_compliance, connectors; a group whose app is not
	installed is left out. Pass the ``id`` values as ``tools``."""
	user_agents.require_enabled()
	return {"max_tools": user_agents.MAX_TOOLS, "groups": user_agents.pickable_groups()}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def start_user_agent_chat(name: str) -> dict:
	"""Open a new, empty chat with the agent. The SPA routes to the conversation and
	sends messages with the normal ``jarvis.chat.api.send_message``; the chat stays
	bound to the agent. Refused while the agent is off or not yet synced.

	Args: ``name``. Returns ``{"conversation": str, "agent_id": str, "agent_name": str}``."""
	refuse_in_tool_dispatch()
	user_agents.require_enabled()
	row = user_agents.get_owned(name)
	user_agents.refuse_unusable(row)
	conversation = user_agents.open_conversation(row)
	return {"conversation": conversation, "agent_id": row.agent_id, "agent_name": row.agent_name}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def run_user_agent_now(name: str, prompt: str | None = None) -> dict:
	"""Run the agent now, as a scheduled run would: a new chat with ``prompt`` (or the
	scheduled prompt when omitted) as its first message. Does not move the schedule.

	Args: ``name``, ``prompt`` (optional, max 4,000). Returns ``{"conversation": str,
	"run_id": str | None}``; the reply streams into that conversation."""
	refuse_in_tool_dispatch()
	user_agents.require_enabled()
	row = user_agents.get_owned(name)
	user_agents.refuse_unusable(row)
	text = (prompt or row.schedule_prompt or "").strip()
	if not text:
		frappe.throw(_("Write what the agent should do on this run, or set a scheduled prompt first."))
	if len(text) > 4000:
		frappe.throw(_("The prompt can be at most 4,000 characters. Shorten it and try again."))
	code = user_agent_scheduler.entitlement_code(frappe.session.user)
	if code:
		frappe.throw(_RUN_SENTENCES.get(code) or _("This agent cannot run right now. Try again later."))
	out = user_agent_scheduler.start_run(row, text, scheduled=False)
	if not out.get("ok"):
		# The removal of the chat opened for this run must outlive the refusal's
		# rollback of the request.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- keep the cleanup before the refusal
		frappe.throw(_RUN_SENTENCES["overloaded"])
	user_agent_scheduler.stamp_started(row.name, now_datetime(), out["conversation"])
	return {"conversation": out["conversation"], "run_id": out.get("run_id")}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def retry_user_agent_sync(name: str) -> dict:
	"""Send the site's agents to the runtime again after a failed or stuck sync.
	Args: ``name`` (the agent the user is looking at). Returns ``Agent``, sync Pending."""
	refuse_in_tool_dispatch()
	user_agents.require_enabled()
	row = user_agents.get_owned(name)
	key = _RETRY_LIMIT_KEY.format(frappe.session.user)
	if not frappe.flags.in_test and frappe.cache().get_value(key, expires=True):
		frappe.throw(_("A sync was just started. Wait a moment before trying again."))
	frappe.cache().set_value(key, "1", expires_in_sec=10)
	frappe.db.set_value(
		DOCTYPE, row.name, {"sync_status": "Pending", "sync_error": "", "sync_error_message": ""}
	)
	user_agent_sync.request_sync()
	return _fresh(row.name)


def _owned_doc(name: str):
	"""The caller's agent as a Document, for a write. Owner checked before loading."""
	user_agents.get_owned(name)
	return frappe.get_doc(DOCTYPE, name)


def _fresh(name: str) -> dict:
	return _shape(user_agents.get_owned(name))


def _browser_mode(value: str) -> str:
	mode = user_agents.BROWSER_MODES.get(str(value or "").strip().lower())
	if not mode:
		frappe.throw(_('The browser mode must be "listed" or "public".'))
	return mode


def _sites_text(sites) -> str:
	"""The stored form: one site per line. A JSON list string counts as a list."""
	if sites is None:
		return ""
	if isinstance(sites, str) and sites.strip().startswith("["):
		try:
			sites = frappe.parse_json(sites)
		except Exception:
			pass
	if isinstance(sites, list | tuple):
		return "\n".join(str(s or "") for s in sites)
	return str(sites)


def _tools_value(tools) -> str:
	if tools is None:
		return "[]"
	return tools if isinstance(tools, str) else frappe.as_json(tools)


def _shape(row) -> dict:
	return {
		"name": row.name,
		"agent_id": row.agent_id,
		"agent_name": row.agent_name,
		"description": row.description or "",
		"instructions": row.instructions or "",
		"enabled": bool(cint(row.enabled)),
		"allow_shell": bool(cint(row.allow_shell)),
		"tools": user_agents.picked_tools(row.tools_json),
		"browser": user_agents.browser_view(row),
		"sync": {
			"status": row.sync_status or "Pending",
			"error": row.sync_error or "",
			"message": row.sync_error_message or "",
			"synced_at": _iso(row.synced_at),
		},
		"schedule": {
			"enabled": bool(cint(row.schedule_enabled)),
			"frequency": row.schedule_frequency or "daily",
			"weekday": row.schedule_weekday or "",
			"day_of_month": row.schedule_day_of_month or None,
			"time": _time_str(row.schedule_time),
			"prompt": row.schedule_prompt or "",
			"next_run_at": _iso(row.next_run_at),
		},
		"last_run": {
			"at": _iso(row.last_run_at),
			"status": row.last_run_status or "",
			"error": row.last_run_error or "",
			"message": row.last_run_message or "",
			"conversation": row.last_run_conversation or None,
		},
		"ready": user_agents.is_ready(row),
	}


def _time_str(value) -> str | None:
	"""A Time field as "HH:MM:SS" (the database hands back a timedelta)."""
	if not value:
		return None
	seconds = int(frappe.utils.get_timedelta(value).total_seconds())
	return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _iso(value) -> str | None:
	return str(value) if value else None
