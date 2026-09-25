"""Shared fixtures for the ``Jarvis Pending Action`` suites.

Rows are server-written only (the controller refuses ORM deletes), and the code
under test commits, so cleanup is raw SQL."""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import frappe

from jarvis.chat import pending_actions as pa
from jarvis.permissions import ensure_jarvis_user_role

PA = "Jarvis Pending Action"
WAITER = "Jarvis Pending Action Waiter"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
AGENT_WRITE = "Jarvis Agent Write"

OWNER = "pa-owner@example.com"
OTHER = "pa-other@example.com"
SM_USER = "pa-sm@example.com"
DELEGATE = "pa-delegate@example.com"
USERS = (OWNER, OTHER, SM_USER, DELEGATE)
_ROLES = {SM_USER: ("Jarvis User", "System Manager")}

DEFAULT_TOOL = "add_comment"
DEFAULT_ARGS = {"reference_doctype": "ToDo", "reference_name": "pa-probe", "content": "PA-SECRET-ARG-7731"}
CARD = {"title": "Add a comment", "fields": [{"label": "Note", "value": "<b>bold</b> & co"}]}
PREVIEW = {"would": {"doctype": "Comment"}, "card": CARD}


def ensure_user(email: str, roles=("Jarvis User",)) -> str:
	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value("User", email, "enabled", 1)
	missing = [r for r in roles if r not in frappe.get_roles(email)]
	if missing:
		frappe.get_doc("User", email).add_roles(*missing)
	frappe.db.commit()
	return email


@contextlib.contextmanager
def as_user(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


@contextlib.contextmanager
def fake_dispatch(fn=None):
	"""Replace the tool body under ``api.dispatch_confirmed``; yields the call log."""
	calls: list[tuple] = []

	def _dispatch(tool, args):
		calls.append((tool, args))
		return fn(tool, args) if fn else {"name": "RESULT-1"}

	with patch("jarvis.api.dispatch", side_effect=_dispatch):
		yield calls


def draft_doctype() -> str | None:
	"""A submittable doctype a File Box run may draft (outside its denied modules)."""
	from jarvis.chat.held_writes import DENIED_MODULES

	return frappe.db.get_value(
		"DocType",
		{"is_submittable": 1, "istable": 0, "issingle": 0, "module": ["not in", sorted(DENIED_MODULES)]},
		"name",
		order_by="name asc",
	)


def wipe_rows() -> None:
	frappe.db.sql(
		f"DELETE FROM `tab{WAITER}` WHERE parent IN (SELECT name FROM `tab{PA}` WHERE owner_user IN %(u)s)",
		{"u": USERS},
	)
	frappe.db.sql(f"DELETE FROM `tab{PA}` WHERE owner_user IN %(u)s", {"u": USERS})


class PendingActionTestMixin:
	"""Users + conversations + raw-SQL teardown of every PA and waiter row."""

	def setUp(self):
		super().setUp()
		for user in USERS:
			ensure_user(user, _ROLES.get(user, ("Jarvis User",)))
		self._pa_orig_user = frappe.session.user
		self._pa_t0 = frappe.utils.now_datetime()
		self.addCleanup(self._pa_cleanup)

	def _pa_cleanup(self):
		frappe.db.rollback()
		frappe.set_user(self._pa_orig_user)
		frappe.local.jarvis_dispatch_depth = 0
		wipe_rows()
		convs = frappe.get_all(CONV, filters={"owner": ["in", USERS]}, pluck="name")
		if convs:
			frappe.db.delete(MSG, {"conversation": ["in", convs]})
			# A settled chat card's continuation queues a turn (pump mode): leaked
			# queued turns would push the shard over MAX_QUEUE_DEPTH for later suites.
			frappe.db.delete("Jarvis Chat Turn", {"conversation": ["in", convs]})
			frappe.db.delete(CONV, {"name": ["in", convs]})
		frappe.db.delete(AGENT_WRITE, {"actor": ["in", USERS]})
		frappe.db.delete("ToDo", {"description": ["like", "pa-test%"]})
		frappe.db.commit()

	def logged(self, title: str, contains: str = "") -> list[str]:
		"""Error Log bodies titled ``jarvis.pending_action.<title>`` since setUp."""
		filters = {"method": f"jarvis.pending_action.{title}", "creation": [">=", self._pa_t0]}
		if contains:
			filters["error"] = ["like", f"%{contains}%"]
		return frappe.get_all("Error Log", filters=filters, pluck="error")

	def make_conv(self, owner: str = OWNER) -> str:
		with as_user(owner):
			doc = frappe.get_doc({"doctype": CONV, "title": "pa test"}).insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def make_todo(self, owner: str = OWNER) -> str:
		with as_user(owner):
			doc = frappe.get_doc(
				{"doctype": "ToDo", "description": "pa-test target", "allocated_to": owner}
			).insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def park(
		self,
		conversation=None,
		*,
		kind="chat",
		owner=OWNER,
		exec_user=None,
		tool=DEFAULT_TOOL,
		args=None,
		**kw,
	):
		kw.setdefault("card", CARD)
		kw.setdefault("preview", PREVIEW)
		kw.setdefault("summary", "Add a comment")
		return pa.park(
			kind=kind,
			owner_user=owner,
			exec_user=exec_user or owner,
			tool=tool,
			args=dict(DEFAULT_ARGS) if args is None else args,
			conversation=conversation,
			**kw,
		)

	def row(self, name: str):
		return frappe.db.sql(f"SELECT * FROM `tab{PA}` WHERE name=%(n)s", {"n": name}, as_dict=True)[0]

	def set_col(self, name: str, **cols):
		for col, value in cols.items():
			frappe.db.sql(f"UPDATE `tab{PA}` SET `{col}`=%(v)s WHERE name=%(n)s", {"v": value, "n": name})
		frappe.db.commit()
