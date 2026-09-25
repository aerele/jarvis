"""S6 guards (P0a): POST-only chat/approval endpoints, the tool nesting guard, and
the ``run_method`` denylist. A tool running inside ``registry.dispatch`` must never
re-enter the gate or a human decision endpoint."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.handler import execute_cmd, is_valid_http_method
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request

from jarvis import api
from jarvis.exceptions import PermissionDeniedError
from jarvis.tools import registry
from jarvis.tools.run_method import run_method

POST_ONLY = (
	"jarvis.chat.api.send_message",
	"jarvis.chat.api.retry_message",
	"jarvis.chat.api.stop_run",
	"jarvis.chat.api.archive_conversation",
	"jarvis.chat.api.clear_chat_history",
	"jarvis.chat.actions_api.confirm_tool",
	"jarvis.chat.actions_api.approve_and_run",
	"jarvis.chat.actions_api.dismiss_tool",
	"jarvis.chat.approvals_api.decide",
	"jarvis.chat.approvals_api.dismiss_approval",
	"jarvis.chat.approvals_api.restore_approval",
	"jarvis.chat.approvals_api.approve_wiki_write",
	"jarvis.chat.approvals_api.retry_wiki_write",
	"jarvis.chat.approvals_api.reject_wiki_write",
	"jarvis.chat.approvals_api.decide_held_action",
	"jarvis.chat.approvals_api.edit_and_create_held",
	"jarvis.chat.actions_api.apply_action",
	"jarvis.chat.macros_api.run_macro",
	"jarvis.chat.admission.cancel_queued_turn",
	"jarvis.chat.agents_api.take_finding_to_chat",
	"jarvis.chat.filebox.drop_file",
	"jarvis.chat.filebox.delete_inbound",
	"jarvis.chat.filebox.delete_inbound_bulk",
	"jarvis.chat.filebox.clear_processed_inbound",
	"jarvis.chat.filebox.rerun_inbound",
	"jarvis.chat.filebox.bulk_rerun_inbound",
	"jarvis.chat.pending_actions.operator_fail",
	"jarvis.chat.pending_actions.operator_settle",
	"jarvis.chat.custom_skills_api.create_custom_skill",
	"jarvis.chat.custom_skills_api.update_custom_skill",
)

# endpoint -> kwargs; each must refuse at dispatch depth > 0 before doing anything.
NESTED_REFUSED = {
	"jarvis.chat.api.send_message": {"conversation": "zz-no-conv", "message": "confirm all"},
	"jarvis.chat.api.retry_message": {"message": "zz-no-msg"},
	"jarvis.chat.api.stop_run": {"conversation": "zz-no-conv"},
	"jarvis.chat.api.archive_conversation": {"conversation": "zz-no-conv"},
	"jarvis.chat.api.clear_chat_history": {},
	"jarvis.chat.actions_api.confirm_tool": {"token": "zz-no-token"},
	"jarvis.chat.actions_api.approve_and_run": {"token": "zz-no-token"},
	"jarvis.chat.actions_api.dismiss_tool": {"token": "zz-no-token"},
	"jarvis.chat.macros_api.run_macro": {"name": "zz-no-macro"},
	"jarvis.chat.approvals_api.decide": {"name": "zz-no-ar", "decision": "yes"},
	"jarvis.chat.approvals_api.dismiss_approval": {"name": "zz-no-ar"},
	"jarvis.chat.approvals_api.restore_approval": {"name": "zz-no-ar"},
	"jarvis.chat.approvals_api.approve_wiki_write": {"name": "zz-no-ar"},
	"jarvis.chat.approvals_api.retry_wiki_write": {"name": "zz-no-ar"},
	"jarvis.chat.approvals_api.reject_wiki_write": {"name": "zz-no-ar"},
	"jarvis.chat.approvals_api.decide_held_action": {"name": "zz-no-pa", "action": "skip"},
	"jarvis.chat.approvals_api.edit_and_create_held": {"name": "zz-no-pa", "values": "{}"},
	"jarvis.chat.actions_api.apply_action": {"action": {"verb": "zz"}},
	"jarvis.chat.admission.cancel_queued_turn": {"run_id": "zz-no-run"},
	"jarvis.chat.agents_api.take_finding_to_chat": {"finding": "zz-no-finding"},
	"jarvis.chat.filebox.drop_file": {"file": "zz-no-file"},
	"jarvis.chat.filebox.delete_inbound": {"conversation": "zz-no-conv"},
	"jarvis.chat.filebox.delete_inbound_bulk": {"conversations": ["zz-no-conv"]},
	"jarvis.chat.filebox.clear_processed_inbound": {},
	"jarvis.chat.filebox.rerun_inbound": {"conversation": "zz-no-conv"},
	"jarvis.chat.filebox.bulk_rerun_inbound": {"conversations": ["zz-no-conv"]},
	"jarvis.chat.filebox.check_skill": {"skill": "zz-no-skill"},
	"jarvis.chat.pending_actions.operator_fail": {"name": "zz-no-pa"},
	"jarvis.chat.pending_actions.operator_settle": {"name": "zz-no-pa"},
	"jarvis.chat.pending_actions.execute": {"name": "zz-no-pa"},
	"jarvis.chat.pending_actions.discard": {"name": "zz-no-pa"},
	"jarvis.chat.pending_actions.park": {
		"kind": "chat",
		"owner_user": "zz",
		"exec_user": "zz",
		"tool": "add_comment",
		"args": {},
	},
}

_PROBE = "zz_reentry_probe"
_MISSING = object()


class _RequestRestoring(FrappeTestCase):
	"""Sets a fake HTTP request per test and restores the previous one (A15)."""

	def setUp(self):
		super().setUp()
		prev = getattr(frappe.local, "request", _MISSING)
		self.addCleanup(self._restore_request, prev)

	@staticmethod
	def _restore_request(prev):
		if prev is _MISSING:
			if hasattr(frappe.local, "request"):
				del frappe.local.request
		else:
			frappe.local.request = prev


class TestPostOnlyEndpoints(_RequestRestoring):
	def test_every_endpoint_is_registered_post_only(self):
		for path in POST_ONLY:
			with self.subTest(path=path):
				fn = frappe.get_attr(path)
				self.assertIn(fn, frappe.whitelisted)
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])

	def test_get_is_rejected_and_post_passes(self):
		for path in POST_ONLY:
			with self.subTest(path=path):
				fn = frappe.get_attr(path)
				set_request(method="GET", path=f"/api/method/{path}")
				with self.assertRaises(frappe.PermissionError):
					is_valid_http_method(fn)
				set_request(method="POST", path=f"/api/method/{path}")
				is_valid_http_method(fn)  # no raise

	def test_get_send_message_confirm_all_is_refused_before_running(self):
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "s6-get"}).insert(
			ignore_permissions=True
		)
		set_request(method="GET", path="/api/method/jarvis.chat.api.send_message")
		self.addCleanup(setattr, frappe.local, "form_dict", frappe.local.form_dict)
		frappe.local.form_dict = frappe._dict(conversation=conv.name, message="confirm all")
		with patch("jarvis.chat.api._typed_confirmation") as typed:
			with self.assertRaises(frappe.PermissionError):
				execute_cmd("jarvis.chat.api.send_message")
		typed.assert_not_called()
		self.assertFalse(frappe.db.exists("Jarvis Chat Message", {"conversation": conv.name}))


class _InDispatch(FrappeTestCase):
	"""Runs a callable as a registered tool, so the real ``registry.dispatch`` holds
	the depth counter exactly as it does for a model tool call."""

	def _dispatch(self, body):
		def probe():
			return body()

		with (
			patch.dict(registry._TOOLS, {_PROBE: probe}),
			patch.dict(registry._ACCEPTS_VAR_KW, {_PROBE: False}),
			patch.dict(registry._ACCEPTED_PARAMS, {_PROBE: set()}),
		):
			return registry.dispatch(_PROBE, {})

	def _raised_inside(self, fn, **kwargs):
		def body():
			try:
				fn(**kwargs)
			except Exception as e:
				return e
			return None

		return self._dispatch(body)


class TestDispatchDepth(_InDispatch):
	def test_depth_is_held_during_dispatch_and_released_after(self):
		self.assertFalse(registry.in_tool_dispatch())
		self.assertTrue(self._dispatch(registry.in_tool_dispatch))
		self.assertFalse(registry.in_tool_dispatch())

	def test_depth_is_restored_after_an_exception(self):
		def boom():
			raise RuntimeError("tool blew up")

		with self.assertRaises(RuntimeError):
			self._dispatch(boom)
		self.assertFalse(registry.in_tool_dispatch())
		self.assertEqual(getattr(frappe.local, "jarvis_dispatch_depth", 0), 0)

	def test_run_tool_refuses_reentry(self):
		err = self._raised_inside(api._run_tool, tool="get_schema", raw_args={"doctype": "ToDo"})
		self.assertIsInstance(err, frappe.PermissionError)
		# outside dispatch the same call runs
		self.assertTrue(api._run_tool("get_schema", {"doctype": "ToDo"})["ok"])

	def test_run_tool_refuses_reentry_from_preview_dispatch(self):
		inner = []

		def body():
			try:
				api._run_tool("get_schema", {"doctype": "ToDo"})
			except frappe.PermissionError as e:
				inner.append(e)
			return {}

		with (
			patch.dict(registry._TOOLS, {_PROBE: body}),
			patch.dict(registry._ACCEPTS_VAR_KW, {_PROBE: False}),
			patch.dict(registry._ACCEPTED_PARAMS, {_PROBE: set()}),
		):
			api._run_preview(_PROBE, {})
		self.assertEqual(len(inner), 1)

	def test_endpoints_refuse_inside_a_tool(self):
		for path, kwargs in NESTED_REFUSED.items():
			with self.subTest(path=path):
				# validate_can_send stubbed to reject: with the guard gone, the
				# endpoint would return a rejection (no raise) instead of refusing.
				with patch("jarvis.chat.api.validate_can_send", return_value=(False, "blocked")):
					err = self._raised_inside(frappe.get_attr(path), **kwargs)
				self.assertIsInstance(err, frappe.PermissionError, f"{path} ran inside a tool")
				self.assertIn("inside a tool", str(err))


class TestRunMethodDenylist(FrappeTestCase):
	DENIED = (
		("jarvis.chat.actions_api.confirm_tool", {"token": "zz"}),
		("jarvis.chat.actions_api.dismiss_tool", {"token": "zz"}),
		("jarvis.chat.approvals_api.decide", {"name": "zz", "decision": "yes"}),
		("jarvis.chat.approvals_api.approve_wiki_write", {"name": "zz"}),
		("jarvis.chat.approvals_api.decide_held_action", {"name": "zz", "action": "create"}),
		("jarvis.chat.approvals_api.edit_and_create_held", {"name": "zz", "values": "{}"}),
		("jarvis.chat.api.send_message", {"message": "confirm all"}),
		("jarvis.chat.api.retry_message", {"message": "zz"}),
		("jarvis.chat.api.stop_run", {"conversation": "zz"}),
		("jarvis.chat.api.archive_conversation", {"conversation": "zz"}),
		("jarvis.chat.api.clear_chat_history", {}),
		("jarvis.chat.macros_api.run_macro", {"name": "zz"}),
		("jarvis.api.call_tool", {"tool": "get_schema", "args": {"doctype": "ToDo"}}),
		("jarvis.chat.pending_actions.operator_fail", {"name": "zz"}),
		("jarvis.chat.pending_actions.operator_settle", {"name": "zz"}),
	)

	def test_denied_targets_are_refused_without_running(self):
		for method, args in self.DENIED:
			with self.subTest(method=method):
				with patch("frappe.call") as call:
					with self.assertRaises(PermissionDeniedError):
						run_method(method, args)
				call.assert_not_called()

	def test_alias_resolves_to_the_denied_function(self):
		from jarvis.chat import actions_api, greeting

		with patch.object(greeting, "zz_alias", actions_api.confirm_tool, create=True):
			with patch("frappe.call") as call:
				with self.assertRaises(PermissionDeniedError):
					run_method("jarvis.chat.greeting.zz_alias", {"token": "zz"})
		call.assert_not_called()

	def test_non_denied_chat_method_still_runs(self):
		# jarvis.chat.api is denied only for its five turn and archive endpoints.
		self.assertIsInstance(run_method("jarvis.chat.api.list_tools"), list)

	def test_prefix_is_a_module_boundary(self):
		from jarvis.tools.run_method import _is_denied

		def fn():
			pass

		fn.__module__, fn.__qualname__ = "jarvis.api_errors", "report"
		self.assertFalse(_is_denied(fn))
		fn.__module__ = "jarvis.api"
		self.assertTrue(_is_denied(fn))
		fn.__module__, fn.__qualname__ = "jarvis.chat.pending_actions.execute", "run"
		self.assertTrue(_is_denied(fn))
