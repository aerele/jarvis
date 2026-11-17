"""Activity events written by the tool path (plan 2026-11-06-user-activity-report 4.3).

Drives the real choke points (``_run_tool`` / ``_dispatch_and_wrap`` / ``call_tool``)
with ``jarvis.api.dispatch`` patched, the pattern of test_agent_audit.py and
test_site_pin_call_tool.py. Every assertion diffs against the rows present BEFORE the
action: other suite tests commit activity rows of their own.
"""

import contextlib
import hashlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis._plugin_auth import PluginAuthError
from jarvis.exceptions import InvalidArgumentError
from jarvis.tests.test_api import _FakeRequest

DT = "Jarvis Activity Event"
_PLAIN = "activity-tool-plain@example.com"
_DISABLED = "activity-tool-disabled@example.com"
_SESSION = "agent:test:activity-tool"
_SECRET = "SECRET-MARKER-7731"


def _ensure_user(email, enabled=1):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Act",
				"send_welcome_email": 0,
				"enabled": enabled,
			}
		).insert(ignore_permissions=True)
	return email


def _names(doctype=DT):
	return set(frappe.get_all(doctype, pluck="name"))


def _new(before, **filters):
	return [
		r
		for r in frappe.get_all(DT, filters=filters, fields=["*"], order_by="creation asc")
		if r.name not in before
	]


class ActivityCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.before = _names()
		self.writes_before = _names("Jarvis Agent Write")
		self.addCleanup(self._drop_rows)

	def _drop_rows(self):
		# Keep the site's counts exact for the next test, whatever committed.
		for doctype, before in ((DT, self.before), ("Jarvis Agent Write", self.writes_before)):
			for name in _names(doctype) - before:
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

	def run_tool(self, tool, args, *, returns=None, raises=None):
		kw = {"side_effect": raises} if raises is not None else {"return_value": returns}
		with patch("jarvis.api.dispatch", **kw):
			return api._run_tool(tool, args)


class TestReads(ActivityCase):
	def test_get_list_records_one_read_with_doctype_and_rows_and_no_content(self):
		rows = [{"name": "T-1", "description": _SECRET}, {"name": "T-2", "description": _SECRET}]
		env = self.run_tool(
			"get_list", {"doctype": "ToDo", "filters": {"description": _SECRET}}, returns=rows
		)
		self.assertTrue(env["ok"])
		new = _new(self.before, method="get_list")
		self.assertEqual(len(new), 1)
		row = new[0]
		self.assertEqual((row.event, row.outcome, row.ref_doctype, row.row_count), ("read", "ok", "ToDo", 2))
		self.assertEqual(row.actor, "Administrator")
		self.assertNotIn(_SECRET, repr(row))  # no args, no results, anywhere in the row

	def test_introspection_tool_records_nothing(self):
		self.run_tool("get_schema", {"doctype": "ToDo"}, returns={"fields": []})
		self.assertEqual(_new(self.before), [])

	def test_failed_read_is_recorded_as_failed_with_the_code_only(self):
		env = self.run_tool(
			"get_list", {"doctype": "ToDo"}, raises=InvalidArgumentError(f"bad filter {_SECRET}")
		)
		self.assertFalse(env["ok"])
		new = _new(self.before, method="get_list")
		self.assertEqual(len(new), 1)
		self.assertEqual((new[0].outcome, new[0].code), ("failed", "InvalidArgumentError"))
		self.assertNotIn(_SECRET, repr(new[0]))

	def test_permission_denied_read_is_recorded_as_denied(self):
		env = self.run_tool("get_list", {"doctype": "Error Log"}, raises=frappe.PermissionError("no"))
		self.assertFalse(env["ok"])
		new = _new(self.before, method="get_list")
		self.assertEqual(len(new), 1)
		self.assertEqual((new[0].event, new[0].outcome), ("read", "denied"))
		self.assertEqual(new[0].code, "PermissionDeniedError")
		self.assertEqual(new[0].ref_doctype, "Error Log")

	def test_unexpected_exception_rolls_back_and_records_nothing(self):
		# plan 4.3(6): the envelope-None re-raise path is a full rollback in Frappe.
		with self.assertRaises(RuntimeError):
			self.run_tool("get_list", {"doctype": "ToDo"}, raises=RuntimeError("boom"))
		self.assertEqual(_new(self.before), [])

	def test_unknown_tool_name_is_never_stored_by_the_tool_path(self):
		# The caller's own string (a hallucinated or hostile tool name) reaches the
		# ToolNotFoundError failure branch; the row says unknown_tool.
		name = f"zzz-{_SECRET}"
		env = api._run_tool(name, {"doctype": _SECRET})
		self.assertFalse(env["ok"])
		new = _new(self.before)
		self.assertEqual([(r.event, r.method, r.ref_doctype) for r in new], [("read", "unknown_tool", "")])
		self.assertNotIn("SECRET", repr(new[0]))

	def test_unknown_tool_name_is_never_stored_on_success_either(self):
		env = self.run_tool(f"zzz-{_SECRET}", {"doctype": _SECRET}, returns=[])
		self.assertTrue(env["ok"])
		new = _new(self.before)
		self.assertEqual([(r.method, r.ref_doctype) for r in new], [("unknown_tool", "")])
		self.assertNotIn("SECRET", repr(new[0]))

	def test_failed_read_of_a_made_up_doctype_stores_no_ref(self):
		# A1: the argument is model-supplied free text. A customer's name and phone number
		# pasted into `doctype` must never reach the table.
		for junk in ("Alice 555-1234", f"{_SECRET}@example.com"):
			env = self.run_tool("get_list", {"doctype": junk}, raises=InvalidArgumentError("bad"))
			self.assertFalse(env["ok"])
		rows = _new(self.before, method="get_list")
		self.assertEqual([(r.outcome, r.ref_doctype) for r in rows], [("failed", ""), ("failed", "")])
		self.assertNotIn("Alice", repr(rows))
		self.assertNotIn(_SECRET, repr(rows))

	def test_successful_read_of_a_made_up_doctype_stores_no_ref_either(self):
		self.run_tool("get_list", {"doctype": "Alice 555-1234"}, returns=[])
		self.assertEqual([r.ref_doctype for r in _new(self.before, method="get_list")], [""])

	def test_error_code_is_stored_only_when_it_is_a_plain_identifier(self):
		# A3: the code is "the code only, never the message"; a tool that put free text in
		# `code` must not smuggle it in.
		record = api._record_read_failure
		for raw in (
			f"Alice 555-1234 {_SECRET}",
			"has space",
			"1StartsWithDigit",
			"x" * 65,
			"",
			None,
			42,
			["List"],
		):
			record("get_list", {"doctype": "ToDo"}, {"ok": False, "error": {"code": raw}})
		record("get_list", {"doctype": "ToDo"}, {"ok": False, "error": "a bare string"})
		record("get_list", {"doctype": "ToDo"}, {"ok": False})
		record("get_list", {"doctype": "ToDo"}, {"ok": False, "error": {"code": "ValidationError"}})
		rows = _new(self.before, method="get_list")
		self.assertEqual([r.code for r in rows], ["failed"] * 10 + ["ValidationError"])
		self.assertTrue(all(r.outcome == "failed" for r in rows[:-1]))
		self.assertNotIn(_SECRET, repr(rows))
		self.assertNotIn("Alice", repr(rows))

	def test_a_64_char_identifier_code_is_kept(self):
		code = "E" * 64
		api._record_read_failure("get_list", {}, {"ok": False, "error": {"code": code}})
		self.assertEqual([r.code for r in _new(self.before, method="get_list")], [code])

	def test_connector_read_that_returns_its_own_failure_is_a_failed_read(self):
		# A4: call_connector dispatched (outer ok) but answered {ok: false} in its own
		# envelope (the safe-read carve-out runs it as a read).
		with patch("jarvis.api.dispatch", return_value={"ok": False, "error": "blocked"}):
			env = api._dispatch_and_wrap("call_connector", {"connector": "c", "action": "a"}, is_write=False)
		self.assertTrue(env["ok"])  # the outer envelope: the tool did dispatch
		new = _new(self.before, method="call_connector")
		self.assertEqual([(r.event, r.outcome) for r in new], [("read", "failed")])

	def test_connector_read_that_succeeds_is_an_ok_read(self):
		with patch("jarvis.api.dispatch", return_value={"ok": True, "data": []}):
			api._dispatch_and_wrap("call_connector", {"connector": "c", "action": "a"}, is_write=False)
		self.assertEqual(
			[(r.event, r.outcome) for r in _new(self.before, method="call_connector")], [("read", "ok")]
		)

	def test_activity_target_is_the_one_place_the_privacy_rule_lives(self):
		self.assertEqual(api._activity_target("get_list", {"doctype": "ToDo"}), ("get_list", "ToDo"))
		self.assertEqual(api._activity_target("get_list", {"doctype": "Alice 555-1234"}), ("get_list", ""))
		self.assertEqual(api._activity_target(f"zzz-{_SECRET}", {"doctype": "ToDo"}), ("unknown_tool", ""))
		self.assertEqual(api._activity_target("get_list", None), ("get_list", ""))

	def test_every_recorder_derives_its_target_through_the_helper(self):
		import inspect

		for fn in (
			api._record_tool_activity,
			api._record_read_failure,
			api._record_denied_call,
		):
			src = inspect.getsource(fn)
			self.assertIn("_activity_target(", src, fn.__name__)
			self.assertNotIn("ref_doctype_of(", src, fn.__name__)
			self.assertNotIn("_known_tool(", src, fn.__name__)

	def test_activity_session_actor_reads_the_session_row_without_side_effects(self):
		# A6: no resolve_identity (it frappe.throws, leaving a message in the response).
		messages = list(frappe.local.message_log)
		with patch("jarvis.chat.collaboration.resolve_identity", side_effect=AssertionError("not this")):
			self.assertEqual(api._activity_session_actor("agent:no-such-session-key"), "")
			with patch("frappe.db.get_value", return_value="someone@example.com") as get_value:
				self.assertEqual(api._activity_session_actor("agent:k"), "someone@example.com")
		self.assertEqual(get_value.call_args.args[0], "Jarvis Chat Session")
		self.assertEqual(get_value.call_args.args[1], {"session_key": "agent:k"})
		self.assertEqual(list(frappe.local.message_log), messages)

	def test_activity_session_actor_swallows_a_lookup_error(self):
		with patch("frappe.db.get_value", side_effect=RuntimeError("db down")):
			self.assertEqual(api._activity_session_actor("agent:k"), "")


class TestExportsAndWrites(ActivityCase):
	def _agent_writes(self, tool, before):
		return [
			r
			for r in frappe.get_all("Jarvis Agent Write", filters={"tool": tool}, fields=["name", "outcome"])
			if r.name not in before
		]

	def test_immediate_export_query_is_one_agent_write_and_one_export(self):
		writes_before = _names("Jarvis Agent Write")
		env = self.run_tool(
			"export_query",
			{"doctype": "ToDo"},
			returns={"file_url": "/private/files/x.xlsx", "filename": "x.xlsx", "total": 7},
		)
		self.assertTrue(env["ok"])
		self.assertEqual(len(self._agent_writes("export_query", writes_before)), 1)
		new = _new(self.before)
		self.assertEqual([r.event for r in new], ["export"])
		self.assertEqual((new[0].method, new[0].row_count, new[0].ref_doctype), ("export_query", 7, "ToDo"))
		self.assertEqual(_new(self.before, event="read"), [])

	def test_export_query_handed_to_a_background_job_records_no_export(self):
		writes_before = _names("Jarvis Agent Write")
		env = self.run_tool(
			"export_query",
			{"doctype": "ToDo"},
			returns={"export_job": "JEJ-1", "status": "queued", "rows_written": 0},
		)
		self.assertTrue(env["ok"])
		self.assertEqual(len(self._agent_writes("export_query", writes_before)), 1)  # unchanged
		self.assertEqual(_new(self.before), [])

	def test_export_result_without_a_file_url_records_no_export(self):
		# A7: only an immediate success (a delivered file) is an export; an unrecognised
		# shape is not guessed at.
		for returned in ({"filename": "x.xlsx", "total": 3}, {"total": 3}, {}, [], "ok"):
			self.run_tool("export_query", {"doctype": "ToDo"}, returns=returned)
		self.assertEqual(_new(self.before), [])

	def test_each_export_tool_with_a_file_url_records_one_export(self):
		for tool in ("export_excel", "export_document", "report_pdf", "download_pdf"):
			with patch("jarvis.api.dispatch", return_value={"file_url": "/private/files/x", "filename": "x"}):
				api._dispatch_and_wrap(tool, {}, is_write=True)
		self.assertEqual(
			sorted(r.method for r in _new(self.before, event="export")),
			["download_pdf", "export_document", "export_excel", "report_pdf"],
		)

	def test_prepared_report_status_envelope_records_no_export(self):
		self.run_tool("export_query", {"report_name": "R"}, returns={"status": "generating"})
		self.assertEqual(_new(self.before), [])

	def test_refused_export_is_not_an_export_event(self):
		env = self.run_tool("export_query", {"doctype": "ToDo"}, raises=frappe.ValidationError("no"))
		self.assertFalse(env["ok"])
		self.assertEqual(_new(self.before), [])

	def test_plain_write_has_an_agent_write_row_and_no_activity_row(self):
		# create_doc parks a confirmation card in _run_tool; the choke point is
		# _dispatch_and_wrap, as test_agent_audit drives it.
		writes_before = _names("Jarvis Agent Write")
		with patch("jarvis.api.dispatch", return_value={"name": "T-9"}):
			env = api._dispatch_and_wrap(
				"create_doc", {"doctype": "ToDo", "description": _SECRET}, is_write=True
			)
		self.assertTrue(env["ok"])
		self.assertEqual(len(self._agent_writes("create_doc", writes_before)), 1)
		self.assertEqual(_new(self.before), [])

	def test_failed_write_records_no_activity_row(self):
		with patch("jarvis.api.dispatch", side_effect=frappe.ValidationError("x")):
			env = api._dispatch_and_wrap("update_doc", {"doctype": "ToDo", "name": "T-1"}, is_write=True)
		self.assertFalse(env["ok"])
		self.assertEqual(_new(self.before), [])


class TestPreDispatchRefusals(ActivityCase):
	def _shared(self):
		return (
			patch("jarvis.chat.collaboration.is_collaborative", return_value=True),
			patch("jarvis.chat.collaboration_callbacks.permitted_callback", return_value=MagicMock()),
		)

	def test_shared_chat_without_a_permit_is_a_denied_call(self):
		with (
			patch("jarvis.chat.collaboration.is_collaborative", return_value=True),
			patch("jarvis.chat.collaboration_callbacks.permitted_callback", return_value=None),
		):
			env = api._run_tool("get_list", {"doctype": "ToDo"}, conversation="CONV-1")
		self.assertFalse(env["ok"])
		new = _new(self.before)
		self.assertEqual(
			[(r.event, r.outcome, r.method, r.code) for r in new],
			[("denied_call", "denied", "get_list", "PermissionError")],
		)

	def test_shared_chat_refused_tool_is_a_denied_call(self):
		a, b = self._shared()
		with a, b, patch("jarvis.api.dispatch") as dispatch:
			env = api._run_tool("schedule_run", {}, conversation="CONV-1")  # not a shared-chat tool
		self.assertFalse(env["ok"])
		dispatch.assert_not_called()
		self.assertEqual([(r.event, r.method) for r in _new(self.before)], [("denied_call", "schedule_run")])

	def test_shared_chat_private_target_is_a_denied_call(self):
		a, b = self._shared()
		with a, b, patch("jarvis.api.dispatch") as dispatch:
			env = api._run_tool(
				"get_doc", {"doctype": "Jarvis User Memory", "name": "x@y.z"}, conversation="CONV-1"
			)
		self.assertFalse(env["ok"])
		dispatch.assert_not_called()
		self.assertEqual([(r.event, r.method) for r in _new(self.before)], [("denied_call", "get_doc")])

	def test_wiki_disabled_is_a_denied_call(self):
		with patch("jarvis.chat.wiki.wiki_enabled", return_value=False):
			env = api._run_tool("read_wiki", {"query": _SECRET})
		self.assertFalse(env["ok"])
		new = _new(self.before)
		self.assertEqual(
			[(r.event, r.method, r.code) for r in new], [("denied_call", "read_wiki", "FeatureDisabledError")]
		)
		self.assertNotIn(_SECRET, repr(new[0]))

	def test_unknown_tool_name_is_never_stored(self):
		with patch("jarvis.chat.wiki.wiki_enabled", return_value=False):
			api._record_denied_call(f"made-up-{_SECRET}", "PermissionError")
		self.assertEqual([r.method for r in _new(self.before)], ["unknown_tool"])


class TestCallTool(ActivityCase):
	def setUp(self):
		super().setUp()
		_ensure_user(_PLAIN)
		_ensure_user(_DISABLED, enabled=0)
		self.addCleanup(self._clear_claims)
		self._clear_claims()

	def _clear_claims(self):
		digest = hashlib.sha256(_SESSION.encode()).hexdigest()[:12]
		frappe.cache().delete_value("jarvis:foreign_session:" + digest)

	@contextlib.contextmanager
	def plugin(self, *, user="Administrator", verified=True, shared=False):
		headers = {"X-Jarvis-Token": "t", "X-Jarvis-Session": _SESSION}
		if verified:
			verify = patch("jarvis.api.validate_plugin_request", return_value=_SESSION)
		else:
			err = PluginAuthError(http_status=401, code="AuthenticationError", message="bad signature")
			verify = patch("jarvis.api.validate_plugin_request", side_effect=err)
		with (
			patch.object(frappe, "request", _FakeRequest(headers), create=True),
			verify,
			patch("jarvis.chat.collaboration.is_collaborative_session", return_value=shared),
			patch("jarvis.chat.collaboration.legacy_session_user", return_value=user),
			patch("jarvis.api._persist_and_publish_tool_call"),
			patch("frappe.enqueue"),
		):
			yield

	def test_plugin_read_is_labelled_chat(self):
		with self.plugin(), patch("jarvis.api.dispatch", return_value=[{"name": "T-1"}]):
			result = api.call_tool(tool="get_list", args={"doctype": "ToDo"})
		self.assertTrue(result["ok"], msg=result)
		new = _new(self.before, method="get_list")
		self.assertEqual([(r.event, r.channel, r.row_count) for r in new], [("read", "chat", 1)])

	def test_disabled_user_is_an_auth_rejected(self):
		with self.plugin(user=_DISABLED):
			result = api.call_tool(tool="get_list", args={"doctype": "ToDo"})
		self.assertFalse(result["ok"])
		self.assertEqual(frappe.local.response.http_status_code, 403)
		new = _new(self.before)
		self.assertEqual(
			[(r.event, r.actor, r.outcome, r.code) for r in new],
			[("auth_rejected", _DISABLED, "denied", "user_disabled")],
		)

	def test_stale_device_pairing_is_an_auth_rejected(self):
		with (
			self.plugin(),
			patch("jarvis.chat.device.session_device_is_stale", return_value=True),
			patch("jarvis.api._notify_stale_pairing"),
		):
			result = api.call_tool(tool="get_list", args={"doctype": "ToDo"})
		self.assertFalse(result["ok"])
		self.assertEqual(frappe.local.response.http_status_code, 401)
		self.assertEqual(
			[(r.event, r.actor, r.outcome, r.code) for r in _new(self.before)],
			[("auth_rejected", "Administrator", "denied", "stale_session")],
		)

	def test_unknown_session_is_an_auth_rejected_and_a_foreign_session(self):
		# jarvis.test lacks the Jarvis Settings counter column, so the throttle's own
		# bookkeeping is stubbed (test_foreign_session covers it on a migrated site).
		with self.plugin(user=None), patch("jarvis.chat.foreign_session._record", return_value=True):
			result = api.call_tool(tool="get_list", args={"doctype": "ToDo"})
		self.assertEqual(frappe.local.response.http_status_code, 400)
		self.assertFalse(result["ok"])
		events = sorted((r.event, r.actor, r.code) for r in _new(self.before))
		self.assertEqual(
			events, [("auth_rejected", "", "unknown_session"), ("foreign_session", "", "unknown_session")]
		)

	def test_foreign_session_record_writes_one_event_per_first_sighting(self):
		from jarvis.chat import foreign_session

		with patch("jarvis.chat.foreign_session._record", side_effect=[True, False]):
			self.assertTrue(foreign_session.record(_SESSION))
			self.assertFalse(foreign_session.record(_SESSION))  # throttled: no second row
		new = _new(self.before, event="foreign_session")
		self.assertEqual(
			[(r.actor, r.outcome, r.code, r.channel) for r in new],
			[("", "denied", "unknown_session", "chat")],
		)
		self.assertNotIn(_SESSION, repr(new[0]))

	def test_shared_chat_private_tool_403_is_a_denied_call(self):
		with self.plugin(shared=True), patch("jarvis.api._deposit_dispatch_refusal", return_value=None):
			result = api.call_tool(tool="recall", args={})
		self.assertEqual(frappe.local.response.http_status_code, 403)
		self.assertFalse(result["ok"])
		new = _new(self.before)
		self.assertEqual(
			[(r.event, r.method, r.outcome, r.channel) for r in new],
			[("denied_call", "recall", "denied", "chat")],
		)

	def test_signed_in_user_without_jarvis_access_is_a_denied_call(self):
		frappe.set_user(_PLAIN)
		with patch("jarvis.api.has_jarvis_access", return_value=False):
			result = api.call_tool(tool="get_list", args={"doctype": "ToDo"})
		self.assertEqual(frappe.local.response.http_status_code, 403)
		self.assertFalse(result["ok"])
		frappe.set_user("Administrator")
		self.assertEqual(
			[(r.event, r.actor, r.method) for r in _new(self.before)],
			[("denied_call", _PLAIN, "get_list")],
		)

	def test_unsigned_unknown_caller_401_records_nothing(self):
		with self.plugin(verified=False):
			result = api.call_tool(tool="get_list", args={"doctype": "ToDo"})
		self.assertFalse(result["ok"])
		self.assertEqual(frappe.local.response.http_status_code, 401)
		self.assertEqual(_new(self.before), [])

	def test_guest_401_records_nothing(self):
		frappe.set_user("Guest")
		result = api.call_tool(tool="get_list", args={"doctype": "ToDo"})
		frappe.set_user("Administrator")
		self.assertFalse(result["ok"])
		self.assertEqual(_new(self.before), [])
