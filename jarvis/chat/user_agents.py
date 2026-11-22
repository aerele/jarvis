"""User agents: agents a Jarvis user builds for themselves (v1).

An agent is one ``Jarvis User Agent`` row. It runs as its OWNER, with only the
tools the owner picked, and only the owner can chat with it, run it or edit it.
Everything is off unless ``Jarvis Settings.enable_user_agents`` is on.

The runtime knows an agent by its ``agent_id`` (``agent-u-<10 hex>``). A chat
addresses it with the session key ``agent:<agent_id>:dashboard:jarvis-chat-...``
(the same self-addressing shape a role profile uses), and its conversation
carries ``user_agent`` so a session minted again later still reaches the agent.

This module holds the rules every entry point shares: the flag, the tool
catalogue, who may use an agent, the session key and the ``call_tool`` limiter.
The runtime sync is ``user_agent_sync``; the schedule is ``user_agent_scheduler``.
"""

from __future__ import annotations

import re
import secrets
import time

import frappe
from frappe import _
from frappe.utils import cint

DOCTYPE = "Jarvis User Agent"
CONV = "Jarvis Conversation"
CHAT_SESSION = "Jarvis Chat Session"
SETTINGS = "Jarvis Settings"
FLAG = "enable_user_agents"

ID_PREFIX = "agent-u-"
ID_RE = re.compile(r"^agent-u-[0-9a-f]{10}$")
# The one session key shape a user agent may have. Anything else that starts with
# ``agent:agent-u-`` was not minted here and is refused outright.
SESSION_RE = re.compile(r"^agent:(agent-u-[0-9a-f]{10}):dashboard:")
SESSION_PREFIX = "agent:" + ID_PREFIX

MAX_PER_OWNER = 5
# The runtime takes at most 20 agents per workspace (fleet contract 1.57).
MAX_PER_SITE = 20
MAX_TOOLS = 64
TOOL_PREFIX = "jarvis__"

# Tools a user agent may be given, by the app that must be installed for them to
# work. Built from the closed shared-chat policy (permission-checked reads and
# explicit writes), so private memory, delegate-only sources and assistant
# authoring tools can never be picked.
_APP_TOOLS = {
	"erpnext": frozenset(
		"""get_stock_balance get_valuation_rate scan_barcode get_balance_on
		get_customer_outstanding get_party_dashboard_info get_exchange_rate get_fiscal_year
		get_itemised_tax_breakup preview_landed_cost_voucher""".split()
	),
	"hrms": frozenset(
		"""get_leave_balance_on get_leaves_for_period get_leave_details
		get_holidays_for_employee get_employee_shift""".split()
	),
	"india_compliance": frozenset({"get_gstr1_summary"}),
}
_CONNECTOR_TOOLS = frozenset({"call_connector", "list_connector_actions"})
# Read tools in the shared policy that only a marketplace delegate run can use.
_DELEGATE_ONLY = frozenset({"get_engagement_config", "get_bank_recon_inputs"})
_GROUP_LABELS = {
	"frappe": "Records and reports",
	"erpnext": "ERPNext",
	"hrms": "HR",
	"india_compliance": "India Compliance",
	"connectors": "Connectors",
}

_LOCAL_ROWS = "jarvis_user_agent_rows"


class UserAgentUnavailableError(frappe.ValidationError):
	"""The agent cannot be used right now; the message says why and what to do."""


def is_enabled() -> bool:
	"""The site flag, read fresh (a switch-off applies to the next call). False on
	any read failure (a site that has not migrated yet)."""
	try:
		return bool(cint(frappe.db.get_single_value(SETTINGS, FLAG, cache=False)))
	except Exception:
		return False


def require_enabled() -> None:
	if not is_enabled():
		frappe.throw(
			_("Custom agents are turned off on this site. Ask your Jarvis admin to turn them on."),
			frappe.PermissionError,
		)


def new_agent_id() -> str:
	"""A fresh ``agent-u-<10 hex>`` id no row holds yet."""
	for _attempt in range(5):
		candidate = ID_PREFIX + secrets.token_hex(5)
		if not frappe.db.exists(DOCTYPE, {"agent_id": candidate}):
			return candidate
	frappe.throw(_("Could not pick an id for this agent. Try again."))


def pickable_tools() -> dict[str, str]:
	"""``{tool: group}`` for every tool an agent may be given on this site."""
	from jarvis.chat.collaboration_tools import READ_TOOLS, WRITE_TOOLS
	from jarvis.tools.registry import list_tools

	installed = set(frappe.get_installed_apps())
	registered = set(list_tools())
	out = {}
	for tool in sorted(((READ_TOOLS | WRITE_TOOLS) & registered) - _DELEGATE_ONLY):
		app = next((a for a, tools in _APP_TOOLS.items() if tool in tools), None)
		if app and app not in installed:
			continue
		out[tool] = "connectors" if tool in _CONNECTOR_TOOLS else app or "frappe"
	return out


def pickable_groups() -> list[dict]:
	"""The catalogue the editor shows, grouped by app (see ``list_pickable_tools``)."""
	from jarvis.chat.collaboration_tools import tool_kind

	groups: dict[str, list] = {}
	for tool, group in pickable_tools().items():
		groups.setdefault(group, []).append({"id": tool, "label": tool_label(tool), "kind": tool_kind(tool)})
	return [
		{"app": key, "label": _GROUP_LABELS[key], "tools": groups[key]}
		for key in _GROUP_LABELS
		if key in groups
	]


def tool_label(tool: str) -> str:
	return tool.replace("_", " ").capitalize()


def normalize_tool(tool) -> str:
	value = str(tool or "").strip()
	return value[len(TOOL_PREFIX) :] if value.startswith(TOOL_PREFIX) else value


def picked_tools(raw) -> list[str]:
	"""The stored ``tools_json`` as a list of bare tool names (empty when unreadable)."""
	try:
		value = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except Exception:
		return []
	if not isinstance(value, list):
		return []
	return [normalize_tool(t) for t in value if isinstance(t, str) and normalize_tool(t)]


def get_owned(name: str, user: str | None = None) -> frappe._dict:
	"""The agent row ``name`` if ``user`` (default: the caller) owns it, else a
	PermissionError. System Managers are not exempt: agents are owner-only in v1."""
	user = user or frappe.session.user
	row = frappe.db.get_value(DOCTYPE, name, "*", as_dict=True)
	if not row or row.owner != user:
		frappe.throw(_("This agent was not found, or it belongs to someone else."), frappe.PermissionError)
	return row


def is_ready(row) -> bool:
	"""Enabled, and the runtime holds its CURRENT content: every definition change
	sets Pending, and only a push that carried this exact content sets Synced."""
	return bool(cint(row.get("enabled")) and row.get("sync_status") == "Synced")


def refuse_unusable(row) -> None:
	"""Throw when ``row`` cannot chat or run now: switched off, or its current
	content is not on the runtime (Pending or Failed)."""
	if not cint(row.enabled):
		frappe.throw(_("This agent is switched off. Turn it on to use it."), UserAgentUnavailableError)
	if not is_ready(row):
		frappe.throw(
			_("This agent is not ready yet. Wait for its sync to finish, or use Retry sync."),
			UserAgentUnavailableError,
		)


def mint_session(agent_id: str, user: str) -> str:
	"""Mint a session key addressing ``agent_id`` and insert its Chat Session row
	(the key -> user map ``call_tool`` resolves). Not committed. The key is created
	by the runtime on first use, like a role profile's, so the agent must be on the
	runtime already: the caller checks ``refuse_unusable`` first."""
	from jarvis.chat import device
	from jarvis.chat.entities import scrub

	if not ID_RE.match(agent_id or ""):
		frappe.throw(_("This agent has no valid id. Save it again."), UserAgentUnavailableError)
	settings = frappe.get_single(SETTINGS)
	if not settings.agent_url or not device.has_paired_token(settings):
		frappe.throw(_("Jarvis isn't connected yet. Ask your workspace admin to finish the setup."))
	key = f"agent:{agent_id}:dashboard:jarvis-chat-{scrub(user)}-{int(time.time() * 1000)}"
	frappe.get_doc(
		{
			"doctype": CHAT_SESSION,
			"session_key": key,
			"user": user,
			"chat_device_id": (settings.chat_device_id or "").strip(),
		}
	).insert(ignore_permissions=True)
	return key


def mint_for_conversation(agent_id: str, user: str) -> str:
	"""A fresh key for an existing agent chat whose session was reclaimed or
	invalidated. Refuses, rather than falling back to the main agent, when the agent
	is gone, off, or not ``user``'s."""
	require_enabled()
	name = frappe.db.get_value(DOCTYPE, {"agent_id": agent_id}, "name")
	if not name:
		frappe.throw(_("This agent was deleted. Start a new chat."), UserAgentUnavailableError)
	refuse_unusable(get_owned(name, user))
	return mint_session(agent_id, user)


def open_conversation(row, *, agent_initiated: bool = False) -> str:
	"""Create a conversation bound to agent ``row`` for its owner (the current user)
	and return its name. ``session_key`` and ``user_agent`` are permlevel 1, so they
	are written straight to the row, as the chat worker writes ``session_key``."""
	key = mint_session(row.agent_id, row.owner)
	conv = frappe.get_doc(
		{
			"doctype": CONV,
			"title": (row.agent_name or "Agent")[:140],
			"status": "Active",
			"agent_initiated": 1 if agent_initiated else 0,
		}
	)
	conv.insert()
	frappe.db.set_value(
		CONV, conv.name, {"session_key": key, "user_agent": row.agent_id}, update_modified=False
	)
	return conv.name


def bound_agent(conv) -> str | None:
	"""The agent id a conversation is bound to, or None for a normal chat. Only a
	non-empty string counts, so a stand-in conversation object reads as unbound."""
	getter = getattr(conv, "get", None)
	value = getter("user_agent") if callable(getter) else getattr(conv, "user_agent", None)
	return value if isinstance(value, str) and value else None


def agent_names(agent_ids, owner: str) -> dict[str, str]:
	"""``{agent_id: agent_name}`` for the agents ``owner`` owns among ``agent_ids``,
	in one query. An id that is gone, or is not ``owner``'s, is simply missing."""
	ids = sorted({i for i in agent_ids if isinstance(i, str) and i})
	if not ids:
		return {}
	try:
		rows = frappe.get_list(
			DOCTYPE,
			filters={"agent_id": ["in", ids], "owner": owner},
			fields=["agent_id", "agent_name"],
			limit_page_length=len(ids),
		)
	except frappe.PermissionError:
		# A cosmetic label: a user who can no longer read agents must still open the chat.
		return {}
	return {r.agent_id: r.agent_name for r in rows}


def refuse_send(conv_doc) -> None:
	"""``send_message`` guard: a chat bound to an agent only runs while that agent
	is usable by the chat's owner."""
	agent_id = bound_agent(conv_doc)
	if not agent_id:
		return
	require_enabled()
	name = frappe.db.get_value(DOCTYPE, {"agent_id": agent_id}, "name")
	if not name:
		frappe.throw(_("This agent was deleted. Start a new chat."), UserAgentUnavailableError)
	refuse_unusable(get_owned(name, conv_doc.owner))


def tool_refusal(session_key: str | None, user: str, tool: str) -> str | None:
	"""The ``call_tool`` limiter. None for any key that is not a user agent's (one
	string check, no query). For a user agent key: a refusal sentence unless the
	flag is on, the agent exists, is enabled, is owned by the session ``user`` and
	the tool is one its owner picked. One indexed read per request at most."""
	key = session_key if isinstance(session_key, str) else ""
	if not key.startswith(SESSION_PREFIX):
		return None
	match = SESSION_RE.match(key)
	if not match:
		return "This agent session is not valid. Start a new chat with the agent."
	if not is_enabled():
		return "Custom agents are turned off on this site, so this agent cannot use tools."
	row = _row_for_request(match.group(1))
	if not row:
		return "This agent was deleted, so it cannot use tools. Start a new chat."
	if row.owner != user:
		return "This agent belongs to someone else, so it cannot act for this user."
	if not cint(row.enabled):
		return "This agent is switched off, so it cannot use tools. Its owner can turn it on."
	if normalize_tool(tool) not in set(picked_tools(row.tools_json)):
		return (
			f"This agent is not allowed to use the {normalize_tool(tool)} tool. "
			"Its owner can add the tool on the Agents page."
		)
	return None


def _row_for_request(agent_id: str):
	"""The limiter's row, read once per request and cached on ``frappe.local``."""
	cache = getattr(frappe.local, _LOCAL_ROWS, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, _LOCAL_ROWS, cache)
	if agent_id not in cache:
		cache[agent_id] = frappe.db.get_value(
			DOCTYPE, {"agent_id": agent_id}, ["owner", "enabled", "tools_json"], as_dict=True
		)
	return cache[agent_id]
