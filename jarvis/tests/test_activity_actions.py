"""Direct SPA/PWA actions in the user-activity report: the request hooks.

Direct-call style (as ``test_error_capture.py``): a fake request is bound, ``capture_request``
(before_request) runs, a stand-in "endpoint" does whatever the real one would do to the
session, then ``record_request`` (after_request) sees the response. One end-to-end class at
the bottom drives the real WSGI app to prove both hooks are registered.

The hook COMMITS its own row (it runs past the request commit), so these tests clean their
rows explicitly instead of relying on the class rollback.
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

import frappe
import redis
from frappe.tests.test_api import FrappeAPITestCase
from frappe.tests.utils import FrappeTestCase
from werkzeug.datastructures import Headers
from werkzeug.wrappers import Response

from jarvis import activity_actions, activity_log
from jarvis._session import impersonate

DT = "Jarvis Activity Event"
USER = "actact-user@example.com"
OTHER = "actact-other@example.com"
SECRET = "SECRET-FORM-VALUE-71c4"

ARCHIVE = "jarvis.chat.api.archive_conversation"  # LOGGED, id `conversation` of Jarvis Conversation
CREATE_MACRO = "jarvis.chat.macros_api.create_macro"  # LOGGED, no id param
RUN_MACRO = "jarvis.chat.macros_api.run_macro"  # LOGGED, id `name`
CLEAR_HISTORY = "jarvis.chat.api.clear_chat_history"  # LOGGED, no id param
LOAD_DOC = "jarvis.chat.actions_api.load_doc"  # LOGGED read (D15), ref_doctype from `doctype`
DOWNLOAD = "jarvis.chat.export.download_conversation"  # LOGGED export
LIST_CONVERSATIONS = "jarvis.chat.api.list_conversations"  # SKIPPED read


def _path(key: str) -> str:
	return f"/api/method/{key}"


def _reset_flood_state():
	"""Forget the dedupe keys, per-minute caps and health counters: they are site-scoped Redis
	keys that outlive a test (and a test run), so a call one test made would dedupe the next."""
	cache = frappe.cache()
	for prefix in (activity_actions.FLOOD_PREFIX, activity_actions.SEEN_PREFIX):
		cache.delete_keys(prefix)


class _Flaky:
	"""The real cache, except the named commands raise a connection error (Redis down)."""

	def __init__(self, real, broken):
		self._real, self._broken = real, set(broken)

	def __getattr__(self, name):
		if name in self._broken:
			raise redis.exceptions.ConnectionError("redis is down")
		return getattr(self._real, name)


class _Req:
	"""The bits of a werkzeug request the hooks read."""

	def __init__(self, path, method="POST", headers=None):
		self.path = path
		self.method = method
		self.headers = Headers(headers or {})


class Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for email in (USER, OTHER):
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": email.split("@")[0],
						"send_welcome_email": 0,
						"user_type": "System User",
					}
				).insert(ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.delete(DT)
		_reset_flood_state()
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "actact"}).insert(
			ignore_permissions=True
		)
		self.conv = conv.name
		frappe.db.commit()  # the hook rolls back first: what a test needs must be committed
		self._had_request = hasattr(frappe.local, "request")
		self._request = getattr(frappe.local, "request", None)
		self._response = getattr(frappe.local, "response", None)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.jarvis_activity_req = None
		if self._had_request:
			frappe.local.request = self._request
		elif hasattr(frappe.local, "request"):
			del frappe.local.request
		frappe.local.response = self._response
		frappe.db.rollback()
		frappe.db.delete(DT)
		frappe.db.delete("Jarvis Conversation", {"name": self.conv})
		frappe.db.delete("ToDo", {"description": ("like", "actact-%")})
		frappe.db.commit()
		_reset_flood_state()
		super().tearDown()

	def call(
		self,
		path,
		*,
		form=None,
		headers=None,
		status=200,
		body="{}",
		user=USER,
		handler=None,
		exc_type=None,
		method="POST",
		mimetype="application/json",
		capture=True,
		response=True,
	):
		"""One request through both hooks, as Frappe would run them."""
		frappe.set_user(user)
		request = _Req(path, method, headers)
		frappe.local.request = request
		frappe.local.form_dict = frappe._dict(form or {})
		frappe.local.response = frappe._dict(exc_type=exc_type) if exc_type else frappe._dict()
		if capture:
			activity_actions.capture_request()
		if handler:
			handler()
		resp = Response(body, status=status, mimetype=mimetype) if response else None
		activity_actions.record_request(response=resp, request=request)
		frappe.set_user("Administrator")
		frappe.db.rollback()  # the test connection's snapshot: see the hook's committed row

	def rows(self) -> list:
		return frappe.get_all(DT, fields=["*"], order_by="creation, name")

	def one(self) -> frappe._dict:
		rows = self.rows()
		self.assertEqual(len(rows), 1, rows)
		return rows[0]


# --------------------------------------------------------------------------- #
# routes: every way to run a LOGGED endpoint records exactly one row
# --------------------------------------------------------------------------- #
class TestRoutes(Base):
	def test_api_method_path_records_the_canonical_name(self):
		self.call(_path(CREATE_MACRO))
		row = self.one()
		self.assertEqual(
			(row.event, row.method, row.actor, row.outcome, row.channel),
			("action", CREATE_MACRO, USER, "ok", "api"),
		)
		self.assertEqual(row.label, "Create macro")
		self.assertEqual(row.actor_name, frappe.db.get_value("User", USER, "full_name"))

	def test_v1_prefix_path(self):
		self.call(f"/api/v1/method/{CREATE_MACRO}")
		self.assertEqual(self.one().method, CREATE_MACRO)

	def test_v1_ignores_what_follows_the_first_slash(self):
		# api/v1.py handle_rpc_call: method.split("/")[0]
		self.call(_path(CREATE_MACRO) + "/trailing/segments")
		self.assertEqual(self.one().method, CREATE_MACRO)

	def test_cmd_form_key_on_another_path_runs_the_cmd(self):
		# app.py: a client-supplied cmd wins over routing, on any path.
		self.call("/api/method/ping", form={"cmd": CREATE_MACRO})
		self.assertEqual(self.one().method, CREATE_MACRO)

	def test_cmd_beats_the_path_both_ways(self):
		# The PATH is LOGGED but the cmd that actually runs is a read: nothing to record.
		self.call(_path(CREATE_MACRO), form={"cmd": LIST_CONVERSATIONS})
		self.assertEqual(self.rows(), [])
		# And the other way round: a read path cannot hide a LOGGED cmd.
		self.call(_path(LIST_CONVERSATIONS), form={"cmd": CREATE_MACRO})
		self.assertEqual(self.one().method, CREATE_MACRO)

	def test_cmd_on_a_non_api_path(self):
		self.call("/", form={"cmd": CREATE_MACRO})
		self.assertEqual(self.one().method, CREATE_MACRO)

	def test_v2_method_path(self):
		self.call(f"/api/v2/method/{CREATE_MACRO}")
		self.assertEqual(self.one().method, CREATE_MACRO)

	def test_v2_doctype_method_form_expands_through_the_controller_module(self):
		from jarvis.chat import macros_api

		with patch("frappe.modules.utils.load_doctype_module", return_value=macros_api) as load:
			self.call("/api/v2/method/Jarvis Macro/create_macro")
		load.assert_called_once_with("Jarvis Macro")
		self.assertEqual(self.one().method, CREATE_MACRO)

	def test_v2_doctype_form_with_no_controller_is_ignored(self):
		with patch("frappe.modules.utils.load_doctype_module", side_effect=ImportError("gone")):
			self.call("/api/v2/method/Nope/create_macro", status=404)
		self.assertEqual(self.rows(), [])

	def test_alias_module_path_is_the_same_endpoint(self):
		# A re-export under another module path: the canonical key is the DEFINING module's.
		from jarvis.chat import macros_api

		alias = types.ModuleType("jarvis._activity_alias_probe")
		alias.renamed_create = macros_api.create_macro
		sys.modules[alias.__name__] = alias
		self.addCleanup(sys.modules.pop, alias.__name__, None)
		self.call(_path("jarvis._activity_alias_probe.renamed_create"))
		self.assertEqual(self.one().method, CREATE_MACRO)

	def test_override_whitelisted_method_is_applied(self):
		# Frappe runs the OVERRIDE; the row names what ran, not what was asked for.
		with patch.object(
			frappe,
			"override_whitelisted_method",
			side_effect=lambda m: CREATE_MACRO if m == LIST_CONVERSATIONS else m,
		):
			self.call(_path(LIST_CONVERSATIONS))
		self.assertEqual(self.one().method, CREATE_MACRO)

	def test_options_preflight_is_not_a_call(self):
		self.call(_path(CREATE_MACRO), method="OPTIONS")
		self.assertEqual(self.rows(), [])

	def test_unresolvable_or_unknown_names_record_nothing(self):
		self.call(_path("jarvis.chat.macros_api.no_such_function"), status=404)
		self.call(_path("jarvis.no_such_module.fn"), status=404)
		self.call(_path("jarvis"), status=404)
		self.call("/api/method/", status=404)
		self.call("/api/resource/ToDo")
		self.assertEqual(self.rows(), [])


class TestImpersonation(Base):
	def test_an_endpoint_that_impersonates_still_records_one_row_with_its_ref(self):
		# `impersonate` calls frappe.set_user on entry AND exit, and set_user replaces
		# local.form_dict: an after_request that read form_dict would find no `cmd` and no id.
		def run():
			with impersonate(OTHER):
				self.assertEqual(frappe.session.user, OTHER)

		self.call(
			"/api/method/ping",
			form={"cmd": ARCHIVE, "conversation": self.conv},
			handler=run,
		)
		row = self.one()
		self.assertEqual((row.method, row.actor), (ARCHIVE, USER))
		self.assertEqual((row.ref_doctype, row.ref_name), ("Jarvis Conversation", self.conv))

	def test_impersonation_on_the_plain_path_too(self):
		def run():
			with impersonate(OTHER):
				pass

		self.call(_path(ARCHIVE), form={"conversation": self.conv}, handler=run)
		self.assertEqual(self.one().ref_name, self.conv)


# --------------------------------------------------------------------------- #
# what is not recorded
# --------------------------------------------------------------------------- #
class TestSkipped(Base):
	def test_every_skipped_endpoint_records_nothing(self):
		skipped = [k for k, v in activity_actions.CLASSIFICATION.items() if v[0] == "SKIPPED"]
		self.assertGreater(len(skipped), 100)
		for key in skipped:
			self.call(_path(key))
		self.assertEqual(self.rows(), [])

	def test_each_skip_reason_has_a_live_example(self):
		reasons = {v[1] for v in activity_actions.CLASSIFICATION.values() if v[0] == "SKIPPED"}
		self.assertEqual(reasons, {"read", "poll", "ui_state", "chat", "tool_path", "desk", "self", "auth"})

	def test_the_tool_path_confirm_endpoint_is_not_double_counted(self):
		key = "jarvis.chat.actions_api.confirm_tool"
		self.assertEqual(activity_actions.CLASSIFICATION[key], ("SKIPPED", "tool_path"))
		self.call(_path(key))
		self.assertEqual(self.rows(), [])

	def test_desk_only_endpoints_are_never_logged(self):
		desk = [k for k, v in activity_actions.CLASSIFICATION.items() if v == ("SKIPPED", "desk")]
		self.assertTrue(desk)
		for key in desk:
			self.call(_path(key))
			self.call("/api/method/ping", form={"cmd": key})
		self.assertEqual(self.rows(), [])

	def test_guest_401_and_429_record_nothing(self):
		self.call(_path(CREATE_MACRO), user="Guest")
		self.call(_path(CREATE_MACRO), status=401)
		self.call(_path(CREATE_MACRO), status=429)
		self.assertEqual(self.rows(), [])

	def test_no_response_or_no_snapshot_records_nothing(self):
		self.call(_path(CREATE_MACRO), response=False)  # v15: response=None after an HTTPException
		self.call(_path(CREATE_MACRO), capture=False)  # before_request never ran
		self.assertEqual(self.rows(), [])

	def test_a_stale_snapshot_never_leaks_into_the_next_request(self):
		self.call(_path(CREATE_MACRO))
		self.assertIsNone(getattr(frappe.local, "jarvis_activity_req", None))
		self.call(_path(CREATE_MACRO), capture=False)
		self.assertEqual(len(self.rows()), 1)  # only the first


class TestPrefilter(Base):
	"""A Desk request or a signed-out one costs the hook no import and no query."""

	def _spied(self, path, **kw):
		with (
			patch("frappe.get_attr") as get_attr,
			patch.object(activity_actions, "_resolve") as resolve,
			patch.object(activity_log, "record") as record,
			patch.object(frappe.local.db, "sql") as sql,
			patch.object(frappe.local.db, "exists") as exists,
			patch.object(frappe.local.db, "get_value") as get_value,
		):
			self.call(path, **kw)
		return get_attr, resolve, record, sql, exists, get_value

	def test_a_desk_request_triggers_no_get_attr_and_no_query(self):
		get_attr, resolve, record, sql, exists, get_value = self._spied(
			"/api/method/frappe.desk.search.search_link"
		)
		for spy in (get_attr, resolve, record, exists, get_value):
			spy.assert_not_called()
		# the only SQL is the test harness's own rollback bookkeeping, never the hook's
		self.assertEqual([c for c in sql.call_args_list if "tabJarvis" in str(c)], [])

	def test_a_401_request_triggers_no_get_attr_and_no_query(self):
		get_attr, resolve, record, sql, exists, get_value = self._spied(_path(CREATE_MACRO), status=401)
		for spy in (get_attr, resolve, record, exists, get_value):
			spy.assert_not_called()

	def test_an_upload_without_the_header_is_dropped_before_resolution(self):
		get_attr, resolve, record, *_ = self._spied("/api/method/upload_file")
		for spy in (get_attr, resolve, record):
			spy.assert_not_called()

	def test_a_skipped_jarvis_call_resolves_but_never_queries(self):
		get_attr, resolve, record, sql, exists, get_value = self._spied(_path(LIST_CONVERSATIONS))
		resolve.assert_called_once()
		for spy in (record, exists, get_value):
			spy.assert_not_called()

	def test_capture_request_does_no_import_and_no_query(self):
		frappe.local.request = _Req(_path(CREATE_MACRO))
		frappe.local.form_dict = frappe._dict()
		with (
			patch("frappe.get_attr") as get_attr,
			patch.object(frappe.local.db, "sql") as sql,
			patch("frappe.modules.utils.load_doctype_module") as load,
		):
			activity_actions.capture_request()
		get_attr.assert_not_called()
		sql.assert_not_called()
		load.assert_not_called()
		self.assertEqual(frappe.local.jarvis_activity_req.method, CREATE_MACRO)


# --------------------------------------------------------------------------- #
# outcome
# --------------------------------------------------------------------------- #
class TestOutcome(Base):
	def test_403_is_denied_with_the_error_class_only(self):
		self.call(_path(CREATE_MACRO), status=403, exc_type="PermissionError")
		row = self.one()
		self.assertEqual((row.outcome, row.code), ("denied", "PermissionError"))
		self.assertEqual(row.row_count, 0)

	def test_other_4xx_and_5xx_are_failed(self):
		# Three different endpoints: an identical failed call inside five minutes is one row (D3).
		# 404 is no longer an example: the function never ran, so it is not recorded at all.
		self.call(_path(CREATE_MACRO), status=417, exc_type="ValidationError")
		self.call(_path(CLEAR_HISTORY), status=500)
		self.call(_path(RUN_MACRO), status=502, exc_type="free text, not a class!")
		rows = self.rows()
		self.assertEqual([r.outcome for r in rows], ["failed"] * 3)
		self.assertEqual([r.code for r in rows], ["ValidationError", "HTTP 500", "HTTP 502"])

	def test_a_failed_request_keeps_its_own_writes_rolled_back(self):
		def run():
			frappe.get_doc({"doctype": "ToDo", "description": "actact-rolled-back"}).insert(
				ignore_permissions=True
			)

		self.call(_path(CREATE_MACRO), status=417, exc_type="ValidationError", handler=run)
		self.assertEqual(self.one().outcome, "failed")
		self.assertFalse(frappe.db.exists("ToDo", {"description": "actact-rolled-back"}))

	def test_a_committed_request_keeps_its_writes(self):
		def run():
			frappe.get_doc({"doctype": "ToDo", "description": "actact-kept"}).insert(ignore_permissions=True)
			frappe.db.commit()  # Frappe commits a 2xx POST before after_request

		self.call(_path(CREATE_MACRO), handler=run)
		self.assertEqual(self.one().outcome, "ok")
		self.assertTrue(frappe.db.exists("ToDo", {"description": "actact-kept"}))

	def test_a_2xx_with_an_ok_false_envelope_is_failed(self):
		body = json.dumps({"message": {"ok": False, "error": {"code": "FilterRequired", "message": "x"}}})
		self.call(_path(CREATE_MACRO), body=body)
		row = self.one()
		self.assertEqual((row.outcome, row.code), ("failed", "FilterRequired"))

	def test_an_ok_false_envelope_without_a_code_is_still_failed(self):
		self.call(_path(CREATE_MACRO), body=json.dumps({"message": {"ok": False, "reason": "no"}}))
		row = self.one()
		self.assertEqual((row.outcome, row.code), ("failed", "ok_false"))

	def test_the_v2_data_envelope_counts_too(self):
		self.call(f"/api/v2/method/{CREATE_MACRO}", body=json.dumps({"data": {"ok": False}}))
		self.assertEqual(self.one().outcome, "failed")

	def test_the_error_message_is_never_stored(self):
		body = json.dumps({"message": {"ok": False, "error": {"code": "boom", "message": SECRET}}})
		self.call(_path(CREATE_MACRO), body=body)
		self.assertNotIn(SECRET, json.dumps(self.one(), default=str))

	def test_an_unsafe_error_code_is_dropped(self):
		body = json.dumps(
			{"message": {"ok": False, "error": {"code": f"free text {SECRET}!!", "message": ""}}}
		)
		self.call(_path(CREATE_MACRO), body=body)
		row = self.one()
		self.assertEqual(row.code, "ok_false")
		self.assertNotIn(SECRET, json.dumps(row, default=str))

	def test_ok_true_and_other_ok_values_are_ok(self):
		for message in ({"ok": True}, {"ok": None}, {"ok": 0}, {"rows": []}, [1, 2], "ok", None):
			_reset_flood_state()  # seven identical ok calls would otherwise dedupe to one (D3)
			self.call(_path(CREATE_MACRO), body=json.dumps({"message": message}))
		self.assertEqual({r.outcome for r in self.rows()}, {"ok"})
		self.assertEqual(len(self.rows()), 7)

	def test_a_large_or_non_json_body_is_not_read(self):
		huge = json.dumps({"message": {"ok": False, "pad": "x" * 70000}})
		self.call(_path(CREATE_MACRO), body=huge)
		self.call(_path(CREATE_MACRO), body='{"message": {"ok": false}}', mimetype="text/html")
		self.call(_path(CREATE_MACRO), body="not json {ok: false")
		self.assertEqual({r.outcome for r in self.rows()}, {"ok"})

	def test_a_redirect_is_ok(self):
		self.call(_path(CREATE_MACRO), status=302)
		self.assertEqual(self.one().outcome, "ok")


# --------------------------------------------------------------------------- #
# what is stored: the id param only, only when it exists
# --------------------------------------------------------------------------- #
class TestRefs(Base):
	def test_the_declared_id_param_is_stored_when_the_record_exists(self):
		self.call(_path(ARCHIVE), form={"conversation": self.conv})
		row = self.one()
		self.assertEqual((row.ref_doctype, row.ref_name), ("Jarvis Conversation", self.conv))

	def test_a_missing_record_leaves_ref_name_blank(self):
		self.call(_path(ARCHIVE), form={"conversation": "no-such-conversation"})
		row = self.one()
		self.assertEqual((row.ref_doctype, row.ref_name), ("Jarvis Conversation", ""))

	def test_a_non_string_or_oversized_id_is_ignored(self):
		for value in ([self.conv], {"a": 1}, 7, None, "", "x" * 300):
			self.call(_path(ARCHIVE), form={"conversation": value})
		self.assertEqual({r.ref_name for r in self.rows()}, {""})

	def test_an_extra_name_key_is_not_a_ref(self):
		# CREATE_MACRO declares no id param: whatever else the caller sent is not stored.
		self.call(_path(CREATE_MACRO), form={"name": self.conv, "conversation": self.conv, "x": SECRET})
		row = self.one()
		self.assertEqual((row.ref_doctype, row.ref_name), ("", ""))
		self.assertNotIn(SECRET, json.dumps(row, default=str))

	def test_a_name_key_is_not_a_ref_when_the_entry_declares_no_id(self):
		# An entry that knows the DocType from the request but declares no id param stores
		# metadata only, so even an EXISTING record named in `name` is not stored. (LOAD_DOC
		# itself declares `name` since D2, so the entry is swapped for one that does not.)
		todo = frappe.get_doc({"doctype": "ToDo", "description": "actact-existing"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		no_id = ("LOGGED", None, "read", "@doctype", 1)
		with patch.dict(activity_actions.CLASSIFICATION, {LOAD_DOC: no_id}):
			self.call(_path(LOAD_DOC), form={"doctype": "ToDo", "name": todo.name})
		row = self.one()
		self.assertEqual((row.ref_doctype, row.ref_name), ("ToDo", ""))

	def test_no_argument_other_than_the_id_is_stored_anywhere(self):
		form = {"conversation": self.conv, "title": SECRET, "body": SECRET, "filters": SECRET}
		self.call(_path(ARCHIVE), form=form)
		self.assertNotIn(SECRET, json.dumps(self.rows(), default=str))

	def test_ref_name_is_the_database_spelling(self):
		self.call(_path(ARCHIVE), form={"conversation": self.conv.upper()})
		self.assertIn(self.one().ref_name, ("", self.conv))  # never the caller's casing as typed

	def test_the_ref_doctype_param_must_name_a_real_doctype(self):
		self.call(_path(LOAD_DOC), form={"doctype": "ToDo", "name": "TD-1"})
		_reset_flood_state()  # the last two resolve to the same blank reference: one row otherwise
		self.call(_path(LOAD_DOC), form={"doctype": f"Customer {SECRET}", "name": "x"})
		_reset_flood_state()
		self.call(_path(LOAD_DOC), form={"doctype": ["ToDo"], "name": "x"})
		rows = self.rows()
		self.assertEqual([r.ref_doctype for r in rows], ["ToDo", "", ""])
		self.assertNotIn(SECRET, json.dumps(rows, default=str))


# --------------------------------------------------------------------------- #
# D15 reads and downloads
# --------------------------------------------------------------------------- #
class TestReadsAndExports(Base):
	def test_a_document_load_is_a_read_of_one_row(self):
		self.call(_path(LOAD_DOC), form={"doctype": "ToDo", "name": "TD-1"})
		row = self.one()
		self.assertEqual((row.event, row.ref_doctype, row.row_count), ("read", "ToDo", 1))
		self.assertEqual(row.ref_name, "")  # a name that is no record of the doctype is never stored
		self.assertEqual(row.label, "Opened a document")

	def test_a_document_load_carries_the_record_it_opened(self):
		# D2: "Opened a document" without WHICH document answers nothing an admin asks.
		todo = frappe.get_doc({"doctype": "ToDo", "description": "actact-opened"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		self.assertEqual(activity_actions.CLASSIFICATION[LOAD_DOC], ("LOGGED", "name", "read", "@doctype", 1))
		self.call(_path(LOAD_DOC), form={"doctype": "ToDo", "name": todo.name})
		row = self.one()
		self.assertEqual((row.ref_doctype, row.ref_name), ("ToDo", todo.name))

	def test_a_document_load_stores_no_name_for_a_doctype_that_is_not_one(self):
		self.call(_path(LOAD_DOC), form={"doctype": "Not A Real Doctype", "name": "x"})
		row = self.one()
		self.assertEqual((row.ref_doctype, row.ref_name), ("", ""))

	def test_a_refused_read_counts_no_rows(self):
		self.call(_path(LOAD_DOC), form={"doctype": "ToDo", "name": "TD-1"}, status=403)
		row = self.one()
		self.assertEqual((row.event, row.outcome, row.row_count), ("read", "denied", 0))

	def test_dashboard_and_payment_calendar_data_reads_are_logged(self):
		keys = [
			"jarvis.chat.dashboards_api.run_dashboard_source",
			"jarvis.chat.dashboards_api.preview_dashboard_source",
			"jarvis.payment_calendar.api.request_snapshot",
			"jarvis.payment_calendar.api.request_account_balances",
			"jarvis.payment_calendar.api.get_day_detail",
			"jarvis.payment_calendar.api.get_bucket_detail",
			"jarvis.payment_calendar.api.get_upcoming",
		]
		for key in keys:
			self.call(_path(key))
		rows = self.rows()
		self.assertEqual([r.method for r in rows], keys)
		self.assertEqual({r.event for r in rows}, {"read"})

	def test_an_admin_viewing_another_users_macro_or_skill_is_a_read_with_its_ref(self):
		macro = frappe.get_doc({"doctype": "ToDo", "description": "actact-stand-in"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		entries = {
			"jarvis.chat.macros_admin_api.admin_get_macro": "Jarvis Macro",
			"jarvis.chat.skills_admin_api.admin_get_skill": "Jarvis Custom Skill",
		}
		for key, doctype in entries.items():
			self.assertEqual(activity_actions.CLASSIFICATION[key], ("LOGGED", "name", "read", doctype, 1))
			self.call(_path(key), form={"name": "no-such-record"})
		rows = self.rows()
		self.assertEqual(
			sorted((r.method, r.event, r.ref_doctype, r.ref_name) for r in rows),
			sorted((k, "read", d, "") for k, d in entries.items()),
		)
		# the id is stored only when it names a real record of the declared doctype
		with patch.dict(
			activity_actions.CLASSIFICATION,
			{"jarvis.chat.macros_admin_api.admin_get_macro": ("LOGGED", "name", "read", "ToDo", 1)},
		):
			self.call(_path("jarvis.chat.macros_admin_api.admin_get_macro"), form={"name": macro.name})
		self.assertIn(macro.name, [r.ref_name for r in self.rows()])

	def test_ui_metadata_lookups_stay_unlogged(self):
		for key in (
			"jarvis.chat.api.get_doctype_fields",
			"jarvis.chat.actions_api.get_doctype_form_meta",
			"jarvis.chat.docmeta_api.get_docmeta",
			"jarvis.chat.user_settings_api.get_my_settings",
			"jarvis.payment_calendar.api.get_preferences",
			"jarvis.payment_calendar.api.get_snapshot_status",
		):
			self.call(_path(key))
		self.assertEqual(self.rows(), [])

	def test_a_download_is_an_export_event(self):
		self.call(_path(DOWNLOAD), method="GET", form={"conversation": self.conv}, mimetype="text/markdown")
		row = self.one()
		self.assertEqual((row.event, row.method), ("export", DOWNLOAD))
		self.assertEqual(row.ref_name, self.conv)


# --------------------------------------------------------------------------- #
# uploads (stock Frappe upload_file): only with the SPA/PWA header
# --------------------------------------------------------------------------- #
class TestUploads(Base):
	ROUTES = (
		"/api/method/upload_file",
		"/api/method/frappe.handler.upload_file",
		"/api/v2/method/upload_file",
	)

	def test_an_upload_with_the_header_is_one_action_row(self):
		for path in self.ROUTES:
			_reset_flood_state()  # the same upload three ways would otherwise dedupe to one row
			self.call(path, headers={"X-Jarvis-Client": "desktop"})
		rows = self.rows()
		self.assertEqual(len(rows), 3)
		for row in rows:
			self.assertEqual(
				(row.event, row.method, row.label, row.outcome, row.channel, row.actor),
				("action", "frappe.handler.upload_file", "Uploaded a file", "ok", "desktop", USER),
			)

	def test_the_pwa_header_sets_the_channel(self):
		self.call("/api/method/upload_file", headers={"X-Jarvis-Client": "pwa"})
		self.assertEqual(self.one().channel, "pwa")

	def test_an_upload_without_the_header_is_not_jarvis(self):
		for path in self.ROUTES:
			self.call(path)
		self.assertEqual(self.rows(), [])

	def test_a_forged_header_value_is_not_the_shim(self):
		for value in ("", "mobile", "desktop,pwa", "DESKTOPS"):
			self.call("/api/method/upload_file", headers={"X-Jarvis-Client": value})
		self.assertEqual(self.rows(), [])

	def test_the_header_is_case_insensitive_in_value(self):
		self.call("/api/method/upload_file", headers={"X-Jarvis-Client": " Desktop "})
		self.assertEqual(self.one().channel, "desktop")

	def test_a_jarvis_alias_of_upload_file_still_needs_the_header(self):
		alias = types.ModuleType("jarvis._activity_upload_probe")
		alias.send = frappe.handler.upload_file
		sys.modules[alias.__name__] = alias
		self.addCleanup(sys.modules.pop, alias.__name__, None)
		path = _path("jarvis._activity_upload_probe.send")
		self.call(path)
		self.assertEqual(self.rows(), [])
		self.call(path, headers={"X-Jarvis-Client": "pwa"})
		self.assertEqual(self.one().method, "frappe.handler.upload_file")

	def test_a_failed_upload_is_recorded_as_failed(self):
		self.call("/api/method/upload_file", headers={"X-Jarvis-Client": "desktop"}, status=417)
		self.assertEqual(self.one().outcome, "failed")

	def test_a_guest_upload_is_not_recorded(self):
		self.call("/api/method/upload_file", headers={"X-Jarvis-Client": "desktop"}, user="Guest")
		self.assertEqual(self.rows(), [])

	def test_another_frappe_method_with_the_header_is_not_logged(self):
		self.call("/api/method/frappe.client.get_list", headers={"X-Jarvis-Client": "desktop"})
		self.assertEqual(self.rows(), [])


# --------------------------------------------------------------------------- #
# registry shape and labels
# --------------------------------------------------------------------------- #
class TestRegistry(Base):
	def test_every_key_resolves_to_its_own_canonical_whitelisted_function(self):
		# The runtime half of the AST contract test: `fn.__module__ + "." + fn.__qualname__`
		# must equal the key for every classified endpoint (a decorator that forgets
		# functools.wraps would move the key and the endpoint would silently go unlogged).
		bad = []
		for key in activity_actions.CLASSIFICATION:
			fn = frappe.get_attr(key)
			if activity_actions._canonical(fn) != key or fn not in frappe.whitelisted:
				bad.append(key)
		self.assertEqual(bad, [])

	def test_every_logged_endpoint_records_exactly_one_row(self):
		logged = [k for k, v in activity_actions.CLASSIFICATION.items() if v[0] == "LOGGED"]
		self.assertGreater(len(logged), 150)
		# one person calling every endpoint in a minute is far over the real cap (D3)
		with patch.object(activity_actions, "ACTOR_CAP_PER_MINUTE", 10**6):
			for key in logged:
				self.call(_path(key))
		self.assertEqual([r.method for r in self.rows()], logged)
		events = {r.event for r in self.rows()}
		self.assertEqual(events, {"action", "read", "export"})

	def test_label_is_the_humanised_name_unless_overridden(self):
		self.assertEqual(
			activity_actions.label_for("jarvis.chat.api.archive_conversation"), "Archive a conversation"
		)
		self.assertEqual(activity_actions.label_for("jarvis.chat.wiki.save_wiki_page"), "Save wiki page")
		self.assertEqual(activity_actions.label_for("jarvis.x._ops.operator_fail"), "Operator fail")
		self.call(_path("jarvis.chat.wiki.save_wiki_page"))
		self.assertEqual(self.one().label, "Save wiki page")

	def test_every_override_names_a_logged_endpoint(self):
		for key in activity_actions.LABEL_OVERRIDES:
			self.assertEqual(activity_actions.CLASSIFICATION[key][0], "LOGGED", key)

	def test_no_em_dash_in_labels(self):
		for label in activity_actions.LABEL_OVERRIDES.values():
			self.assertNotIn(chr(0x2014), label)


# --------------------------------------------------------------------------- #
# never raises, never costs the user's call
# --------------------------------------------------------------------------- #
class TestNeverRaises(Base):
	def test_an_insert_failure_is_swallowed(self):
		with patch.object(activity_log, "record", side_effect=RuntimeError(SECRET)):
			self.call(_path(CREATE_MACRO))
		self.assertEqual(self.rows(), [])

	def test_a_resolver_failure_goes_to_the_file_logger_only(self):
		activity_log._reset_failure_log()
		self.addCleanup(activity_log._reset_failure_log)
		with (
			patch.object(activity_actions, "_resolve", side_effect=RuntimeError(SECRET)),
			patch.object(activity_log, "_fail") as fail,
		):
			self.call(_path(CREATE_MACRO))
		fail.assert_called_once()
		# activity_log._fail logs the exception CLASS and raise site, never its message
		self.assertEqual(fail.call_args.args[0], "request not recorded")
		self.assertFalse(frappe.db.exists("Error Log", {"error": ("like", f"%{SECRET}%")}))

	def test_a_broken_response_object_is_swallowed(self):
		broken = types.SimpleNamespace(status_code="not a number")
		frappe.set_user(USER)
		frappe.local.request = _Req(_path(CREATE_MACRO))
		frappe.local.form_dict = frappe._dict()
		activity_actions.capture_request()
		activity_actions.record_request(response=broken, request=frappe.local.request)
		self.assertEqual(self.rows(), [])

	def test_capture_never_raises_on_odd_state(self):
		frappe.local.request = None
		activity_actions.capture_request()
		frappe.local.request = _Req(None)  # path None
		frappe.local.form_dict = None
		activity_actions.capture_request()
		frappe.local.request = _Req(_path(CREATE_MACRO))
		frappe.local.form_dict = frappe._dict(cmd=["a", "list"])
		activity_actions.capture_request()
		self.assertIsNone(frappe.local.jarvis_activity_req)

	def test_a_missing_table_does_not_fail_the_call(self):
		with patch.object(frappe.local.db, "table_exists", return_value=False):
			self.call(_path(CREATE_MACRO))
		self.assertEqual(self.rows(), [])


# --------------------------------------------------------------------------- #
# end to end: the real WSGI app proves both hooks are registered
# --------------------------------------------------------------------------- #
class TestHooksAreRegistered(FrappeAPITestCase):
	PROBE = "jarvis.chat.sharing.get_sharing_setting"  # harmless; SKIPPED in the shipped registry

	def setUp(self):
		super().setUp()
		frappe.db.delete(DT)
		frappe.db.commit()
		_reset_flood_state()  # a probe call an earlier test (or run) made would dedupe this one

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.delete(DT)
		frappe.db.commit()
		_reset_flood_state()
		super().tearDown()

	def test_the_hooks_are_declared_in_order(self):
		self.assertIn("jarvis.activity_actions.capture_request", frappe.get_hooks("before_request"))
		after = frappe.get_hooks("after_request")
		self.assertIn("jarvis.activity_actions.record_request", after)
		self.assertLess(
			after.index("jarvis.api_errors.capture_failed_request"),
			after.index("jarvis.activity_actions.record_request"),
		)

	def _event_rows(self):
		frappe.db.rollback()  # the request ran on its own connection
		return frappe.get_all(
			DT, fields=["method", "event", "outcome", "actor", "channel"], order_by="outcome"
		)

	def test_a_real_request_records_one_row_and_a_skipped_one_none(self):
		# Not LOGGED today: no row. Same request with the probe registered: exactly one.
		self.post(self.method(self.PROBE), {"sid": self.sid})
		self.assertEqual(self._event_rows(), [])
		entry = ("LOGGED", None, "action", None, 0)
		with patch.dict(activity_actions.CLASSIFICATION, {self.PROBE: entry}):
			response = self.post(self.method(self.PROBE), {"sid": self.sid})
		self.assertEqual(response.status_code, 200, response.text)
		rows = self._event_rows()
		self.assertEqual(
			[(r.method, r.event, r.outcome, r.actor) for r in rows],
			[(self.PROBE, "action", "ok", "Administrator")],
		)

	def test_the_cmd_route_and_a_failed_call_through_the_real_app(self):
		entry = ("LOGGED", None, "action", None, 0)
		with patch.dict(activity_actions.CLASSIFICATION, {self.PROBE: entry}):
			self.post(self.method("ping"), {"sid": self.sid, "cmd": self.PROBE})
			self.get(self.method(self.PROBE), {"sid": self.sid})  # POST-only endpoint: 403
		rows = self._event_rows()
		self.assertEqual([(r.method, r.outcome) for r in rows], [(self.PROBE, "denied"), (self.PROBE, "ok")])


# --------------------------------------------------------------------------- #
# D1: a held change decided on someone else's behalf is an action by the decider
# --------------------------------------------------------------------------- #
HELD_DECISIONS = {
	"jarvis.chat.approvals_api.decide_held_action": "Decided a held change",
	"jarvis.chat.approvals_api.apply_sheet": "Applied a sheet",
	"jarvis.chat.approvals_api.discard_sheet": "Discarded a sheet",
}
SM = "actact-sm@example.com"


class TestHeldDecisions(Base):
	def _held_row(self):
		# A raw insert: the doctype refuses ORM writes and seals a call on insert, neither of
		# which matters to a row this test only needs to EXIST and be owned by someone else.
		name = f"actact-held-{frappe.generate_hash(length=8)}"
		frappe.db.sql(
			"INSERT INTO `tabJarvis Pending Action` "
			"(name, creation, modified, modified_by, owner, docstatus, idx, kind, status, owner_user, exec_user) "
			"VALUES (%s, NOW(), NOW(), 'Administrator', 'Administrator', 0, 0, 'file_box_held', 'Pending', %s, %s)",
			(name, OTHER, OTHER),
		)
		frappe.db.commit()
		self.addCleanup(self._drop, name)
		return name

	def _drop(self, name):
		frappe.db.delete("Jarvis Pending Action", {"name": name})
		frappe.db.commit()

	def _sm(self):
		if not frappe.db.exists("User", SM):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": SM,
					"first_name": "actactsm",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		frappe.get_doc("User", SM).add_roles("System Manager")
		frappe.db.commit()

	def test_the_deciders_are_logged_actions_on_a_pending_action(self):
		for key, label in HELD_DECISIONS.items():
			self.assertEqual(
				activity_actions.CLASSIFICATION[key],
				("LOGGED", "name", "action", "Jarvis Pending Action", 0),
			)
			self.assertEqual(activity_actions.LABEL_OVERRIDES[key], label)

	def test_edit_and_create_stays_on_the_tool_path(self):
		# It creates the record AS the approver, through the tool path, which records it.
		self.assertEqual(
			activity_actions.CLASSIFICATION["jarvis.chat.approvals_api.edit_and_create_held"],
			("SKIPPED", "tool_path"),
		)

	def test_a_system_manager_deciding_another_users_held_row_is_recorded_as_them(self):
		from jarvis.chat import approvals_api

		self._sm()
		name = self._held_row()
		for key, label in HELD_DECISIONS.items():
			_reset_flood_state()  # same actor, same row, three verbs
			frappe.db.delete(DT)
			frappe.db.commit()

			def decide():
				# the framework's own rule: an SM may act on a row another user owns
				self.assertTrue(approvals_api._authorized_held(name, SM))

			self.call(_path(key), form={"name": name}, user=SM, handler=decide)
			row = self.one()
			self.assertEqual(
				(row.actor, row.event, row.method, row.label, row.ref_doctype, row.ref_name, row.outcome),
				(SM, "action", key, label, "Jarvis Pending Action", name, "ok"),
				key,
			)


# --------------------------------------------------------------------------- #
# D4: classification of the endpoints the review reclassified
# --------------------------------------------------------------------------- #
class TestReclassified(Base):
	def test_the_connection_sync_poll_is_skipped(self):
		key = "jarvis.onboarding.sync_connection"
		self.assertEqual(activity_actions.CLASSIFICATION[key], ("SKIPPED", "poll"))
		self.call(_path(key))
		self.assertEqual(self.rows(), [])

	def test_changing_my_settings_is_a_logged_action(self):
		# the auto-mode default is security relevant: it pre-arms every new chat
		key = "jarvis.chat.user_settings_api.update_my_settings"
		self.assertEqual(activity_actions.CLASSIFICATION[key], ("LOGGED", None, "action", None, 0))
		self.call(_path(key), form={"default_auto_mode": 1})
		self.assertEqual(self.one().method, key)

	def test_reorder_and_count_plan_data_reads_are_logged_reads(self):
		expect = {
			"jarvis.chat.reorder_review.get_review": ("LOGGED", "name", "read", "Jarvis Agent Run", 1),
			"jarvis.chat.reorder_review.list_reviews": ("LOGGED", None, "read", None, 0),
			"jarvis.chat.cycle_count_entry.get_count_plan": (
				"LOGGED",
				"request",
				"read",
				"Jarvis Approval Request",
				1,
			),
		}
		for key, entry in expect.items():
			self.assertEqual(activity_actions.CLASSIFICATION[key], entry, key)
			self.call(_path(key))
		self.assertEqual({r.event for r in self.rows()}, {"read"})
		self.assertEqual(len(self.rows()), 3)


# --------------------------------------------------------------------------- #
# D3: flood control. (a) calls that never ran, (b) dedupe, (c) per-person cap; all fail open
# --------------------------------------------------------------------------- #
def _logged_keys(n):
	return [k for k, v in activity_actions.CLASSIFICATION.items() if v[0] == "LOGGED"][:n]


def _read_keys(n):
	return [k for k, v in activity_actions.CLASSIFICATION.items() if v[0] == "LOGGED" and v[2] == "read"][:n]


READ = "jarvis.payment_calendar.api.get_upcoming"  # LOGGED read, no id
DELETE_PAGE = "jarvis.chat.wiki.delete_wiki_page"  # LOGGED action, no id


class TestFloodControl(Base):
	def test_a_call_that_never_ran_is_not_recorded(self):
		# 400/404/405/415: the function was not called (bad request, no such method, wrong verb,
		# wrong media type), so there is no action to attribute and a client can spam them.
		for status in (400, 404, 405, 415):
			self.call(_path(CREATE_MACRO), status=status)
		self.assertEqual(self.rows(), [])
		# a refusal or a failure that DID run is still a row
		self.call(_path(CREATE_MACRO), status=403, exc_type="PermissionError")
		self.call(_path(CREATE_MACRO), status=417, exc_type="ValidationError")
		self.assertEqual([r.outcome for r in self.rows()], ["denied", "failed"])

	def test_an_identical_read_within_five_minutes_is_one_row(self):
		for _ in range(5):
			self.call(_path(READ))
		self.assertEqual(len(self.rows()), 1)
		cache = frappe.cache()
		keys = cache.get_keys(activity_actions.FLOOD_PREFIX + "dedupe:")
		self.assertEqual(len(keys), 1)
		self.assertTrue(0 < cache.ttl(keys[0]) <= 300)
		self.assertEqual(activity_actions.DEDUPE_WINDOW_S, 300)

	def test_the_dedupe_key_covers_actor_method_reference_and_outcome(self):
		todos = [
			frappe.get_doc({"doctype": "ToDo", "description": f"actact-dedupe-{i}"}).insert(
				ignore_permissions=True
			)
			for i in range(2)
		]
		frappe.db.commit()
		t1, t2 = (t.name for t in todos)

		def form(name):
			return {"doctype": "ToDo", "name": name}

		self.call(_path(LOAD_DOC), form=form(t1))
		self.call(_path(LOAD_DOC), form=form(t1))  # identical: dropped
		self.call(_path(LOAD_DOC), form=form(t2))  # another record
		self.call(_path(LOAD_DOC), form=form(t1), user=OTHER)  # another person
		self.call(_path(LOAD_DOC), form=form(t1), status=417)  # another outcome
		self.call(_path(READ))  # another method
		self.assertEqual(
			sorted((r.actor, r.method, r.ref_name, r.outcome) for r in self.rows()),
			sorted(
				[
					(USER, LOAD_DOC, t1, "ok"),
					(USER, LOAD_DOC, t2, "ok"),
					(OTHER, LOAD_DOC, t1, "ok"),
					(USER, LOAD_DOC, t1, "failed"),
					(USER, READ, "", "ok"),
				]
			),
		)

	def test_identical_refused_calls_are_still_deduped(self):
		for _ in range(3):
			self.call(_path(CREATE_MACRO), status=403, exc_type="PermissionError")
			self.call(_path(CREATE_MACRO), status=417, exc_type="ValidationError")
		self.assertEqual(sorted(r.outcome for r in self.rows()), ["denied", "failed"])

	def test_successful_actions_are_never_deduped(self):
		# N1: five real deletes of the same kind in five minutes are five rows.
		for _ in range(5):
			self.call(_path(DELETE_PAGE))
		self.assertEqual(len(self.rows()), 5)
		self.assertEqual(activity_actions.health_counters()["dropped"], 0)
		self.assertEqual(frappe.cache().get_keys(activity_actions.FLOOD_PREFIX + "dedupe:"), [])

	def test_successful_exports_are_never_deduped(self):
		for _ in range(3):
			self.call(
				_path(DOWNLOAD), method="GET", form={"conversation": self.conv}, mimetype="text/markdown"
			)
		self.assertEqual([r.event for r in self.rows()], ["export"] * 3)

	def test_a_burst_of_reads_never_costs_a_change_its_row(self):
		# N1: reads and refusals share the cap; a successful action neither spends it nor waits on it.
		reads = _read_keys(5)
		with patch.object(activity_actions, "ACTOR_CAP_PER_MINUTE", 3):
			for key in reads:
				self.call(_path(key))  # five distinct reads: the last two are over the cap of three
			self.call(_path(DELETE_PAGE))
			self.call(_path(CREATE_MACRO))
		rows = self.rows()
		self.assertEqual([r.method for r in rows if r.event == "read"], reads[:3])
		self.assertEqual([r.method for r in rows if r.event == "action"], [DELETE_PAGE, CREATE_MACRO])

	def test_the_real_cap_is_120_reads_a_minute_and_a_delete_after_them_is_recorded(self):
		self.assertEqual(activity_actions.ACTOR_CAP_PER_MINUTE, 120)
		reads = _read_keys(3)
		clock = 2_000_000.0
		with patch.object(activity_actions, "_epoch", return_value=clock):
			cache = frappe.cache()
			bucket = cache.make_key(
				f"{activity_actions.FLOOD_PREFIX}cap:{activity_actions._digest(USER)}:{int(clock // 60)}"
			)
			cache.set(bucket, 119, ex=120)  # 119 distinct reads already this minute
			self.call(_path(reads[0]))  # the 120th: recorded
			self.call(_path(reads[1]))  # the 121st: dropped
			self.call(_path(DELETE_PAGE))  # a successful delete: recorded
		self.assertEqual([r.method for r in self.rows()], [reads[0], DELETE_PAGE])

	def test_dropped_rows_are_counted_and_log_a_throttled_line(self):
		reads = _read_keys(3)
		with (
			patch.object(activity_actions, "ACTOR_CAP_PER_MINUTE", 1),
			patch.object(activity_log, "_fail") as fail,
		):
			for key in reads:
				self.call(_path(key))  # two over the cap
			self.call(_path(reads[0]))  # a repeat
		self.assertEqual(activity_actions.health_counters()["dropped"], 3)
		self.assertEqual([c.args[0] for c in fail.call_args_list], ["flood control dropped rows"] * 3)
		# ``_fail`` itself logs one line per text per five minutes
		activity_log._reset_failure_log()
		self.addCleanup(activity_log._reset_failure_log)
		with patch.object(activity_log.frappe, "logger") as logger:
			activity_log._fail("flood control dropped rows", event="read")
			activity_log._fail("flood control dropped rows", event="read")
		self.assertEqual(logger.return_value.error.call_count, 1)

	def test_a_row_that_failed_to_insert_does_not_block_the_next_identical_call(self):
		# N7: the dedupe claim is released, or one failed insert hides the next five minutes.
		with patch.object(activity_log, "record", return_value=None):
			self.call(_path(READ))
		self.assertEqual(frappe.cache().get_keys(activity_actions.FLOOD_PREFIX + "dedupe:"), [])
		self.call(_path(READ))
		self.assertEqual(len(self.rows()), 1)

	def test_each_person_is_capped_per_minute(self):
		keys = _read_keys(6)
		with patch.object(activity_actions, "ACTOR_CAP_PER_MINUTE", 3):
			for key in keys:
				self.call(_path(key))  # six distinct calls by one person
			self.call(_path(keys[0]), user=OTHER)  # someone else is not affected
		rows = self.rows()
		self.assertEqual([r.method for r in rows if r.actor == USER], keys[:3])
		self.assertEqual([r.method for r in rows if r.actor == OTHER], [keys[0]])
		self.assertEqual(activity_actions.ACTOR_CAP_PER_MINUTE, 120)

	def test_the_cap_window_is_one_minute(self):
		keys = _read_keys(4)
		clock = [1_000_000.0]
		with (
			patch.object(activity_actions, "ACTOR_CAP_PER_MINUTE", 2),
			patch.object(activity_actions, "_epoch", side_effect=lambda: clock[0]),
		):
			for key in keys[:3]:
				self.call(_path(key))
			clock[0] += 61
			self.call(_path(keys[3]))
		self.assertEqual([r.method for r in self.rows()], [keys[0], keys[1], keys[3]])

	def test_a_repeat_does_not_spend_the_cap(self):
		a, b, c = _read_keys(3)
		with patch.object(activity_actions, "ACTOR_CAP_PER_MINUTE", 2):
			for _ in range(6):
				self.call(_path(a))  # deduped after the first: one slot, not six
			self.call(_path(b))
			self.call(_path(c))  # over the cap of two
		self.assertEqual([r.method for r in self.rows()], [a, b])

	def test_redis_down_still_records_both_checks_fail_open(self):
		down = redis.exceptions.ConnectionError("down")
		with patch.object(activity_actions, "_cache", side_effect=down):
			for _ in range(3):
				self.call(_path(READ))
		self.assertEqual(len(self.rows()), 3)  # no dedupe, no cap, but nothing lost

	def test_a_failing_cap_counter_alone_still_records(self):
		flaky = _Flaky(frappe.cache(), {"pipeline"})
		with patch.object(activity_actions, "_cache", return_value=flaky):
			self.call(_path(READ))
		self.assertEqual(len(self.rows()), 1)

	def test_a_failing_dedupe_check_alone_still_records(self):
		flaky = _Flaky(frappe.cache(), {"set"})
		with patch.object(activity_actions, "_cache", return_value=flaky):
			self.call(_path(READ))
			self.call(_path(READ))
		self.assertEqual(len(self.rows()), 2)

	def test_a_flood_check_failure_is_file_logged(self):
		down = redis.exceptions.ConnectionError(SECRET)
		with (
			patch.object(activity_actions, "_cache", side_effect=down),
			patch.object(activity_log, "_fail") as fail,
		):
			self.call(_path(READ))
		self.assertIn("flood control unavailable", [c.args[0] for c in fail.call_args_list])

	def test_the_keys_are_site_scoped_and_survive_a_cache_clear(self):
		self.call(_path(READ))
		cache = frappe.cache()
		keys = cache.get_keys(activity_actions.FLOOD_PREFIX)
		self.assertTrue(keys)
		for key in keys:
			self.assertTrue(key.startswith(cache.make_key("")), key)
		persistent = frappe.get_hooks("persistent_cache_keys")
		for prefix in (activity_actions.FLOOD_PREFIX, activity_actions.SEEN_PREFIX):
			self.assertIn(prefix, persistent)


# --------------------------------------------------------------------------- #
# D5: the request hooks fail visibly, and a success is never rolled back
# --------------------------------------------------------------------------- #
class TestHookFailures(Base):
	def _hooks(self, status, exc_type=None):
		"""Both hooks for one POST, spying on rollback around the after_request half."""
		frappe.set_user(USER)
		frappe.local.request = _Req(_path(CREATE_MACRO))
		frappe.local.form_dict = frappe._dict()
		frappe.local.response = frappe._dict(exc_type=exc_type) if exc_type else frappe._dict()
		activity_actions.capture_request()
		with patch.object(frappe.local.db, "rollback", wraps=frappe.local.db.rollback) as rollback:
			activity_actions.record_request(
				response=Response("{}", status=status, mimetype="application/json"),
				request=frappe.local.request,
			)
		frappe.set_user("Administrator")
		frappe.db.rollback()
		return rollback

	def test_a_snapshot_failure_is_file_logged_with_its_own_text(self):
		with (
			patch.object(activity_actions, "_method_from", side_effect=RuntimeError(SECRET)),
			patch.object(activity_log, "_fail") as fail,
		):
			self.call(_path(CREATE_MACRO))
		fail.assert_called_once()
		self.assertEqual(fail.call_args.args[0], "request snapshot failed")
		self.assertIsInstance(fail.call_args.args[1], RuntimeError)
		self.assertEqual(self.rows(), [])
		self.assertEqual(activity_actions.health_counters()["failures"], 1)

	def test_the_record_hook_logs_a_different_text(self):
		with (
			patch.object(activity_actions, "_resolve", side_effect=RuntimeError(SECRET)),
			patch.object(activity_log, "_fail") as fail,
		):
			self.call(_path(CREATE_MACRO))
		self.assertEqual(fail.call_args.args[0], "request not recorded")
		self.assertEqual(activity_actions.health_counters()["failures"], 1)

	def test_an_insert_that_writes_nothing_counts_as_a_failure(self):
		with patch.object(activity_log, "record", return_value=None):
			self.call(_path(CREATE_MACRO))
		self.assertEqual(activity_actions.health_counters(), {"seen": 1, "failures": 1, "dropped": 0})

	def test_a_success_is_not_rolled_back(self):
		rollback = self._hooks(200)
		rollback.assert_not_called()
		self.assertEqual(len(self.rows()), 1)

	def test_a_failure_still_rolls_back_what_the_request_left(self):
		rollback = self._hooks(417, "ValidationError")
		rollback.assert_called()
		self.assertEqual(self.one().outcome, "failed")

	def test_a_success_keeps_what_an_earlier_after_request_hook_left_uncommitted(self):
		# Mirrors api_errors.capture_failed_request: our commit is past the request commit, and a
		# rollback here would drop another app's after_request work.
		def run():
			frappe.get_doc({"doctype": "ToDo", "description": "actact-after-hook"}).insert(
				ignore_permissions=True
			)

		self.call(_path(CREATE_MACRO), handler=run)  # status 200, the write was never committed
		self.assertTrue(frappe.db.exists("ToDo", {"description": "actact-after-hook"}))
		self.assertEqual(self.one().outcome, "ok")

	def test_the_prefilter_comment_matches_what_the_code_does(self):
		# The v2 /method/<doctype>/<m> form must load the doctype's controller to learn the
		# name; only the doctype-less forms are free. The text says so instead of claiming "nothing".
		self.assertIn("v2", activity_actions._record.__doc__ or "")


# --------------------------------------------------------------------------- #
# D6: counters the logging-health block reads
# --------------------------------------------------------------------------- #
class TestHealthCounters(Base):
	def test_every_logged_request_is_seen_even_when_dropped(self):
		keys = _read_keys(3)
		with patch.object(activity_actions, "ACTOR_CAP_PER_MINUTE", 1):
			for key in keys:
				self.call(_path(key))  # two of three are over the cap
		self.call(_path(keys[0]))  # a repeat
		self.assertEqual(len(self.rows()), 1)
		self.assertEqual(activity_actions.health_counters(), {"seen": 4, "failures": 0, "dropped": 3})

	def test_a_skipped_endpoint_or_a_call_that_never_ran_is_not_seen(self):
		self.call(_path(LIST_CONVERSATIONS))
		self.call(_path(CREATE_MACRO), status=404)
		self.assertEqual(activity_actions.health_counters(), {"seen": 0, "failures": 0, "dropped": 0})

	def test_counters_are_hourly_and_cover_24_hours(self):
		clock = [10_000 * 3600.0]
		with patch.object(activity_actions, "_epoch", side_effect=lambda: clock[0]):
			self.call(_path(CREATE_MACRO))
			clock[0] += 23 * 3600
			self.assertEqual(activity_actions.health_counters()["seen"], 1)
			clock[0] += 2 * 3600  # 25 hours after the call
			self.assertEqual(activity_actions.health_counters()["seen"], 0)

	def test_counter_keys_expire(self):
		self.call(_path(CREATE_MACRO))
		cache = frappe.cache()
		keys = cache.get_keys(activity_actions.SEEN_PREFIX)
		self.assertTrue(keys)
		for key in keys:
			self.assertTrue(0 < cache.ttl(key) <= 26 * 3600, key)

	def test_a_broken_cache_reads_as_zero_and_never_raises(self):
		with patch.object(activity_actions, "_cache", side_effect=redis.exceptions.ConnectionError("x")):
			self.assertEqual(activity_actions.health_counters(), {"seen": 0, "failures": 0, "dropped": 0})
