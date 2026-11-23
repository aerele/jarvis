"""Jarvis User Agent: an agent one Jarvis user builds for their own work.

Every save surface (the Agents page, Desk, REST, a data import) goes through these
checks. The agent's tools never do: an agent must not widen its own tools or turn
on its own shell, so a write from inside a tool call is refused outright.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, escape_html

from jarvis.chat import user_agents
from jarvis.permissions import NotRenamable

MAX_NAME = 140
MAX_DESCRIPTION = 1000
MAX_INSTRUCTIONS = 12000
MAX_PROMPT = 4000

# A change to one of these changes what the runtime holds, so it is pushed again.
SYNC_FIELDS = (
	"agent_name",
	"description",
	"instructions",
	"tools_json",
	"allow_shell",
	"enabled",
	"allow_browser",
	"browser_mode",
	"browser_sites",
)
_SCHEDULE_FIELDS = (
	"enabled",
	"schedule_enabled",
	"schedule_frequency",
	"schedule_weekday",
	"schedule_day_of_month",
	"schedule_time",
)


class JarvisUserAgent(NotRenamable, Document):
	def before_insert(self):
		self.agent_id = user_agents.new_agent_id()
		self.sync_status = "Pending"

	def validate(self):
		self._refuse_tool_write()
		user_agents.require_enabled()
		self._validate_owner()
		self._validate_caps()
		self._validate_text()
		self._validate_tools()
		self._validate_browser()
		self._validate_schedule()
		self._keep_server_fields()
		self._mark_pending()
		self._recompute_next_run()

	def on_update(self):
		if any(self._changed(f) for f in SYNC_FIELDS):
			from jarvis.chat import user_agent_sync

			user_agent_sync.request_sync()

	def on_trash(self):
		self._refuse_tool_write()
		if frappe.session.user != self.owner and "System Manager" not in frappe.get_roles():
			frappe.throw(_("Only this agent's owner can delete it."), frappe.PermissionError)

	def after_delete(self):
		from jarvis.chat import user_agent_sync

		user_agent_sync.request_sync()

	def _changed(self, field: str) -> bool:
		"""``has_value_changed`` that reads empty and NULL as the same value. True for
		every field on insert (there is no saved copy to compare with)."""
		before = self.get_doc_before_save()
		if not before:
			return True
		if not before.get(field) and not self.get(field):
			return False
		return self.has_value_changed(field)

	def _refuse_tool_write(self):
		from jarvis.tools.registry import in_tool_dispatch

		if in_tool_dispatch():
			frappe.throw(
				_("Agents can only be changed on the Agents page, not from a chat."), frappe.PermissionError
			)

	def _validate_owner(self):
		owner = self.owner or frappe.session.user
		if owner in ("Administrator", "Guest"):
			frappe.throw(
				_("Sign in as your own user to create an agent. Administrator and Guest cannot own one."),
				frappe.PermissionError,
			)
		before = self.get_doc_before_save()
		if before and before.owner != self.owner:
			frappe.throw(_("An agent cannot be handed to another user."), frappe.PermissionError)
		if before and frappe.session.user != self.owner:
			frappe.throw(_("Only this agent's owner can change it."), frappe.PermissionError)

	def _validate_caps(self):
		if not self.is_new():
			return
		owner = self.owner or frappe.session.user
		if frappe.db.count(self.doctype, {"owner": owner}) >= user_agents.MAX_PER_OWNER:
			frappe.throw(
				_("You can have at most {0} agents. Delete one to make room.").format(
					user_agents.MAX_PER_OWNER
				)
			)
		if frappe.db.count(self.doctype) >= user_agents.MAX_PER_SITE:
			frappe.throw(
				_("This workspace already has {0} agents, the most it can run.").format(
					user_agents.MAX_PER_SITE
				)
			)

	def _validate_text(self):
		self.agent_name = (self.agent_name or "").strip()
		self.instructions = (self.instructions or "").strip()
		self.description = (self.description or "").strip()
		self.schedule_prompt = (self.schedule_prompt or "").strip()
		if not self.agent_name:
			frappe.throw(_("Give the agent a name."))
		if not self.instructions:
			frappe.throw(_("Tell the agent what to do in its instructions."))
		for value, limit, label in (
			(self.agent_name, MAX_NAME, _("The name")),
			(self.description, MAX_DESCRIPTION, _("The description")),
			(self.instructions, MAX_INSTRUCTIONS, _("The instructions")),
			(self.schedule_prompt, MAX_PROMPT, _("The scheduled prompt")),
		):
			if len(value) > limit:
				frappe.throw(
					_("{0} can be at most {1} characters. Shorten it and save again.").format(label, limit)
				)

	def _validate_tools(self):
		if not self._changed("tools_json"):
			return
		raw = self.tools_json
		try:
			value = frappe.parse_json(raw) if isinstance(raw, str) and raw.strip() else raw or []
		except Exception:
			value = None
		if not isinstance(value, list) or any(not isinstance(t, str) for t in value):
			frappe.throw(_("Tools must be a list of tool names."))
		picked = list(dict.fromkeys(user_agents.normalize_tool(t) for t in value if t.strip()))
		allowed = user_agents.pickable_tools()
		unknown = [t for t in picked if t not in allowed]
		if unknown:
			frappe.throw(
				_(
					"These tools cannot be given to an agent on this site: {0}. Remove them and save again."
				).format(", ".join(unknown))
			)
		if len(picked) > user_agents.MAX_TOOLS:
			frappe.throw(_("An agent can have at most {0} tools.").format(user_agents.MAX_TOOLS))
		self.tools_json = json.dumps(sorted(picked))

	def _validate_browser(self):
		"""Sites are stored normalised, one per line. A listed-sites browser needs 1 to
		30; turning a browser on needs the site switch and a free slot."""
		if self.browser_mode not in (user_agents.BROWSER_LISTED, user_agents.BROWSER_PUBLIC):
			self.browser_mode = user_agents.BROWSER_LISTED
		turning_on = cint(self.allow_browser) and self._changed("allow_browser")
		if turning_on and not user_agents.browser_switch_on():
			frappe.throw(
				_("Web browsing for agents is turned off on this site. Ask your Jarvis admin to turn it on."),
				frappe.PermissionError,
			)
		if any(self._changed(f) for f in ("allow_browser", "browser_mode", "browser_sites")):
			self._validate_sites()
		self._validate_browser_cap()

	def _validate_sites(self):
		sites, bad = user_agents.normalise_sites(self.browser_sites)
		if bad is not None:
			frappe.throw(
				_("{0} is not a website address. Write one site per line, like portal.gst.gov.in.").format(
					frappe.bold(escape_html(bad[:80]))
				)
			)
		if cint(self.allow_browser) and self.browser_mode == user_agents.BROWSER_LISTED:
			if not sites:
				frappe.throw(_("List at least one site the browser may open, one per line."))
			if len(sites) > user_agents.MAX_BROWSER_SITES:
				frappe.throw(
					_("A browser can have at most {0} sites. Remove some and save again.").format(
						user_agents.MAX_BROWSER_SITES
					)
				)
		self.browser_sites = "\n".join(sites)

	def _validate_browser_cap(self):
		if not (cint(self.enabled) and cint(self.allow_browser)):
			return
		if not (self._changed("allow_browser") or self._changed("enabled")):
			return
		others = frappe.db.count(
			self.doctype, {"enabled": 1, "allow_browser": 1, "name": ["!=", self.name or ""]}
		)
		if others >= user_agents.MAX_BROWSER_PER_SITE:
			frappe.throw(user_agents.BROWSER_LIMIT_SENTENCE)

	def _validate_schedule(self):
		from jarvis.chat.macro_scheduler import (
			validate_schedule_day_of_month_or_throw,
			validate_schedule_time_or_throw,
		)

		validate_schedule_time_or_throw(self.schedule_time)
		validate_schedule_day_of_month_or_throw(self.schedule_day_of_month)
		if cint(self.schedule_enabled) and not self.schedule_prompt:
			frappe.throw(_("Write the message the agent gets on each scheduled run."))

	def _keep_server_fields(self):
		"""Sync and run state is written by the server with ``db.set_value``; whatever
		a save carries in these fields is not its own, so the stored value wins."""
		before = self.get_doc_before_save()
		for field in (
			"agent_id",
			"sync_status",
			"sync_error",
			"sync_error_message",
			"synced_at",
			"last_run_at",
			"last_run_status",
			"last_run_error",
			"last_run_message",
			"last_run_conversation",
		):
			if before:
				self.set(field, before.get(field))
			elif field != "agent_id":
				self.set(field, "Pending" if field == "sync_status" else None)

	def _mark_pending(self):
		if not self.is_new() and any(self._changed(f) for f in SYNC_FIELDS):
			self.sync_status = "Pending"
			self.sync_error = ""
			self.sync_error_message = ""

	def _recompute_next_run(self):
		if not any(self._changed(f) for f in _SCHEDULE_FIELDS):
			return
		if not (cint(self.enabled) and cint(self.schedule_enabled)):
			self.next_run_at = None
			return
		from jarvis.chat.macro_scheduler import compute_next_run

		self.next_run_at = compute_next_run(
			self.schedule_frequency,
			self.schedule_time,
			weekday=self.schedule_weekday,
			day_of_month=self.schedule_day_of_month,
		)
