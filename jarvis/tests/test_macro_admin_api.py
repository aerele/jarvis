"""A Jarvis Admin's view of every user's macros (``jarvis.chat.macros_admin_api``).

What is pinned here:

* each endpoint is for a Jarvis Admin only, by POST only, never from inside a tool
  call and never through the ``run_method`` tool, and refuses a ``name`` that is not
  text before it reads or writes anything;
* the list shows other users' macros with no step or summary text, and the last run
  of each macro whoever owns the run row;
* opening a macro shows its steps and summary and never a run's conversation;
* stopping a run stops it, disarms its chat, tells the owner an administrator did it
  (the reason, the chat's closing line, a notification) and leaves a Comment on the
  macro naming who;
* none of this gives an admin the document API: saving or running another user's
  macro is still refused.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.handler import is_valid_http_method
from frappe.utils import set_request

from jarvis.chat import macros, macros_admin_api, macros_api
from jarvis.exceptions import PermissionDeniedError
from jarvis.permissions import ensure_jarvis_admin_role
from jarvis.tests import test_macro_stop_delete as stop_tests
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tools import registry
from jarvis.tools.run_method import _is_denied, run_method

CONV = stop_tests.CONV
MACRO = stop_tests.MACRO
RUN = stop_tests.RUN
OWNER = stop_tests.OWNER
OTHER = "macro-admin-other@example.com"
ADMIN = "macro-admin-admin@example.com"
PLAIN = "macro-admin-plain@example.com"
# A System Manager with no Jarvis role at all: "Jarvis Admin" in code is the role OR
# System Manager OR Administrator (``jarvis/permissions.py``).
SYSMGR = "macro-admin-sysmgr@example.com"
# Every macro these tests make carries this, so a list call can be narrowed to them:
# the test site is shared and may hold other users' macros.
TAG = "e1adm"
PATH = "jarvis.chat.macros_admin_api."
ENDPOINTS = (
	"admin_list_macros",
	"admin_macro_owners",
	"admin_get_macro",
	"admin_stop_run",
	"admin_hold",
	"admin_release",
	"admin_delete",
)
STOPPED = macros_admin_api.STOPPED_BY_ADMIN
_MISSING = object()


class AdminBase(stop_tests.StopBase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Users are created as Administrator: a plain session cannot insert a User.
		frappe.set_user("Administrator")
		ensure_jarvis_admin_role()
		ensure_user(OTHER)
		ensure_user(PLAIN)
		ensure_user(ADMIN, roles=("Jarvis User", "Jarvis Admin"))
		ensure_user(SYSMGR, roles=("System Manager",))
		for role in ("Jarvis User", "Jarvis Admin"):
			if role in frappe.get_roles(SYSMGR):
				frappe.get_doc("User", SYSMGR).remove_roles(role)
				frappe.db.commit()
		if "System Manager" in frappe.get_roles(ADMIN):
			frappe.get_doc("User", ADMIN).remove_roles("System Manager")
			frappe.db.commit()

	def _purge(self):
		super()._purge()
		frappe.set_user("Administrator")
		for owner in (OTHER, ADMIN, PLAIN, SYSMGR):
			for dt in (RUN, MACRO, CONV):
				for name in frappe.get_all(dt, filters={"owner": owner}, pluck="name"):
					frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			frappe.db.delete("Notification Log", {"for_user": owner})
		frappe.db.delete(
			"Comment",
			{"reference_doctype": MACRO, "comment_email": ["in", (ADMIN, SYSMGR, "Administrator")]},
		)
		frappe.db.commit()

	def _macro(self, owner, tag, *, steps=("first prompt",), **fields):
		"""A macro owned by ``owner``. Fields the form would refuse a plain owner
		(the arm) are written raw: these tests are not about the save rules."""
		frappe.set_user(owner)
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{TAG}-{tag}",
				"steps": [{"prompt": p} for p in steps],
			}
		).insert(ignore_permissions=True)
		if fields:
			frappe.db.set_value(MACRO, doc.name, fields, update_modified=False)
		frappe.db.commit()
		return doc.name

	def _run_row(self, macro, owner, status, *, creation=None, conversation=None, **fields):
		"""A run row of ``macro`` owned by ``owner``, the way the engine leaves one."""
		frappe.set_user(owner)
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": macro,
				"conversation": conversation,
				"status": status,
				"current_step": 1,
				"total_steps": 2,
				"run_mode": "stepped",
				"trigger": "manual",
				**fields,
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		if creation:
			frappe.db.set_value(RUN, run.name, "creation", creation, update_modified=False)
		frappe.db.commit()
		return run.name

	def _list(self, **kwargs):
		"""The admin list as ADMIN, narrowed to this module's macros."""
		frappe.set_user(ADMIN)
		kwargs.setdefault("search", TAG)
		return macros_admin_api.admin_list_macros(**kwargs)

	def _names(self, res):
		return [r["name"] for r in res["rows"]]

	def _notes(self, user=OWNER):
		return frappe.get_all(
			"Notification Log",
			filters={"for_user": user},
			fields=["subject", "email_content", "document_type", "document_name"],
		)

	def _comments(self, macro):
		return frappe.get_all(
			"Comment",
			filters={"reference_doctype": MACRO, "reference_name": macro},
			fields=["comment_type", "comment_email", "content"],
		)


# --------------------------------------------------------------------------- #
# Who may call them, and how
# --------------------------------------------------------------------------- #
class TestTheGates(AdminBase):
	def _calls(self):
		"""``{endpoint: kwargs}`` aimed at a live run of OWNER's macro, plus the run."""
		run, _conv, macro = self._mk_run(tag=f"{TAG}-gate")
		return (
			run,
			macro,
			{
				"admin_list_macros": {},
				"admin_macro_owners": {},
				"admin_get_macro": {"name": macro},
				"admin_stop_run": {"run": run},
				"admin_hold": {"macro": macro, "reason": "Paused for review"},
				"admin_release": {"macro": macro},
				"admin_delete": {"macro": macro},
			},
		)

	def test_a_jarvis_admin_succeeds_on_another_users_macro(self):
		run, macro, calls = self._calls()
		frappe.set_user(ADMIN)
		self.assertIn(macro, self._names(macros_admin_api.admin_list_macros(search=TAG)))
		self.assertEqual(macros_admin_api.admin_get_macro(**calls["admin_get_macro"])["owner"], OWNER)
		self.assertTrue(macros_admin_api.admin_stop_run(**calls["admin_stop_run"])["stopped"])
		self.assertEqual(self._run(run).status, "stopped")

	def test_a_system_manager_and_administrator_are_admins_too(self):
		# Neither holds the Jarvis Admin role. The description's privacy note is about
		# exactly these two, so a gate narrowed to the role alone must fail here.
		self.assertNotIn("Jarvis Admin", frappe.get_roles(SYSMGR))
		for who in (SYSMGR, "Administrator"):
			with self.subTest(who=who):
				run, macro, calls = self._calls()
				frappe.set_user(who)
				self.assertIn(macro, self._names(macros_admin_api.admin_list_macros(search=TAG)))
				owners = macros_admin_api.admin_macro_owners(**calls["admin_macro_owners"])["owners"]
				self.assertIn(OWNER, [o["user"] for o in owners])
				self.assertEqual(macros_admin_api.admin_get_macro(**calls["admin_get_macro"])["owner"], OWNER)
				self.assertTrue(macros_admin_api.admin_stop_run(**calls["admin_stop_run"])["stopped"])
				self.assertEqual(self._run(run).status, "stopped")
				(comment,) = self._comments(macro)
				self.assertEqual(comment.comment_email, who)
				self._purge()

	def test_anyone_but_an_admin_is_refused(self):
		# The macro's own owner is "a plain user" here too: these are not their
		# endpoints, and their Stop is `macros_api.stop_macro_run`.
		run, _macro, calls = self._calls()
		for who in (PLAIN, OWNER):
			for endpoint, kwargs in calls.items():
				with self.subTest(who=who, endpoint=endpoint):
					frappe.set_user(who)
					with self.assertRaises(frappe.PermissionError):
						getattr(macros_admin_api, endpoint)(**kwargs)
		self.assertEqual(self._run(run).status, "running")

	def test_every_endpoint_is_post_only(self):
		prev = getattr(frappe.local, "request", _MISSING)
		self.addCleanup(self._restore_request, prev)
		for endpoint in ENDPOINTS:
			with self.subTest(endpoint=endpoint):
				fn = getattr(macros_admin_api, endpoint)
				self.assertIn(fn, frappe.whitelisted)
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])
				set_request(method="GET", path=f"/api/method/{PATH}{endpoint}")
				with self.assertRaises(frappe.PermissionError):
					is_valid_http_method(fn)
				set_request(method="POST", path=f"/api/method/{PATH}{endpoint}")
				is_valid_http_method(fn)  # no raise

	@staticmethod
	def _restore_request(prev):
		if prev is _MISSING:
			if hasattr(frappe.local, "request"):
				del frappe.local.request
		else:
			frappe.local.request = prev

	def test_no_module_function_is_whitelisted_without_being_listed_here(self):
		# A verb added to the module later is covered by the loops above or fails here.
		found = {
			name
			for name, fn in vars(macros_admin_api).items()
			if callable(fn)
			and getattr(fn, "__module__", "") == macros_admin_api.__name__
			and fn in frappe.whitelisted
		}
		self.assertEqual(found, set(ENDPOINTS))

	def test_refused_inside_a_tool_call(self):
		# An admin's assistant must not reach these: the read would put every user's
		# prompts in the model's context.
		run, _macro, calls = self._calls()
		frappe.set_user(ADMIN)
		for endpoint, kwargs in calls.items():
			with self.subTest(endpoint=endpoint):
				err = self._raised_in_a_tool(getattr(macros_admin_api, endpoint), **kwargs)
				self.assertIsInstance(err, frappe.PermissionError, f"{endpoint} ran inside a tool")
				self.assertIn("inside a tool", str(err))
		self.assertEqual(self._run(run).status, "running")

	def _raised_in_a_tool(self, fn, **kwargs):
		"""Call ``fn`` as the body of a registered tool: the real ``registry.dispatch``
		holds the depth counter as it does for a model's tool call."""
		probe = "zz_macro_admin_probe"

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

	def test_refused_through_the_run_method_tool(self):
		run, _macro, calls = self._calls()
		frappe.set_user(ADMIN)
		for endpoint, kwargs in calls.items():
			with self.subTest(endpoint=endpoint):
				self.assertTrue(_is_denied(getattr(macros_admin_api, endpoint)))
				with patch("frappe.call") as call:
					with self.assertRaises(PermissionDeniedError):
						run_method(PATH + endpoint, kwargs)
				call.assert_not_called()
		# And for real, with nothing patched: the run is not stopped.
		with self.assertRaises(PermissionDeniedError):
			run_method(PATH + "admin_stop_run", {"run": run})
		self.assertEqual(self._run(run).status, "running")

	def test_a_name_that_is_not_text_is_refused_before_anything_is_read(self):
		# A dict reaches the database layer as a FILTER: `{"owner": ...}` would load
		# (and stop) whichever row matched it. The endpoint's own check is the only
		# thing in the way: the module's hints are strings (`from __future__ import
		# annotations`) and Frappe's type check skips string hints.
		run, _macro, _calls = self._calls()
		frappe.set_user(ADMIN)
		matches_the_run = {"owner": OWNER}
		for endpoint, arg in (("admin_get_macro", "name"), ("admin_stop_run", "run")):
			for bad in (None, "", "  ", ["x"], matches_the_run, 7):
				with self.subTest(endpoint=endpoint, value=bad):
					fn = getattr(macros_admin_api, endpoint)
					with (
						patch.object(frappe, "get_doc", wraps=frappe.get_doc) as get_doc,
						patch.object(frappe.db, "get_value", wraps=frappe.db.get_value) as get_value,
						patch.object(macros, "_stop_run") as stop,
					):
						with self.assertRaises(frappe.ValidationError):
							fn(**{arg: bad})
					# The gates read the caller's own User row; nothing reads a macro or a run.
					read = [c.args[0] for c in get_doc.call_args_list + get_value.call_args_list if c.args]
					self.assertFalse({MACRO, RUN} & {r for r in read if isinstance(r, str)}, read)
					stop.assert_not_called()
		self.assertEqual(self._run(run).status, "running")

	def test_a_name_that_is_not_text_is_refused_on_a_request_too(self):
		# With a real request in place, which is when Frappe would type-check a
		# whitelisted function's arguments. It does not check these (string hints),
		# so the refusal must still be the endpoint's own sentence.
		run, _macro, _calls = self._calls()
		prev = getattr(frappe.local, "request", _MISSING)
		self.addCleanup(self._restore_request, prev)
		frappe.set_user(ADMIN)
		for endpoint, arg in (("admin_get_macro", "name"), ("admin_stop_run", "run")):
			set_request(method="POST", path=f"/api/method/{PATH}{endpoint}")
			for bad in (None, ["x"], {"owner": OWNER}, 7):
				with self.subTest(endpoint=endpoint, value=bad):
					with self.assertRaises(frappe.ValidationError) as raised:
						frappe.call(getattr(macros_admin_api, endpoint), **{arg: bad})
					self.assertIn("must be a name", str(raised.exception))
		self.assertEqual(self._run(run).status, "running")

	def test_a_search_or_a_sort_that_is_not_text_is_refused(self):
		# Ignoring one would answer with the unfiltered list; passing one on raised a
		# TypeError from the sort helper.
		self._macro(OWNER, "a")
		prev = getattr(frappe.local, "request", _MISSING)
		self.addCleanup(self._restore_request, prev)
		set_request(method="POST", path=f"/api/method/{PATH}admin_list_macros")
		frappe.set_user(ADMIN)
		for arg in ("search", "sort_field", "sort_dir"):
			for bad in (["x"], {"owner": OWNER}, 7):
				with self.subTest(arg=arg, value=bad):
					with self.assertRaises(frappe.ValidationError) as raised:
						frappe.call(macros_admin_api.admin_list_macros, **{arg: bad})
					self.assertIn("must be text", str(raised.exception))
		# Nothing sent is "not searching", as before.
		res = macros_admin_api.admin_list_macros(search=None, sort_field=None, sort_dir=None)
		self.assertTrue(res["rows"])


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #
class TestTheList(AdminBase):
	def test_it_lists_other_users_macros_without_their_text(self):
		mine = self._macro(OWNER, "a", steps=("SECRET-STEP-ONE", "SECRET-STEP-TWO"))
		frappe.db.set_value(
			MACRO, mine, {"merged_prompt": "SECRET-SUMMARY", "description": "SECRET-DESCRIPTION"}
		)
		theirs = self._macro(OTHER, "b", steps=("SECRET-STEP-THREE",), skip_confirmation=1)
		frappe.db.commit()

		res = self._list()
		rows = {r["name"]: r for r in res["rows"]}
		self.assertEqual(set(rows), {mine, theirs})
		self.assertEqual((rows[mine]["owner"], rows[theirs]["owner"]), (OWNER, OTHER))
		self.assertEqual(rows[mine]["macro_name"], f"{TAG}-a")
		self.assertTrue(rows[mine]["owner_full_name"])
		self.assertEqual((rows[mine]["skip_confirmation"], rows[theirs]["skip_confirmation"]), (0, 1))
		for key in (
			"enabled",
			"schedule_enabled",
			"schedule_frequency",
			"next_run_at",
			"last_run",
			"live_run",
		):
			self.assertIn(key, rows[mine])
		for row in res["rows"]:
			for key in ("steps", "merged_prompt", "description", "prompt"):
				self.assertNotIn(key, row)
		self.assertNotIn("SECRET", json.dumps(res, default=str))

	def test_the_owners_it_offers_as_a_filter_are_everyone_with_a_macro(self):
		self._macro(OWNER, "a")
		self._macro(OTHER, "b")
		self._macro(OTHER, "c")
		frappe.set_user(ADMIN)
		res = macros_admin_api.admin_macro_owners()
		owners = {o["user"]: o for o in res["owners"]}
		self.assertTrue({OWNER, OTHER} <= set(owners))
		self.assertTrue(owners[OTHER]["full_name"])
		self.assertEqual(owners[OTHER]["macros"], 2)
		# Someone with no macro is not offered: this is not a user directory.
		self.assertNotIn(PLAIN, owners)
		# They are not recomputed with every page of the list.
		self.assertNotIn("owners", self._list())

	def test_the_owner_choices_are_capped(self):
		self._macro(OWNER, "a")
		self._macro(OTHER, "b")
		self._macro(OTHER, "c")
		frappe.set_user(ADMIN)
		with patch.object(macros_admin_api, "OWNERS_MAX", 1):
			res = macros_admin_api.admin_macro_owners()
		self.assertEqual((len(res["owners"]), res["more"]), (1, True))
		with patch.object(macros_admin_api, "OWNERS_MAX", 10**6):
			self.assertFalse(macros_admin_api.admin_macro_owners()["more"])

	def test_the_last_run_is_the_macros_whoever_owns_the_run_row(self):
		# After a hand-over the macro belongs to someone who owns none of its past
		# runs. The owner's list looks runs up by owner and would show "never ran".
		macro = self._macro(OWNER, "a")
		self._run_row(macro, OWNER, "failed", creation="2026-01-01 10:00:00", error="boom")
		frappe.db.set_value(MACRO, macro, "owner", OTHER, update_modified=False)
		frappe.db.commit()
		self.assertEqual(macros_api._last_runs([macro], OTHER), {})

		(row,) = self._list()["rows"]
		self.assertEqual(row["owner"], OTHER)
		self.assertEqual(row["last_run"]["status"], "failed")
		# The reason can quote what a step tried to do: it is for the opened macro.
		self.assertNotIn("error", row["last_run"])
		self.assertNotIn("conversation", row["last_run"])

		# And the newest run wins, again whoever owns it.
		self._run_row(macro, OTHER, "completed", creation="2026-01-02 10:00:00")
		(row,) = self._list()["rows"]
		self.assertEqual(row["last_run"]["status"], "completed")

	def test_one_query_finds_the_last_runs_of_a_whole_page(self):
		for tag in ("a", "b", "c"):
			self._run_row(self._macro(OWNER, tag), OWNER, "completed")
		frappe.set_user(ADMIN)
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
			res = macros_admin_api.admin_list_macros(search=TAG)
		self.assertEqual(len(res["rows"]), 3)
		on_runs = [c for c in sql.call_args_list if "tabJarvis Macro Run" in str(c.args[0])]
		# The last runs and the live runs: two statements, however many rows.
		self.assertEqual(len(on_runs), 2)

	def test_a_live_run_is_named_so_it_can_be_stopped(self):
		run, _conv, macro = self._mk_run(tag=f"{TAG}-live")
		(row,) = self._list()["rows"]
		self.assertEqual((row["name"], row["live_run"]), (macro, run))
		# One field names the run to stop. `last_run` naming one too invited stopping
		# the wrong run when an older one is the live one.
		self.assertNotIn("name", row["last_run"])
		self.assertEqual(row["last_run"]["status"], "running")

		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		self.assertEqual(self._list()["rows"][0]["live_run"], run)

		frappe.db.set_value(RUN, run, "status", "completed")
		frappe.db.commit()
		(row,) = self._list()["rows"]
		self.assertEqual(row["live_run"], "")

	def test_an_older_run_that_is_still_live_is_the_one_named(self):
		# Two runs of one macro: the newer one ended, the older one is still going.
		macro = self._macro(OWNER, "a")
		older = self._run_row(macro, OWNER, "running", creation="2026-01-01 10:00:00")
		self._run_row(macro, OWNER, "failed", creation="2026-01-02 10:00:00")
		(row,) = self._list()["rows"]
		self.assertEqual((row["last_run"]["status"], row["live_run"]), ("failed", older))
		self.assertEqual(self._names(self._list(filters={"live_run": 1})), [macro])

	def test_the_last_run_says_an_admin_stopped_it_and_nothing_else_about_why(self):
		theirs = self._macro(OWNER, "theirs")
		engine = self._macro(OWNER, "engine")
		own = self._macro(OWNER, "own")
		failed = self._macro(OWNER, "failed")
		self._run_row(theirs, OWNER, "stopped", error=STOPPED)
		self._run_row(engine, OWNER, "stopped", error="Stopped at step 2: SECRET-STEP-TEXT")
		self._run_row(own, OWNER, "stopped")
		# The same sentence on a run that did not stop is not an admin's stop.
		self._run_row(failed, OWNER, "failed", error=STOPPED)
		res = self._list()
		flags = {r["name"]: r["last_run"]["stopped_by_admin"] for r in res["rows"]}
		self.assertEqual(flags, {theirs: 1, engine: 0, own: 0, failed: 0})
		for row in res["rows"]:
			self.assertNotIn("error", row["last_run"])
		dumped = json.dumps(res, default=str)
		self.assertNotIn("SECRET", dumped)
		self.assertNotIn(STOPPED, dumped)

	def test_it_is_paged(self):
		names = [self._macro(OWNER, tag) for tag in ("a", "b", "c")]
		first = self._list(page_length=2)
		self.assertEqual((len(first["rows"]), first["total"], first["has_more"]), (2, 3, True))
		self.assertEqual((first["start"], first["page_length"]), (0, 2))
		rest = self._list(page_length=2, start=2)
		self.assertEqual((len(rest["rows"]), rest["total"], rest["has_more"]), (1, 3, False))
		self.assertEqual(self._names(first) + self._names(rest), names)  # by name, no overlap

	def test_the_search_matches_the_macro_and_the_owner(self):
		mine = self._macro(OWNER, "alpha")
		theirs = self._macro(OTHER, "beta")
		self.assertEqual(self._names(self._list(search=f"{TAG}-alp")), [mine])
		self.assertEqual(self._names(self._list(search=OTHER, filters={"owner": OTHER})), [theirs])
		# A wildcard typed into the box is a character, not a pattern.
		self.assertEqual(self._names(self._list(search=f"{TAG}-%")), [])

	def test_the_search_matches_the_owners_full_name(self):
		# The list shows the full name first, so that is what an admin types. It
		# matched the address only, and "Asha" found nothing for arao@example.com.
		self._macro(OWNER, "mine")
		theirs = self._macro(OTHER, "theirs")
		prev = frappe.db.get_value("User", OTHER, ["first_name", "last_name", "full_name"], as_dict=True)
		self.addCleanup(self._restore_name, OTHER, prev)
		frappe.set_user("Administrator")
		frappe.db.set_value(
			"User", OTHER, {"first_name": "Zqasha", "last_name": "Rao", "full_name": "Zqasha Rao"}
		)
		frappe.db.commit()
		self.assertNotIn("zqasha", OTHER)
		res = self._list(search="zqasha ra")
		self.assertEqual(self._names(res), [theirs])
		self.assertEqual(res["rows"][0]["owner_full_name"], "Zqasha Rao")
		self.assertEqual(res["total"], 1)

	@staticmethod
	def _restore_name(user, prev):
		frappe.set_user("Administrator")
		frappe.db.set_value("User", user, dict(prev))
		frappe.db.commit()

	def test_the_full_names_of_a_page_take_one_query(self):
		self._macro(OWNER, "a")
		self._macro(OTHER, "b")
		self._macro(ADMIN, "c")
		frappe.set_user(ADMIN)
		with patch.object(frappe, "get_all", wraps=frappe.get_all) as get_all:
			res = macros_admin_api.admin_list_macros(search=TAG)
		self.assertEqual(len(res["rows"]), 3)
		self.assertEqual(len([c for c in get_all.call_args_list if c.args and c.args[0] == "User"]), 1)

	def test_each_filter_narrows_the_list(self):
		plain = self._macro(OWNER, "plain")
		armed = self._macro(OTHER, "armed", skip_confirmation=1)
		scheduled = self._macro(OTHER, "sched", schedule_enabled=1)
		live = self._macro(OWNER, "live")
		self._run_row(live, OWNER, "waiting_capacity")
		self._run_row(plain, OWNER, "completed")
		everything = {plain, armed, scheduled, live}

		def listed(**filters):
			return set(self._names(self._list(filters=filters)))

		self.assertEqual(listed(), everything)
		self.assertEqual(listed(owner=OTHER), {armed, scheduled})
		self.assertEqual(listed(armed=1), {armed})
		self.assertEqual(listed(armed=0), everything - {armed})
		self.assertEqual(listed(scheduled=1), {scheduled})
		self.assertEqual(listed(scheduled=0), everything - {scheduled})
		self.assertEqual(listed(live_run=1), {live})
		self.assertEqual(listed(live_run=0), everything - {live})
		self.assertEqual(listed(owner=OTHER, armed=1), {armed})
		# As the form posts them: one JSON string.
		self.assertEqual(set(self._names(self._list(filters='{"armed": "1"}'))), {armed})

	def test_a_filter_it_does_not_know_or_cannot_read_is_refused(self):
		self._macro(OWNER, "a")
		for bad in ({"enabled_by": 1}, {"owner": [OWNER]}, {"owner": {"like": "%"}}, {"armed": 2}):
			with self.subTest(filters=bad):
				with self.assertRaises(frappe.ValidationError):
					self._list(filters=bad)

	def test_filters_that_are_not_an_object_are_refused_not_dropped(self):
		# The shared reader turns a list or unreadable JSON into "no filters", and the
		# answer was every user's macros: a filter that silently did nothing.
		self._macro(OWNER, "a")
		for bad in ([["owner", "=", OTHER]], '[["owner", "=", "x"]]', "{not json", "owner=x", 7, '"text"'):
			with self.subTest(filters=bad):
				with self.assertRaises(frappe.ValidationError) as raised:
					self._list(filters=bad)
				self.assertIn("Filters", str(raised.exception))
		# Nothing sent is still "not filtering".
		for nothing in (None, "", "  ", {}, "{}"):
			with self.subTest(filters=nothing):
				self.assertEqual(len(self._list(filters=nothing)["rows"]), 1)

	def test_only_a_listed_column_sorts_it(self):
		a = self._macro(OTHER, "a")
		b = self._macro(OWNER, "b")
		self.assertEqual(self._names(self._list()), [a, b])
		self.assertEqual(self._names(self._list(sort_field="macro_name", sort_dir="desc")), [b, a])
		# Anything else falls back to the default order; nothing is interpolated.
		self.assertEqual(self._names(self._list(sort_field="name; DROP TABLE x", sort_dir="desc")), [a, b])


# --------------------------------------------------------------------------- #
# One macro, read-only
# --------------------------------------------------------------------------- #
class TestOpeningAMacro(AdminBase):
	def test_it_shows_the_steps_the_summary_and_the_recent_runs(self):
		run, conv, macro = self._mk_run(steps=2, at_step=1, tag=f"{TAG}-open")
		frappe.db.set_value(MACRO, macro, {"merged_prompt": "the summary", "merge_status": "ready"})
		frappe.db.set_value(RUN, run, {"status": "failed", "error": "Step 1 failed: no such customer."})
		frappe.db.commit()

		frappe.set_user(ADMIN)
		got = macros_admin_api.admin_get_macro(macro)
		self.assertEqual((got["name"], got["owner"]), (macro, OWNER))
		self.assertTrue(got["owner_full_name"])
		self.assertEqual([s["prompt"] for s in got["steps"]], ["step 1", "step 2"])
		self.assertEqual((got["merged_prompt"], got["merge_status"]), ("the summary", "ready"))
		(shown,) = got["runs"]
		self.assertEqual(
			{k: shown[k] for k in ("name", "status", "trigger", "current_step", "total_steps", "error")},
			{
				"name": run,
				"status": "failed",
				"trigger": "manual",
				"current_step": 1,
				"total_steps": 2,
				"error": "Step 1 failed: no such customer.",
			},
		)
		self.assertIn("started_at", shown)
		self.assertIn("finished_at", shown)
		# Never the run's chat: an admin may know a run failed, not read it.
		self.assertNotIn("conversation", shown)
		self.assertNotIn(conv, json.dumps(got, default=str))
		self.assertNotIn("merge_conversation", got)

	def test_its_runs_are_the_macros_whoever_owns_them_newest_first(self):
		macro = self._macro(OWNER, "a")
		old = self._run_row(macro, OWNER, "completed", creation="2026-01-01 10:00:00")
		frappe.db.set_value(MACRO, macro, "owner", OTHER, update_modified=False)
		new = self._run_row(macro, OTHER, "running", creation="2026-01-02 10:00:00")
		frappe.set_user(ADMIN)
		got = macros_admin_api.admin_get_macro(macro)
		self.assertEqual([r["name"] for r in got["runs"]], [new, old])
		self.assertEqual(got["live_run"], new)

	def test_it_shows_only_the_most_recent_runs(self):
		macro = self._macro(OWNER, "a")
		for day in range(1, macros_admin_api.RECENT_RUNS + 3):
			self._run_row(macro, OWNER, "completed", creation=f"2026-01-{day:02d} 10:00:00")
		frappe.set_user(ADMIN)
		self.assertEqual(len(macros_admin_api.admin_get_macro(macro)["runs"]), macros_admin_api.RECENT_RUNS)

	def test_a_macro_that_is_not_there_is_not_found(self):
		# In the pane's own words: the usual cause is a list loaded before the owner
		# deleted the macro, and "Jarvis Macro <hash> not found" tells an admin nothing.
		frappe.set_user(ADMIN)
		with self.assertRaises(frappe.DoesNotExistError) as raised:
			macros_admin_api.admin_get_macro("zz-no-such-macro")
		self.assertEqual(str(raised.exception), "This macro was deleted.")


# --------------------------------------------------------------------------- #
# Stop a run
# --------------------------------------------------------------------------- #
class TestStoppingAnotherUsersRun(AdminBase):
	def test_it_stops_a_running_run_and_says_who_did(self):
		run, conv, macro = self._mk_run(steps=2, at_step=1, tag=f"{TAG}-stop")
		frappe.set_user(ADMIN)
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			res = macros_admin_api.admin_stop_run(run)
		self.assertEqual((res["ok"], res["stopped"], res["status"]), (True, True, "stopped"))

		row = self._run(run)
		# What the owner reads, on the run and as the last line of its chat.
		self.assertEqual((row.status, row.error), ("stopped", STOPPED))
		self.assertIn("administrator", STOPPED)
		(closing,) = self._closing(conv, run)
		self.assertEqual(closing.content, f"■ {STOPPED}")
		(done,) = [c.args[1] for c in publish.call_args_list if c.args[1].get("kind") == "macro:done"]
		self.assertEqual((done["status"], done["error"]), ("stopped", STOPPED))
		(told,) = [c.args[0] for c in publish.call_args_list if c.args[1].get("kind") == "macro:done"]
		self.assertEqual(told, OWNER)

		(comment,) = self._comments(macro)
		self.assertEqual((comment.comment_type, comment.comment_email), ("Info", ADMIN))
		self.assertIn(ADMIN, comment.content)
		self.assertIn(run, comment.content)
		# The Comment did not go through a save of the macro.
		self.assertEqual(frappe.db.get_value(MACRO, macro, "modified_by"), OWNER)

	def test_the_owner_is_notified_that_an_administrator_stopped_it(self):
		# The closing line and the realtime event reach someone looking at the app.
		# Nobody is watching a scheduled run, and the engine's own notification is
		# for runs that end from inside: an admin stop sent none.
		for trigger in ("scheduled", "manual"):
			with self.subTest(trigger=trigger):
				run, _conv, macro = self._mk_run(trigger=trigger, tag=f"{TAG}-note")
				macro_name = frappe.db.get_value(MACRO, macro, "macro_name")
				frappe.set_user(ADMIN)
				self.assertTrue(macros_admin_api.admin_stop_run(run)["stopped"])
				(note,) = self._notes()
				self.assertEqual(note.subject, f"Macro run stopped: {macro_name}")
				self.assertIn(STOPPED, note.email_content)
				# The engine's shape: a note, with no document to open.
				self.assertFalse(note.document_type or note.document_name)
				self._purge()

	def test_a_run_that_had_already_ended_notifies_nobody(self):
		run, _conv, _macro = self._mk_run(tag=f"{TAG}-ended")
		frappe.db.set_value(RUN, run, "status", "completed")
		frappe.db.commit()
		frappe.set_user(ADMIN)
		self.assertFalse(macros_admin_api.admin_stop_run(run)["stopped"])
		self.assertEqual(self._notes(), [])

	def test_an_admin_stopping_their_own_run_is_not_notified_of_it(self):
		macro = self._macro(ADMIN, "own")
		run = self._run_row(macro, ADMIN, "running")
		frappe.set_user(ADMIN)
		self.assertTrue(macros_admin_api.admin_stop_run(run)["stopped"])
		self.assertEqual(self._notes(ADMIN), [])

	def test_a_notification_that_cannot_be_sent_does_not_fail_the_stop(self):
		run, _conv, macro = self._mk_run(tag=f"{TAG}-nonote")
		frappe.set_user(ADMIN)
		with (
			patch.object(macros, "notify_owner", side_effect=RuntimeError("no note")),
			patch.object(frappe, "log_error") as log,
		):
			res = macros_admin_api.admin_stop_run(run)
		self.assertTrue(res["stopped"])
		self.assertEqual(self._run(run).status, "stopped")
		self.assertEqual(len(self._comments(macro)), 1)
		self.assertTrue(self._logged(log, "jarvis.chat.macros_admin_api.notify_failed"))

	def test_the_stop_is_asked_as_the_admin(self):
		run, _conv, _macro = self._mk_run(tag=f"{TAG}-by")
		frappe.set_user(ADMIN)
		with patch.object(macros, "_stop_run", return_value=True) as stop:
			macros_admin_api.admin_stop_run(run)
		stop.assert_called_once_with(run, reason=STOPPED, by=ADMIN)

	def test_it_stops_a_run_waiting_for_capacity(self):
		run, _conv, macro = self._mk_run(steps=2, at_step=0, tag=f"{TAG}-wait")
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		frappe.set_user(ADMIN)
		self.assertTrue(macros_admin_api.admin_stop_run(run)["stopped"])
		self.assertEqual(self._run(run).status, "stopped")
		with patch("jarvis.chat.api._enqueue_turn") as enqueue:
			macros.resume_waiting_capacity_runs()
		enqueue.assert_not_called()
		self.assertEqual(len(self._comments(macro)), 1)

	def test_it_disarms_an_armed_runs_chat(self):
		run, conv, _macro = self._mk_run(steps=2, at_step=1, armed=True, tag=f"{TAG}-armed")
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 1)
		frappe.set_user(ADMIN)
		self.assertTrue(macros_admin_api.admin_stop_run(run)["stopped"])
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)

	def test_it_cancels_a_step_that_has_not_started(self):
		run, conv, _macro = self._mk_run(steps=2, at_step=1, tag=f"{TAG}-queued")
		turn, _seed, _assistant = self._step(conv, "queued")
		frappe.set_user(ADMIN)
		self.assertTrue(macros_admin_api.admin_stop_run(run)["stopped"])
		self.assertEqual(self._turn_state(turn), "cancelled")

	def test_a_run_that_already_ended_is_left_alone_and_the_answer_says_so(self):
		for status in ("completed", "failed", "stopped"):
			with self.subTest(status=status):
				run, conv, macro = self._mk_run(tag=f"{TAG}-{status}")
				frappe.db.set_value(RUN, run, {"status": status, "error": "as it ended"})
				frappe.db.commit()
				frappe.set_user(ADMIN)
				res = macros_admin_api.admin_stop_run(run)
				self.assertEqual((res["ok"], res["stopped"], res["status"]), (True, False, status))
				self.assertIn("already ended", res["message"])
				row = self._run(run)
				self.assertEqual((row.status, row.error), (status, "as it ended"))
				self.assertEqual(self._closing(conv, run), [])
				self.assertEqual(self._comments(macro), [])

	def test_a_run_that_is_not_there_is_not_found(self):
		frappe.set_user(ADMIN)
		with self.assertRaises(frappe.DoesNotExistError):
			macros_admin_api.admin_stop_run("zz-no-such-run")

	def test_a_comment_that_cannot_be_written_does_not_undo_the_stop(self):
		# The real path: the Comment's own insert fails after its row is written, so
		# the rollback in the handler has something to take back.
		from frappe.core.doctype.comment.comment import Comment

		run, _conv, macro = self._mk_run(tag=f"{TAG}-nocomment")
		frappe.db.set_value(MACRO, macro, "description", "SECRET-DESCRIPTION")
		frappe.db.commit()
		frappe.set_user(ADMIN)
		with (
			patch.object(Comment, "after_insert", side_effect=RuntimeError("no comment"), create=True),
			patch.object(frappe, "log_error") as log,
		):
			res = macros_admin_api.admin_stop_run(run)
		self.assertTrue(res["stopped"])
		self.assertEqual(self._run(run).status, "stopped")
		frappe.db.commit()
		self.assertEqual(self._comments(macro), [])
		# The Comment was the only record of who stopped it: the log says it instead,
		# by id, with none of the macro's text.
		(logged,) = self._logged(log, "jarvis.chat.macros_admin_api.comment_failed")
		self.assertIn(run, logged["title"])
		for said in (ADMIN, run, macro, "no comment"):
			self.assertIn(said, logged["message"])
		self.assertNotIn("SECRET", logged["message"])
		self.assertNotIn(f"{TAG}-nocomment", logged["message"])
		# And the owner is still told.
		self.assertEqual(len(self._notes()), 1)


# --------------------------------------------------------------------------- #
# Nothing else changed for an admin
# --------------------------------------------------------------------------- #
class TestAnAdminIsStillNotTheOwner(AdminBase):
	def test_the_document_api_still_refuses_an_admins_read_and_write(self):
		macro = self._macro(OWNER, "a", enabled=1)
		frappe.set_user(ADMIN)
		doc = frappe.get_doc(MACRO, macro)
		for ptype in ("read", "write", "delete"):
			self.assertFalse(doc.has_permission(ptype), ptype)
		with self.assertRaises(frappe.PermissionError):
			frappe.client.get(MACRO, macro)
		with self.assertRaises(frappe.PermissionError):
			frappe.client.set_value(MACRO, macro, "enabled", 0)
		changed = doc.as_dict()
		changed["macro_name"] = f"{TAG}-renamed"
		changed["steps"] = [{"prompt": "an admin's prompt"}]
		with self.assertRaises(frappe.PermissionError):
			frappe.client.save(frappe.as_json(changed))
		with self.assertRaises(frappe.PermissionError):
			frappe.client.delete(MACRO, macro)
		self.assertEqual(frappe.get_list(MACRO, filters={"name": macro}), [])

		frappe.db.rollback()
		row = frappe.db.get_value(MACRO, macro, ["enabled", "macro_name"], as_dict=True)
		self.assertEqual((row.enabled, row.macro_name), (1, f"{TAG}-a"))
		self.assertEqual(
			frappe.get_all("Jarvis Macro Step", filters={"parent": macro}, pluck="prompt"), ["first prompt"]
		)

	def test_an_admin_still_cannot_run_or_change_another_users_macro(self):
		macro = self._macro(OWNER, "a")
		frappe.set_user(ADMIN)
		with self.assertRaises(frappe.PermissionError):
			macros_api.run_macro(macro)
		with self.assertRaises(frappe.PermissionError):
			macros.run_macro(macro, trigger="manual")
		with self.assertRaises(frappe.PermissionError):
			macros_api.update_macro(name=macro, macro_name=f"{TAG}-renamed")
		with self.assertRaises(frappe.PermissionError):
			macros_api.get_macro(macro)
		with self.assertRaises(frappe.PermissionError):
			macros_api.delete_macro(macro)
		frappe.db.rollback()
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)
		self.assertEqual(frappe.db.get_value(MACRO, macro, "macro_name"), f"{TAG}-a")

	def test_the_owners_own_list_is_still_only_theirs(self):
		mine = self._macro(OWNER, "a")
		self._macro(OTHER, "b")
		frappe.set_user(OWNER)
		self.assertEqual([r["name"] for r in macros_api.list_macros_page(search=TAG)["rows"]], [mine])
		frappe.set_user(ADMIN)
		self.assertEqual(macros_api.list_macros_page(search=TAG)["rows"], [])
