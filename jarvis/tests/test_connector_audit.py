"""Unit tests for the connector-envelope unwrap in ``jarvis.api``: a connector
tool never raises and returns its own ``{ok: false}`` envelope as the tool's
data, so the audit trail and the receipt chip must reflect that INNER outcome,
not the outer "the tool dispatched" ok. Narrow: non-connector tools unchanged.

Plain ``unittest`` - ``envelope_ok`` is pure and ``_record_tool_audit`` only
needs ``audit.record`` stubbed. No bench.
"""

from __future__ import annotations

import unittest
from unittest import mock

from jarvis import api


class TestEnvelopeOk(unittest.TestCase):
	def test_non_connector_uses_outer_ok(self):
		self.assertTrue(api.envelope_ok("create_doc", {"ok": True, "data": {"ok": False}}))
		self.assertFalse(api.envelope_ok("create_doc", {"ok": False, "error": {}}))

	def test_connector_unwraps_inner_ok(self):
		# The tool dispatched (outer ok True) but the connector call was blocked.
		self.assertFalse(api.envelope_ok("call_connector", {"ok": True, "data": {"ok": False, "error": {}}}))
		self.assertTrue(api.envelope_ok("call_connector", {"ok": True, "data": {"ok": True, "result": {}}}))

	def test_connector_outer_failure_is_false(self):
		self.assertFalse(api.envelope_ok("call_connector", {"ok": False, "error": {}}))

	def test_connector_data_without_ok_falls_back_to_outer(self):
		self.assertTrue(api.envelope_ok("call_connector", {"ok": True, "data": {"result": 1}}))


class TestRecordToolAudit(unittest.TestCase):
	def test_connector_failure_recorded_as_failure(self):
		data = {"ok": False, "error": {"code": "ssrf_blocked", "message": "nope"}}
		with mock.patch.object(api.audit, "record") as rec:
			api._record_tool_audit("call_connector", {"connector": "github"}, data)
		_, kwargs = rec.call_args
		self.assertFalse(kwargs["ok"])
		self.assertEqual(kwargs["error_code"], "ssrf_blocked")
		self.assertEqual(kwargs["error_message"], "nope")

	def test_connector_success_recorded_as_success(self):
		data = {"ok": True, "result": {"content": []}}
		with mock.patch.object(api.audit, "record") as rec:
			api._record_tool_audit("call_connector", {}, data)
		_, kwargs = rec.call_args
		self.assertTrue(kwargs["ok"])
		self.assertEqual(kwargs["result"], data)

	def test_connector_failure_without_error_dict_uses_fallback_code(self):
		with mock.patch.object(api.audit, "record") as rec:
			api._record_tool_audit("call_connector", {}, {"ok": False})
		_, kwargs = rec.call_args
		self.assertFalse(kwargs["ok"])
		self.assertEqual(kwargs["error_code"], "connector_error")

	def test_non_connector_tool_always_ok_true(self):
		# A non-envelope tool must be recorded exactly as before, even if its data
		# happens to carry an ok:false key.
		data = {"ok": False, "error": {"code": "x"}}
		with mock.patch.object(api.audit, "record") as rec:
			api._record_tool_audit("create_doc", {}, data)
		_, kwargs = rec.call_args
		self.assertTrue(kwargs["ok"])
		self.assertEqual(kwargs["result"], data)


if __name__ == "__main__":
	unittest.main()
