"""The two Phase-1 pilots: Skills and Macros on the ``filters_v2`` contract.

The point of this module is the EQUALITY-OF-SCOPE regression. ``filters_v2`` is
additive: with no clauses, both endpoints must behave exactly as they did before
the migration, for every role. Two independent proofs:

* **Structural** — the WHERE the shared query object emits for each scope branch
  is compared against the literal string the pre-migration code built.
* **Functional** — legacy-only, ``filters_v2=None``, ``filters_v2=[]`` and
  ``filters_v2="[]"`` return identical envelopes, at two roles.

Then: a user clause can only ever NARROW the fixed scope (a filter naming another
user's rows returns nothing rather than widening), legacy and v2 filters AND
together, and rows / total / has_more agree under a v2 filter.
"""

from __future__ import annotations

import contextlib
import unittest

import frappe

from jarvis.chat import list_filters
from jarvis.chat.custom_skills_api import list_custom_skills_page
from jarvis.chat.macros_api import list_macros_page

SKILL = "Jarvis Custom Skill"
MACRO = "Jarvis Macro"
SHARE = "Jarvis Custom Skill Share"

USER_A = "lfp-user-a@example.com"
USER_B = "lfp-user-b@example.com"
USER_SM = "lfp-user-sm@example.com"


def _ensure_user(email: str, system_manager: bool = False) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
		frappe.db.commit()
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
	doc = frappe.get_doc("User", email)
	roles = set(frappe.get_roles(email))
	if "Jarvis User" not in roles:
		doc.add_roles("Jarvis User")
	if system_manager and "System Manager" not in roles:
		doc.add_roles("System Manager")
	if not system_manager and "System Manager" in roles:
		doc.remove_roles("System Manager")
	frappe.db.commit()
	frappe.clear_cache(user=email)
	return email


def setUpModule() -> None:
	frappe.set_user("Administrator")
	_ensure_user(USER_A)
	_ensure_user(USER_B)
	_ensure_user(USER_SM, system_manager=True)


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _wipe() -> None:
	for name in frappe.get_all(SKILL, filters={"skill_name": ["like", "lfp-%"]}, pluck="name"):
		frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(MACRO, filters={"macro_name": ["like", "lfp-%"]}, pluck="name"):
		frappe.delete_doc(MACRO, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk_skill(owner: str, name: str, enabled=1, user_invocable=1, shared_with=None) -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": name,
				"description": f"{name} description",
				"instructions": "do the thing",
				"enabled": enabled,
				"user_invocable": user_invocable,
				"shared_with": [{"user": u} for u in (shared_with or [])],
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _mk_macro(owner: str, name: str, enabled=1, steps=None) -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": name,
				"description": "m",
				"enabled": enabled,
				"stop_on_error": 1,
				"steps": [{"label": lbl, "prompt": prm} for lbl, prm in (steps or [("s0", "p0")])],
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


# --------------------------------------------------------------------------- #
# Structural check on the ASSEMBLER (not on the pilots).
# --------------------------------------------------------------------------- #
class TestQueryObjectReproducesTheLegacyPredicates(unittest.TestCase):
	"""What this proves, precisely: :class:`ListFilterQuery`, fed the same
	server-authored predicates the pre-migration endpoints built by hand, emits
	the same WHERE — modulo the defensive parenthesisation the joiner now adds
	around every part, which is semantically inert.

	What it does NOT prove: that the pilots feed it the right predicates. That is
	:class:`TestPilotEqualityOfScope` below, which exercises the real endpoints
	and compares real result envelopes. This class would still pass if someone
	deleted a ``server_condition`` call from ``list_custom_skills_page``; that
	one would not.
	"""

	def _skills_query(self, scope, shared, search=None, legacy=None):
		q = list_filters.new_query("skills", user=USER_A)
		if scope == "mine":
			q.server_condition("owner = %(me)s", me=USER_A)
		elif scope == "shared":
			if not shared:
				q.server_condition("1=0")
			else:
				q.server_condition("(name IN %(shared)s AND enabled = 1)", shared=shared)
		else:
			if shared:
				q.server_condition(
					"(owner = %(me)s OR (name IN %(shared)s AND enabled = 1))", me=USER_A, shared=shared
				)
			else:
				q.server_condition("owner = %(me)s", me=USER_A)
		if search:
			q.server_condition("(skill_name LIKE %(q)s OR description LIKE %(q)s)", q=search)
		if legacy:
			for sql, params in legacy:
				q.server_condition(sql, **params)
		q.apply(None)
		return q

	def test_skills_scope_branches(self):
		# Right-hand side = the exact pre-migration string, wrapped by the
		# joiner's defensive parentheses (see _and(): a predicate with a
		# top-level OR must not bind loosely into the AND-chain).
		cases = [
			((None, ()), "(owner = %(me)s)"),
			((None, ("s1",)), "((owner = %(me)s OR (name IN %(shared)s AND enabled = 1)))"),
			(("mine", ("s1",)), "(owner = %(me)s)"),
			(("shared", ()), "(1=0)"),
			(("shared", ("s1",)), "((name IN %(shared)s AND enabled = 1))"),
		]
		for (scope, shared), expected in cases:
			with self.subTest(scope=scope, shared=bool(shared)):
				self.assertEqual(self._skills_query(scope, shared).where(), expected)

	def test_skills_full_legacy_chain(self):
		q = self._skills_query(
			None,
			(),
			search="%x%",
			legacy=[
				("enabled = %(enabled)s", {"enabled": 1}),
				("user_invocable = %(user_invocable)s", {"user_invocable": 0}),
			],
		)
		self.assertEqual(
			q.where(),
			"(owner = %(me)s) AND ((skill_name LIKE %(q)s OR description LIKE %(q)s)) "
			"AND (enabled = %(enabled)s) AND (user_invocable = %(user_invocable)s)",
		)
		self.assertEqual(q.params(), {"me": USER_A, "q": "%x%", "enabled": 1, "user_invocable": 0})

	def test_macros_full_legacy_chain(self):
		q = list_filters.new_query("macros", user=USER_A)
		q.server_condition("owner = %(me)s", me=USER_A)
		q.server_condition("(macro_name LIKE %(q)s OR description LIKE %(q)s)", q="%x%")
		q.server_condition("enabled = %(enabled)s", enabled=1)
		q.server_condition("schedule_enabled = %(schedule_enabled)s", schedule_enabled=0)
		q.server_condition("schedule_frequency = %(schedule_frequency)s", schedule_frequency="daily")
		q.apply([])
		self.assertEqual(
			q.where(),
			"(owner = %(me)s) AND ((macro_name LIKE %(q)s OR description LIKE %(q)s)) "
			"AND (enabled = %(enabled)s) AND (schedule_enabled = %(schedule_enabled)s) "
			"AND (schedule_frequency = %(schedule_frequency)s)",
		)


# --------------------------------------------------------------------------- #
# Functional proof: identical envelopes at two roles.
# --------------------------------------------------------------------------- #
class TestPilotEqualityOfScope(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_wipe()
		cls.a_on = _mk_skill(USER_A, "lfp-a-on", enabled=1)
		cls.a_off = _mk_skill(USER_A, "lfp-a-off", enabled=0)
		cls.b_shared = _mk_skill(USER_B, "lfp-b-shared", enabled=1, shared_with=[USER_A])
		cls.b_private = _mk_skill(USER_B, "lfp-b-private", enabled=1)
		cls.sm_own = _mk_skill(USER_SM, "lfp-sm-own", enabled=1)
		cls.ma = _mk_macro(USER_A, "lfp-macro-a", enabled=1, steps=[("build", "do it")])
		cls.ma2 = _mk_macro(USER_A, "lfp-macro-a2", enabled=0)
		cls.mb = _mk_macro(USER_B, "lfp-macro-b", enabled=1)
		cls.msm = _mk_macro(USER_SM, "lfp-macro-sm", enabled=1)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe()

	def _skills(self, user, **kw):
		kw.setdefault("page_length", 100)
		with _as(user):
			return list_custom_skills_page(**kw)

	def _macros(self, user, **kw):
		kw.setdefault("page_length", 100)
		with _as(user):
			return list_macros_page(**kw)

	def test_skills_empty_filters_v2_is_identical_to_legacy_at_both_roles(self):
		for user in (USER_A, USER_SM):
			with self.subTest(user=user):
				baseline = self._skills(user)
				for empty in (None, [], "[]", ""):
					self.assertEqual(
						self._skills(user, filters_v2=empty),
						baseline,
						f"filters_v2={empty!r} changed the result envelope",
					)

	def test_macros_empty_filters_v2_is_identical_to_legacy_at_both_roles(self):
		for user in (USER_A, USER_SM):
			with self.subTest(user=user):
				baseline = self._macros(user)
				for empty in (None, [], "[]", ""):
					self.assertEqual(self._macros(user, filters_v2=empty), baseline)

	def test_skills_scope_is_still_own_plus_shared_enabled(self):
		names = {r["skill_name"] for r in self._skills(USER_A)["rows"]}
		self.assertEqual(names, {"lfp-a-on", "lfp-a-off", "lfp-b-shared"})

	def test_legacy_filters_still_work_unchanged(self):
		res = self._skills(USER_A, filters={"enabled": 1})
		self.assertEqual({r["skill_name"] for r in res["rows"]}, {"lfp-a-on", "lfp-b-shared"})
		res = self._skills(USER_A, filters={"scope": "mine"})
		self.assertEqual({r["skill_name"] for r in res["rows"]}, {"lfp-a-on", "lfp-a-off"})

	def test_filters_v2_narrows_within_scope(self):
		res = self._skills(
			USER_A, filters_v2=[{"doctype": SKILL, "fieldname": "enabled", "operator": "=", "value": 0}]
		)
		self.assertEqual({r["skill_name"] for r in res["rows"]}, {"lfp-a-off"})
		self.assertEqual(res["total"], 1)
		self.assertFalse(res["has_more"])

	def test_filters_v2_can_never_widen_the_fixed_scope(self):
		"""Filtering ON another user's identity returns nothing — it does not
		reach outside the server-authored predicate."""
		res = self._macros(
			USER_A, filters_v2=[{"doctype": MACRO, "fieldname": "owner", "operator": "=", "value": USER_B}]
		)
		self.assertEqual(res["rows"], [])
		self.assertEqual(res["total"], 0)

		res = self._macros(
			USER_A,
			filters_v2=[
				{"doctype": MACRO, "fieldname": "macro_name", "operator": "like", "value": "lfp-macro"}
			],
		)
		self.assertEqual({r["macro_name"] for r in res["rows"]}, {"lfp-macro-a", "lfp-macro-a2"})

	def test_system_manager_gets_no_extra_rows_from_the_v2_path(self):
		"""These lists are owner-scoped for everyone, System Manager included;
		the migration must not have loosened that."""
		res = self._macros(
			USER_SM,
			filters_v2=[
				{"doctype": MACRO, "fieldname": "macro_name", "operator": "like", "value": "lfp-macro"}
			],
		)
		self.assertEqual({r["macro_name"] for r in res["rows"]}, {"lfp-macro-sm"})

	def test_legacy_and_v2_filters_and_together(self):
		res = self._skills(
			USER_A,
			filters={"scope": "mine"},
			filters_v2=[{"doctype": SKILL, "fieldname": "enabled", "operator": "=", "value": 1}],
		)
		self.assertEqual({r["skill_name"] for r in res["rows"]}, {"lfp-a-on"})

	def test_rows_total_and_has_more_share_the_compiled_where(self):
		"""total counts the FILTERED set (not the scope), and has_more is derived
		from the same WHERE the row page used."""
		clause = [{"doctype": MACRO, "fieldname": "macro_name", "operator": "like", "value": "lfp-macro"}]
		page = self._macros(USER_A, filters_v2=clause, page_length=1)
		self.assertEqual(page["total"], 2, "the count query must use the compiled WHERE too")
		self.assertEqual(len(page["rows"]), 1)
		self.assertTrue(page["has_more"])

		rest = self._macros(USER_A, filters_v2=clause, page_length=1, start=1)
		self.assertEqual(len(rest["rows"]), 1)
		self.assertFalse(rest["has_more"])
		self.assertNotEqual(rest["rows"][0]["name"], page["rows"][0]["name"])

		narrowed = self._macros(
			USER_A,
			filters_v2=clause + [{"doctype": MACRO, "fieldname": "enabled", "operator": "=", "value": 1}],
		)
		self.assertEqual(narrowed["total"], 1)
		self.assertEqual({r["macro_name"] for r in narrowed["rows"]}, {"lfp-macro-a"})

	def test_child_filter_through_the_macros_endpoint(self):
		res = self._macros(
			USER_A,
			filters_v2=[
				{"doctype": "Jarvis Macro Step", "fieldname": "label", "operator": "=", "value": "build"}
			],
		)
		self.assertEqual({r["macro_name"] for r in res["rows"]}, {"lfp-macro-a"})
		self.assertEqual(res["total"], 1)

	def test_a_rejected_filter_is_an_envelope_not_an_empty_list(self):
		"""The failure mode a filtered list must never have: the SPA's list
		composable reads ``res.rows`` and falls back to ``[]``, so a rejection
		returned as a bare 200 would render as 'no results' — a wrong answer that
		looks like a right one."""
		res = self._macros(
			USER_A,
			filters_v2=[{"doctype": MACRO, "fieldname": "no_such_field", "operator": "=", "value": "x"}],
		)
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["code"], "list_filter_unknown_field")
		self.assertTrue(res["error"]["message"])
		self.assertNotIn("rows", res)

	def test_schema_endpoint_is_jarvis_user_gated_and_view_scoped(self):
		from jarvis.chat.list_filters import get_list_filter_schema

		with _as(USER_A):
			schema = get_list_filter_schema("skills")
		self.assertEqual(schema["root_doctype"], SKILL)
		self.assertEqual(schema["contract_version"], list_filters.CONTRACT_VERSION)
		self.assertTrue(schema["schema_revision"])
		self.assertEqual(schema["limits"]["max_clauses"], list_filters.MAX_CLAUSES)


# --------------------------------------------------------------------------- #
# U1-1: the stable codes reach THE WIRE.
# --------------------------------------------------------------------------- #
class TestErrorCodesOnTheWire(unittest.TestCase):
	"""Asserts on the serialized HTTP response — status line and JSON body — not
	on the Python object the endpoint returned.

	A code that only exists as an attribute on an exception inside the worker is
	not a contract. This drives the request through Frappe's real dispatch
	(``execute_cmd``) and its real serializer (``build_response('json')``), which
	is what a browser would receive.
	"""

	@staticmethod
	def _request(cmd: str, **params) -> tuple[int, dict]:
		import json as _json

		from frappe.handler import execute_cmd
		from frappe.utils.response import build_response
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		saved = (
			getattr(frappe.local, "response", None),
			getattr(frappe.local, "form_dict", None),
			getattr(frappe.local, "request", None),
			getattr(frappe.local, "message_log", None),
		)
		try:
			# A REAL request object on the real /api/method path: execute_cmd
			# checks the verb against the whitelist's allowed set, and the JSON
			# serializer reads the path to pick its response version.
			frappe.local.request = Request(
				EnvironBuilder(path=f"/api/method/{cmd}", method="GET").get_environ()
			)
			frappe.local.response = frappe._dict({"type": "json"})
			frappe.local.form_dict = frappe._dict({"cmd": cmd, **params})
			frappe.local.message_log = []
			data = execute_cmd(cmd)
			if data is not None:
				frappe.local.response["message"] = data
			response = build_response("json")
			return response.status_code, _json.loads(response.get_data(as_text=True))
		finally:
			frappe.local.response, frappe.local.form_dict, frappe.local.request, frappe.local.message_log = (
				saved
			)

	def test_unknown_field_serializes_as_a_coded_4xx(self):
		with _as(USER_A):
			status, body = self._request(
				"jarvis.chat.macros_api.list_macros_page",
				filters_v2='[{"doctype": "Jarvis Macro", "fieldname": "nope", "operator": "=", "value": "x"}]',
			)
		self.assertEqual(status, 417)
		self.assertEqual(body["message"]["ok"], False)
		self.assertEqual(body["message"]["error"]["code"], "list_filter_unknown_field")
		# The human-readable half rides along too, in Frappe's own channel, so an
		# existing client that only knows _server_messages still shows something.
		self.assertTrue(body.get("_server_messages"))

	def test_malformed_payload_serializes_as_400(self):
		with _as(USER_A):
			status, body = self._request(
				"jarvis.chat.custom_skills_api.list_custom_skills_page", filters_v2="{not json"
			)
		self.assertEqual(status, 400)
		self.assertEqual(body["message"]["error"]["code"], "list_filter_bad_payload")

	def test_schema_endpoint_serializes_its_code_too(self):
		with _as(USER_A):
			status, body = self._request(
				"jarvis.chat.list_filters.get_list_filter_schema", view_key="not-a-view"
			)
		self.assertEqual(status, 417)
		self.assertEqual(body["message"]["error"]["code"], "list_filter_unknown_view")

	def test_a_successful_call_keeps_status_200_and_the_bare_envelope(self):
		"""Non-vacuity: the boundary must not have turned every response into an
		envelope — the success shape is frozen and the SPA reads it directly."""
		with _as(USER_A):
			status, body = self._request("jarvis.chat.macros_api.list_macros_page")
		self.assertEqual(status, 200)
		self.assertIn("rows", body["message"])
		self.assertNotIn("ok", body["message"])

	def test_a_legacy_filter_error_is_left_alone(self):
		"""The boundary catches ListFilterError only. The legacy ``filters``
		contract still throws, which is what its existing callers expect."""
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			self._request("jarvis.chat.macros_api.list_macros_page", filters='{"bogus_key": 1}')
