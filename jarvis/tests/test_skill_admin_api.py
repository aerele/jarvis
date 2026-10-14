"""A Jarvis Admin's view of every user's skills (``jarvis.chat.skills_admin_api``).

What is pinned here:

* each endpoint is for a Jarvis Admin only (the role, a System Manager or
  Administrator), by POST only, never from inside a tool call, a job one queued, or
  the ``run_method`` tool, and refuses a name that is not text;
* the actor by kind matrix of gate outcomes, for reading one skill and holding it;
* the list carries every non-learned skill with no instructions text, labels a
  published copy, and filters, searches, sorts and pages;
* the detail carries the instructions, shares, roles, lineage, history and how many
  macros use the skill;
* the five argument helpers copied from ``macros_admin_api`` behave as the originals;
* ``catalog_update_pending`` and the list's ``sync``;
* a site before the migrate: nothing is held and holding says it is not available.

The acts themselves (what a hold, a release and a delete do) are in
``test_skill_admin_hold``. The endpoints commit, so fixtures are committed and
purged around each test, as the macro admin tests do.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.handler import is_valid_http_method
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request

from jarvis.chat import macros_admin_api, skills_admin, skills_admin_api
from jarvis.exceptions import PermissionDeniedError
from jarvis.jarvis.doctype.jarvis_custom_skill import jarvis_custom_skill as skill_doctype
from jarvis.permissions import ensure_jarvis_admin_role
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tools import registry
from jarvis.tools.run_method import _is_denied, run_method

SKILL = "Jarvis Custom Skill"
PROMO = "Jarvis Skill Promotion Request"
LOG = "Jarvis Skill Admin Log"
MACRO = "Jarvis Macro"
SETTINGS = "Jarvis Settings"

# Every skill these tests make carries this in its name, so a list call can be
# narrowed to them and the purge finds them: the test site is shared.
TAG = "skadm"
OWNER = "skadm-owner@example.com"
OTHER = "skadm-other@example.com"
PLAIN = "skadm-plain@example.com"
REVIEWER = "skadm-reviewer@example.com"
ADMIN = "skadm-admin@example.com"
# A System Manager with no Jarvis role: "Jarvis Admin" in code is the role OR System
# Manager OR Administrator (``jarvis/permissions.py``).
SYSMGR = "skadm-sysmgr@example.com"
USERS = (OWNER, OTHER, PLAIN, REVIEWER, ADMIN, SYSMGR)
# A role every test user holds, for Role skills.
AUDIENCE = "Jarvis User"

PATH = "jarvis.chat.skills_admin_api."
ENDPOINTS = (
	"admin_list_skills",
	"admin_skill_owners",
	"admin_get_skill",
	"admin_hold",
	"admin_release",
	"admin_delete",
)
WRITES = ("admin_hold", "admin_release", "admin_delete")
ROW_KEYS = {
	"name",
	"skill_name",
	"description",
	"owner",
	"owner_name",
	"scope",
	"target_role",
	"enabled",
	"allow_approve_run",
	"admin_hold",
	"admin_hold_reason",
	"shared_with_count",
	"modified",
	"is_mine",
	"is_published",
	"is_learned",
}
_MISSING = object()


class SkillAdminBase(FrappeTestCase):
	"""Users, skill fixtures of every kind, and a committing purge."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_jarvis_admin_role()
		if not frappe.db.exists("Role", "Jarvis Skill Reviewer"):
			frappe.get_doc(
				{"doctype": "Role", "role_name": "Jarvis Skill Reviewer", "desk_access": 1}
			).insert(ignore_permissions=True)
		for user in (OWNER, OTHER, PLAIN):
			ensure_user(user)
		ensure_user(REVIEWER, roles=("Jarvis User", "Jarvis Skill Reviewer"))
		ensure_user(ADMIN, roles=("Jarvis User", "Jarvis Admin"))
		ensure_user(SYSMGR, roles=("System Manager",))
		for user, extra in ((SYSMGR, ("Jarvis User", "Jarvis Admin")), (ADMIN, ("System Manager",))):
			for role in extra:
				if role in frappe.get_roles(user):
					frappe.get_doc("User", user).remove_roles(role)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._purge()
		self.addCleanup(self._purge)

	def _purge(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		names = frappe.get_all(SKILL, filters={"skill_name": ["like", f"%{TAG}%"]}, pluck="name")
		if names:
			frappe.db.delete("Comment", {"reference_doctype": SKILL, "reference_name": ["in", names]})
		frappe.db.delete(PROMO, {"skill_name": ["like", f"%{TAG}%"]})
		for name in names:
			frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
		frappe.db.delete("Jarvis Shared Skill Slug", {"slug": ["like", f"%{TAG}%"]})
		if frappe.db.table_exists(LOG):
			frappe.db.delete(LOG, {"skill_name": ["like", f"%{TAG}%"]})
		for kind in skills_admin.NOTICE_KINDS:
			frappe.db.delete("Notification Log", {"subject": ["like", f"{kind}: %{TAG}%"]})
		for name in frappe.get_all(MACRO, filters={"macro_name": ["like", f"{TAG}%"]}, pluck="name"):
			frappe.delete_doc(MACRO, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	# ------------------------------------------------------------------ fixtures
	def _skill(self, owner, tag, *, scope="User", enabled=1, **fields):
		"""A skill owned by ``owner``, committed. Inserted as Administrator (a reviewer,
		so a Role or company skill is allowed), then its owner written raw as the
		promotion does it (an insert always owns the row as the session user). The
		promotion's flag also skips the per-owner cap, which Administrator's rows on a
		shared test site could reach. Fields the form would refuse (the arm, the learned
		flag, a hold) are written raw too: these tests are not about the save."""
		frappe.set_user("Administrator")
		raw = {k: fields.pop(k) for k in list(fields) if k in _RAW_FIELDS}
		prev = frappe.flags.jarvis_promotion_materialize
		frappe.flags.jarvis_promotion_materialize = True
		try:
			doc = frappe.get_doc(
				{
					"doctype": SKILL,
					"skill_name": f"{TAG}-{tag}",
					"description": f"{tag} description",
					"instructions": f"{tag} instructions",
					"enabled": enabled,
					"scope": scope,
					"target_role": AUDIENCE if scope == "Role" else None,
					**fields,
				}
			).insert(ignore_permissions=True)
		finally:
			frappe.flags.jarvis_promotion_materialize = prev
		frappe.db.set_value(SKILL, doc.name, {"owner": owner, **raw}, update_modified=False)
		frappe.db.commit()
		return doc.name

	def _kinds(self) -> dict:
		"""One skill of each kind the matrix covers, keyed by kind. Each is someone else's
		than the admins'; the published copy's lineage author is OWNER."""
		private = self._skill(OWNER, "private")
		return {
			"private": private,
			"shared_with": self._skill(OWNER, "shared", shared_with=[{"user": OTHER}]),
			"role": self._skill(OWNER, "role", scope="Role"),
			"org": self._skill(OWNER, "org", scope="Org"),
			"learned": self._skill("Administrator", "learned", managed_by_learning=1),
			"published": self._skill("Administrator", "published", scope="Role", source_skill=private),
			"held": self._skill(OWNER, "held", enabled=0, admin_hold=1, admin_hold_reason="Earlier hold"),
		}

	def _row(self, name, *fields):
		return frappe.db.get_value(SKILL, name, list(fields) or ["enabled"], as_dict=True)

	def _list(self, user=ADMIN, **kwargs):
		frappe.set_user(user)
		kwargs.setdefault("search", TAG)
		return skills_admin_api.admin_list_skills(**kwargs)

	@staticmethod
	def _names(res):
		return [r["name"] for r in res["rows"]]

	def _notes(self, user):
		return frappe.get_all(
			"Notification Log",
			filters={"for_user": user, "subject": ["like", f"%{TAG}%"]},
			fields=["name", "subject", "email_content", "document_type", "document_name", "read"],
		)

	def _log(self, skill):
		return frappe.get_all(
			LOG,
			filters={"skill": skill},
			fields=["verb", "admin", "skill_name", "skill_owner", "reason", "needs_apply"],
			order_by="creation asc",
		)


# A hold, the arm and the learned flag are what a fixture writes raw.
_RAW_FIELDS = {"admin_hold", "admin_hold_reason", "allow_approve_run", "managed_by_learning"}


def _raised_in_a_tool(fn, **kwargs):
	"""Call ``fn`` as the body of a registered tool: the real ``registry.dispatch``
	holds the depth counter as it does for a model's tool call."""
	probe = "zz_skill_admin_probe"

	def body():
		try:
			fn(**kwargs)
		except Exception as e:
			return e
		return None

	with (
		patch.dict(registry._TOOLS, {probe: body}),
		patch.dict(registry._ACCEPTS_VAR_KW, {probe: False}),
		patch.dict(registry._ACCEPTED_PARAMS, {probe: set()}),
	):
		return registry.dispatch(probe, {})


# --------------------------------------------------------------------------- #
# Who may call them, and how
# --------------------------------------------------------------------------- #
class TestTheGates(SkillAdminBase):
	def _calls(self, skill):
		return {
			"admin_list_skills": {},
			"admin_skill_owners": {},
			"admin_get_skill": {"name": skill},
			"admin_hold": {"skill": skill, "reason": "Paused for review"},
			"admin_release": {"skill": skill},
			"admin_delete": {"skill": skill},
		}

	def test_every_endpoint_is_post_only(self):
		prev = getattr(frappe.local, "request", _MISSING)
		self.addCleanup(self._restore_request, prev)
		for endpoint in ENDPOINTS:
			with self.subTest(endpoint=endpoint):
				fn = getattr(skills_admin_api, endpoint)
				self.assertIn(fn, frappe.whitelisted)
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])
				set_request(method="GET", path=f"/api/method/{PATH}{endpoint}")
				with self.assertRaises(frappe.PermissionError):
					is_valid_http_method(fn)
				set_request(method="POST", path=f"/api/method/{PATH}{endpoint}")
				is_valid_http_method(fn)

	@staticmethod
	def _restore_request(prev):
		if prev is _MISSING:
			if hasattr(frappe.local, "request"):
				del frappe.local.request
		else:
			frappe.local.request = prev

	def test_no_module_function_is_whitelisted_without_being_listed_here(self):
		found = {
			name
			for name, fn in vars(skills_admin_api).items()
			if callable(fn)
			and getattr(fn, "__module__", "") == skills_admin_api.__name__
			and fn in frappe.whitelisted
		}
		self.assertEqual(found, set(ENDPOINTS))

	def test_refused_inside_a_tool_call(self):
		skill = self._skill(OWNER, "tool")
		frappe.set_user(ADMIN)
		for endpoint, kwargs in self._calls(skill).items():
			with self.subTest(endpoint=endpoint):
				err = _raised_in_a_tool(getattr(skills_admin_api, endpoint), **kwargs)
				self.assertIsInstance(err, frappe.PermissionError, f"{endpoint} ran inside a tool")
				self.assertIn("inside a tool", str(err))
		self.assertEqual(self._row(skill, "enabled").enabled, 1)

	def test_writes_are_refused_from_a_job_a_tool_call_queued(self):
		skill = self._skill(OWNER, "guarded")
		frappe.set_user(ADMIN)
		calls = self._calls(skill)
		with patch("jarvis.tools._write_risk.in_guarded_call", return_value=True):
			for endpoint in WRITES:
				with self.subTest(endpoint=endpoint):
					with self.assertRaises(frappe.PermissionError):
						getattr(skills_admin_api, endpoint)(**calls[endpoint])
		self.assertTrue(frappe.db.exists(SKILL, skill))
		self.assertEqual(self._row(skill, "enabled").enabled, 1)

	def test_refused_through_the_run_method_tool(self):
		skill = self._skill(OWNER, "runmethod")
		frappe.set_user(ADMIN)
		for endpoint, kwargs in self._calls(skill).items():
			with self.subTest(endpoint=endpoint):
				self.assertTrue(_is_denied(getattr(skills_admin_api, endpoint)))
				with patch("frappe.call") as call:
					with self.assertRaises(PermissionDeniedError):
						run_method(PATH + endpoint, kwargs)
				call.assert_not_called()
		with self.assertRaises(PermissionDeniedError):
			run_method(PATH + "admin_delete", {"skill": skill})
		self.assertTrue(frappe.db.exists(SKILL, skill))

	def test_a_name_that_is_not_text_is_refused(self):
		# A dict reaches the database layer as a FILTER and would match some other row.
		skill = self._skill(OWNER, "badname")
		frappe.set_user(ADMIN)
		matches_the_skill = {"owner": OWNER}
		for endpoint, arg in (
			("admin_get_skill", "name"),
			("admin_hold", "skill"),
			("admin_release", "skill"),
			("admin_delete", "skill"),
		):
			for bad in (None, "", "  ", ["x"], matches_the_skill, 7):
				with self.subTest(endpoint=endpoint, value=bad):
					kwargs = {arg: bad}
					if endpoint == "admin_hold":
						kwargs["reason"] = "x"
					with self.assertRaises(frappe.ValidationError):
						getattr(skills_admin_api, endpoint)(**kwargs)
		self.assertTrue(frappe.db.exists(SKILL, skill))
		self.assertEqual(self._row(skill, "enabled").enabled, 1)

	def test_actor_by_kind_matrix(self):
		"""Reading one skill and holding it, for each actor and each kind. Only an admin
		passes; an admin is refused on a learned row; a published copy is the lineage
		author's (OWNER), so Administrator may act on it."""
		kinds = self._kinds()
		actors = (
			("jarvis_user", PLAIN, False),
			("skill_reviewer", REVIEWER, False),
			("owner", OWNER, False),
			("jarvis_admin", ADMIN, True),
			("system_manager", SYSMGR, True),
			("system_identity", "Administrator", True),
		)
		for actor, user, is_admin in actors:
			for kind, skill in kinds.items():
				with self.subTest(actor=actor, kind=kind):
					frappe.set_user(user)
					if not is_admin:
						with self.assertRaises(frappe.PermissionError):
							skills_admin_api.admin_get_skill(skill)
						with self.assertRaises(frappe.PermissionError):
							skills_admin_api.admin_hold(skill, "Matrix")
						continue
					self.assertEqual(skills_admin_api.admin_get_skill(skill)["name"], skill)
					if kind == "learned":
						with self.assertRaises(frappe.ValidationError):
							skills_admin_api.admin_hold(skill, "Matrix")
						self.assertFalse(frappe.utils.cint(self._row(skill, "admin_hold").admin_hold))
						continue
					out = skills_admin_api.admin_hold(skill, "Matrix")
					self.assertTrue(out["ok"])
					self.assertEqual(self._row(skill, "admin_hold").admin_hold, 1)

	def test_an_admins_own_skill_is_refused(self):
		own = self._skill(ADMIN, "mine")
		frappe.set_user(ADMIN)
		for endpoint, kwargs in self._calls(own).items():
			if endpoint not in WRITES:
				continue
			with self.subTest(endpoint=endpoint):
				with self.assertRaises(frappe.ValidationError) as ctx:
					getattr(skills_admin_api, endpoint)(**kwargs)
				self.assertIn("your own skill", str(ctx.exception))
		self.assertTrue(frappe.db.exists(SKILL, own))

	def test_a_published_copy_is_its_lineage_authors(self):
		source = self._skill(ADMIN, "lineage-src")
		copy = self._skill("Administrator", "lineage-copy", scope="Role", source_skill=source)
		frappe.set_user(ADMIN)
		with self.assertRaises(frappe.ValidationError):
			skills_admin_api.admin_hold(copy, "Mine by lineage")
		frappe.set_user("Administrator")
		self.assertTrue(skills_admin_api.admin_hold(copy, "Administrator may")["ok"])
		# A skill Administrator made itself (no lineage) is Administrator's own.
		desk_made = self._skill("Administrator", "desk-made", scope="Role")
		with self.assertRaises(frappe.ValidationError):
			skills_admin_api.admin_hold(desk_made, "Own")


# --------------------------------------------------------------------------- #
# The list and the owners
# --------------------------------------------------------------------------- #
class TestTheList(SkillAdminBase):
	def test_rows_carry_the_contract_and_no_instructions(self):
		kinds = self._kinds()
		res = self._list()
		self.assertEqual(
			set(res), {"rows", "page", "page_size", "has_more", "total", "hold_available", "sync"}
		)
		self.assertTrue(res["hold_available"])
		self.assertEqual(set(res["sync"]), {"pending", "needs_apply", "last_synced_at"})
		by_name = {r["name"]: r for r in res["rows"]}
		for row in res["rows"]:
			self.assertEqual(set(row), ROW_KEYS)
			self.assertNotIn("instructions", row)
			self.assertNotIn("instructions", json.dumps(row))
		self.assertNotIn(kinds["learned"], by_name)  # the learning board's, not this list's
		published = by_name[kinds["published"]]
		self.assertEqual(published["is_published"], 1)
		self.assertEqual(published["owner"], "Administrator")
		self.assertEqual(by_name[kinds["private"]]["is_published"], 0)
		self.assertEqual(by_name[kinds["shared_with"]]["shared_with_count"], 1)
		self.assertEqual(by_name[kinds["role"]]["scope"], "Role")
		self.assertEqual(by_name[kinds["role"]]["target_role"], AUDIENCE)
		self.assertEqual(by_name[kinds["org"]]["scope"], "Org")
		held = by_name[kinds["held"]]
		self.assertEqual((held["admin_hold"], held["admin_hold_reason"]), (1, "Earlier hold"))
		self.assertEqual(by_name[kinds["private"]]["owner_name"], "skadm-owner")

	def test_is_mine_follows_the_lineage_author(self):
		own = self._skill(ADMIN, "own-row")
		copy = self._skill("Administrator", "own-copy", scope="Role", source_skill=own)
		other = self._skill(OWNER, "not-mine")
		rows = {r["name"]: r for r in self._list()["rows"]}
		self.assertEqual(rows[own]["is_mine"], 1)
		self.assertEqual(rows[copy]["is_mine"], 1)
		self.assertEqual(rows[other]["is_mine"], 0)

	def test_filters(self):
		kinds = self._kinds()
		armed = self._skill(OTHER, "armed", allow_approve_run=1)
		off = self._skill(OTHER, "off", enabled=0)
		cases = (
			({"owner": OTHER}, {armed, off}),
			({"scope": "Role"}, {kinds["role"], kinds["published"]}),
			({"scope": "Org"}, {kinds["org"]}),
			({"armed": 1}, {armed}),
			({"on_hold": 1}, {kinds["held"]}),
			({"enabled": 0, "owner": OTHER}, {off}),
		)
		for filters, expected in cases:
			with self.subTest(filters=filters):
				got = set(self._names(self._list(filters=json.dumps(filters))))
				self.assertEqual(got, expected)
		self.assertNotIn(kinds["held"], self._names(self._list(filters={"on_hold": 0})))

	def test_search_matches_the_owners_full_name(self):
		frappe.db.set_value("User", OTHER, "full_name", "Ulrike Qwertz", update_modified=False)
		frappe.db.commit()
		self.addCleanup(self._reset_full_name)
		mine = self._skill(OTHER, "searched")
		self._skill(OWNER, "not-searched")
		self.assertEqual(self._names(self._list(search="qwertz")), [mine])

	def _reset_full_name(self):
		frappe.db.set_value("User", OTHER, "full_name", "skadm-other", update_modified=False)
		frappe.db.commit()

	def test_sort_and_pages(self):
		names = [self._skill(OWNER, f"page-{i}") for i in range(3)]
		res = self._list(search=f"{TAG}-page", sort="skill_name desc", page=1, page_size=2)
		self.assertEqual(self._names(res), [names[2], names[1]])
		self.assertTrue(res["has_more"])
		self.assertEqual(res["total"], 3)
		res = self._list(search=f"{TAG}-page", sort="skill_name desc", page=2, page_size=2)
		self.assertEqual(self._names(res), [names[0]])
		self.assertFalse(res["has_more"])

	def test_bad_arguments_are_refused(self):
		for kwargs in (
			{"sort": "instructions"},
			{"sort": "skill_name sideways"},
			{"filters": ["owner"]},
			{"filters": "not json"},
			{"filters": {"unknown": 1}},
			{"filters": {"scope": "Everyone"}},
			{"search": ["x"]},
		):
			with self.subTest(kwargs=kwargs):
				with self.assertRaises(frappe.ValidationError):
					self._list(**kwargs)

	def test_the_owner_choices(self):
		self._kinds()
		self._skill(OTHER, "owners")
		frappe.set_user(ADMIN)
		res = skills_admin_api.admin_skill_owners(search=TAG)
		self.assertEqual(set(res), {"owners", "more"})
		users = {o["user"]: o["full_name"] for o in res["owners"]}
		self.assertEqual(users.get(OWNER), "skadm-owner")
		self.assertIn(OTHER, users)


# --------------------------------------------------------------------------- #
# One skill, read-only
# --------------------------------------------------------------------------- #
class TestTheDetail(SkillAdminBase):
	def test_the_detail_payload(self):
		kinds = self._kinds()
		frappe.set_user(ADMIN)
		out = skills_admin_api.admin_get_skill(kinds["shared_with"])
		self.assertTrue(
			ROW_KEYS
			| {"instructions", "shared_with", "allowed_roles", "lineage", "history", "used_by_macros"}
			<= set(out)
		)
		self.assertEqual(out["instructions"], "shared instructions")
		self.assertEqual(out["shared_with"], [{"user": OTHER, "full_name": "skadm-other"}])
		self.assertIsNone(out["lineage"])
		self.assertEqual(out["used_by_macros"], {"total": 0, "armed": 0})
		self.assertEqual(skills_admin_api.admin_get_skill(kinds["role"])["allowed_roles"], [AUDIENCE])
		lineage = skills_admin_api.admin_get_skill(kinds["published"])["lineage"]
		self.assertEqual(
			lineage, {"source_skill": kinds["private"], "author": OWNER, "author_name": "skadm-owner"}
		)
		self.assertEqual(skills_admin_api.admin_get_skill(kinds["learned"])["is_learned"], 1)

	def test_history_comes_from_the_audit_log(self):
		skill = self._skill(OWNER, "history", scope="Role")
		frappe.set_user(ADMIN)
		skills_admin_api.admin_hold(skill, "First reason")
		frappe.set_user(SYSMGR)
		skills_admin_api.admin_release(skill)
		history = skills_admin_api.admin_get_skill(skill)["history"]
		self.assertEqual([(h["verb"], h["admin"]) for h in history], [("release", SYSMGR), ("hold", ADMIN)])
		self.assertEqual(history[1]["reason"], "First reason")
		self.assertEqual(history[1]["admin_name"], "skadm-admin")
		self.assertTrue(history[0]["at"])

	def test_used_by_macros_counts_macros_and_armed_ones(self):
		skill = self._skill(OWNER, "macros")
		for i, armed in enumerate((0, 1)):
			frappe.set_user(OWNER)
			macro = frappe.get_doc(
				{
					"doctype": MACRO,
					"macro_name": f"{TAG}-macro-{i}",
					"steps": [{"prompt": "go", "skills": json.dumps([skill])}],
				}
			).insert(ignore_permissions=True)
			frappe.db.set_value(MACRO, macro.name, "skip_confirmation", armed, update_modified=False)
		frappe.db.commit()
		frappe.set_user(ADMIN)
		self.assertEqual(skills_admin_api.admin_get_skill(skill)["used_by_macros"], {"total": 2, "armed": 1})
		self.assertEqual(
			skills_admin_api.admin_hold(skill, "Used")["used_by_macros"], {"total": 2, "armed": 1}
		)

	def test_a_deleted_skill_says_so(self):
		frappe.set_user(ADMIN)
		with self.assertRaises(frappe.DoesNotExistError):
			skills_admin_api.admin_get_skill("zz-no-such-skill")


# --------------------------------------------------------------------------- #
# The copied helpers
# --------------------------------------------------------------------------- #
class TestHelperParity(FrappeTestCase):
	"""The five helpers copied from ``macros_admin_api`` (see the module docstring)
	answer every input as the originals do: the same value, or the same exception
	class. Messages differ where they name a macro."""

	def _outcome(self, fn, *args):
		try:
			return ("value", fn(*args))
		except Exception as e:
			frappe.clear_last_message()
			return ("raised", type(e))

	def _assert_same(self, name, inputs):
		mine, theirs = getattr(skills_admin_api, name), getattr(macros_admin_api, name)
		for args in inputs:
			with self.subTest(helper=name, args=args):
				self.assertEqual(self._outcome(mine, *args), self._outcome(theirs, *args))

	def test_name_and_text(self):
		values = (None, "", "  ", "x", "  x  ", ["x"], {"owner": "a"}, 7, 0, True)
		self._assert_same("_name_arg", [(v, "What") for v in values])
		self._assert_same("_text_arg", [(v, "What") for v in values])

	def test_filters(self):
		self._assert_same(
			"_filters_arg",
			[
				(None,),
				("",),
				("  ",),
				('{"owner": "a"}',),
				({"owner": "a"},),
				("[1, 2]",),
				("not json",),
				(["owner"],),
				(7,),
			],
		)

	def test_full_names(self):
		self._assert_same(
			"_full_names",
			[([],), (["Administrator"],), (["Administrator", "Guest", None, ""],), (["zz@none"],)],
		)

	def test_reason(self):
		self._assert_same(
			"_reason_arg",
			[(None,), ("",), ("  ",), ("why",), ("  why  ",), ("x" * 500,), ("x" * 501,), (5,), (["why"],)],
		)


# --------------------------------------------------------------------------- #
# The catalog update the next Apply carries (D1)
# --------------------------------------------------------------------------- #
class TestCatalogUpdatePending(SkillAdminBase):
	def setUp(self):
		super().setUp()
		prev = frappe.db.get_single_value(SETTINGS, "custom_skills_synced_at")
		self.addCleanup(self._restore_synced_at, prev)

	@staticmethod
	def _restore_synced_at(prev):
		frappe.db.set_single_value(SETTINGS, "custom_skills_synced_at", prev, update_modified=False)
		frappe.db.commit()

	def _synced(self, when):
		frappe.db.set_single_value(SETTINGS, "custom_skills_synced_at", when, update_modified=False)
		frappe.db.commit()

	def test_pending_after_an_act_on_a_company_skill_until_the_next_apply(self):
		self._synced("2000-01-01 00:00:00")
		private = self._skill(OWNER, "cat-private")
		org = self._skill(OWNER, "cat-org", scope="Org")
		frappe.set_user(ADMIN)
		self.assertFalse(skills_admin_api.admin_hold(private, "Private")["needs_apply"])
		self.assertFalse(skills_admin.catalog_update_pending())
		self.assertTrue(skills_admin_api.admin_hold(org, "Company")["needs_apply"])
		self.assertTrue(skills_admin.catalog_update_pending())
		self.assertTrue(self._list()["sync"]["needs_apply"])
		self._synced(frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=1))
		self.assertFalse(skills_admin.catalog_update_pending())
		self.assertFalse(self._list()["sync"]["needs_apply"])


# --------------------------------------------------------------------------- #
# A site before the migrate
# --------------------------------------------------------------------------- #
class TestBeforeTheMigrate(SkillAdminBase):
	def _unmigrated(self):
		return patch.object(skill_doctype, "hold_fields_exist", return_value=False)

	def test_nothing_is_held_and_holding_is_not_available(self):
		skill = self._skill(OWNER, "unmigrated")
		with self._unmigrated():
			res = self._list()
			self.assertFalse(res["hold_available"])
			row = next(r for r in res["rows"] if r["name"] == skill)
			self.assertEqual((row["admin_hold"], row["admin_hold_reason"]), (0, ""))
			self.assertEqual(self._names(self._list(filters={"on_hold": 1})), [])
			self.assertIn(skill, self._names(self._list(filters={"on_hold": 0})))
			for call in (
				lambda: skills_admin_api.admin_hold(skill, "Not yet"),
				lambda: skills_admin_api.admin_release(skill),
			):
				with self.assertRaises(frappe.ValidationError) as ctx:
					call()
				self.assertIn("not available yet", str(ctx.exception))
			detail = skills_admin_api.admin_get_skill(skill)
			self.assertEqual(detail["admin_hold"], 0)
			frappe.set_user(OWNER)
			from jarvis.chat import custom_skills_api

			page = custom_skills_api.list_custom_skills_page(search=TAG)
			own = next(r for r in page["rows"] if r["name"] == skill)
			self.assertEqual((own["admin_hold"], own["admin_hold_reason"]), (0, ""))
		self.assertEqual(self._row(skill, "enabled").enabled, 1)
