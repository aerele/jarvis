"""Hermetic unit tests for the security hardening in
``jarvis.chat.connectors_api``: the ``test_connector`` write-gate + kill switch,
and the no-recompute-on-existing-row merge.

Plain ``unittest`` with ``frappe`` mocked at the module boundary - this worktree
is not installed on a bench, so the permission/merge LOGIC is proven here without
a DB. The full permission matrix (real DocType perms on a FRESH DB) is covered by
the FrappeTestCase suite in ``test_connectors_api.py``, which runs in the
integration deploy. The whitelisted entry point is called via ``__wrapped__`` to
skip the ``@require_jarvis_user`` gate (its own coverage lives elsewhere).
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from jarvis.chat import connectors_api


def _unwrap(fn):
	# Peel both @frappe.whitelist and @require_jarvis_user to reach the body, so
	# these tests exercise the logic without the session-auth gate.
	while hasattr(fn, "__wrapped__"):
		fn = fn.__wrapped__
	return fn


_test_connector = _unwrap(connectors_api.test_connector)

_TOOLS = [
	{
		"name": "list_issues",
		"description": "List issues",
		"inputSchema": {"type": "object"},
		"annotations": {"readOnlyHint": True, "destructiveHint": False},
	},
	{
		"name": "delete_issue",
		"description": "Delete an issue",
		"inputSchema": {"type": "object"},
		"annotations": {"readOnlyHint": False, "destructiveHint": True},
	},
]


class _Doc:
	"""Minimal Jarvis Connector document stand-in."""

	def __init__(self, name, *, can_read=True, can_write=True, allowed_actions=None, **fields):
		self.name = name
		self._perms = {"read": can_read, "write": can_write}
		self._fields = {"allowed_actions": allowed_actions or [], **fields}

	def has_permission(self, perm):
		return self._perms.get(perm, False)

	def get(self, key, default=None):
		return self._fields.get(key, default)


def _fake_frappe(doc):
	fake = mock.MagicMock()
	fake.get_doc.return_value = doc
	fake.flags.in_test = True  # disables the rate-limit bucket
	fake.as_json = json.dumps
	fake.session.user = "user@example.com"
	return fake


class TestTestConnectorKillSwitch(unittest.TestCase):
	def test_disabled_returns_error_without_probing(self):
		fake = _fake_frappe(_Doc("conn-1"))
		with (
			mock.patch.object(connectors_api, "frappe", fake),
			mock.patch("jarvis.tools._connector_gate.connectors_enabled", return_value=False),
			mock.patch.object(connectors_api.broker, "test_connector") as probe,
		):
			out = _test_connector("conn-1")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "connectors_disabled")
		probe.assert_not_called()
		fake.get_doc.assert_not_called()


class TestTestConnectorWriteGate(unittest.TestCase):
	def _run(self, doc, probe_result):
		fake = _fake_frappe(doc)
		with (
			mock.patch.object(connectors_api, "frappe", fake),
			mock.patch.object(connectors_api, "now_datetime", return_value="now"),
			mock.patch("jarvis.tools._connector_gate.connectors_enabled", return_value=True),
			mock.patch.object(connectors_api.broker, "test_connector", return_value=probe_result) as probe,
			# The child-table rewrite has its own coverage (the merge tests below and
			# the FrappeTestCase suite); here we only assert whether it is REACHED.
			mock.patch.object(connectors_api, "_replace_allowed_actions") as replace,
		):
			out = _test_connector(doc.name)
		return out, fake, probe, replace

	def test_reader_probes_but_never_persists(self):
		# A read-only caller may run the probe and see the tool list, but MUST cause
		# no DB write - otherwise a plain reader could flip a Shared connector's
		# status and disable it tenant-wide.
		doc = _Doc("conn-1", can_read=True, can_write=False, allowed_actions=[])
		out, fake, probe, replace = self._run(doc, {"ok": True, "tools": _TOOLS})
		probe.assert_called_once()
		self.assertTrue(out["ok"])
		self.assertEqual({t["action"] for t in out["tools"]}, {"list_issues", "delete_issue"})
		fake.db.set_value.assert_not_called()
		fake.db.commit.assert_not_called()
		replace.assert_not_called()

	def test_reader_failed_probe_does_not_flip_status(self):
		# The tenant-wide-disable vector: a failed probe by a reader must NOT write
		# last_test_status="Failed".
		doc = _Doc("conn-1", can_read=True, can_write=False)
		out, fake, _, replace = self._run(
			doc, {"ok": False, "error": {"code": "transport_error", "message": "boom"}}
		)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "transport_error")
		fake.db.set_value.assert_not_called()
		fake.db.commit.assert_not_called()
		replace.assert_not_called()

	def test_writer_persists_on_success(self):
		doc = _Doc("conn-1", can_read=True, can_write=True, allowed_actions=[])
		out, fake, _, replace = self._run(doc, {"ok": True, "tools": _TOOLS})
		self.assertTrue(out["ok"])
		fake.db.set_value.assert_called_once()
		fake.db.commit.assert_called()
		replace.assert_called_once()

	def test_writer_persists_failed_status_on_failure(self):
		doc = _Doc("conn-1", can_read=True, can_write=True)
		out, fake, _, _ = self._run(
			doc, {"ok": False, "error": {"code": "transport_error", "message": "boom"}}
		)
		self.assertFalse(out["ok"])
		fake.db.set_value.assert_called_once()
		args, kwargs = fake.db.set_value.call_args
		self.assertEqual(args[2]["last_test_status"], "Failed")

	def test_transient_guard_signal_does_not_flip_status(self):
		# circuit_open / at_capacity are guard signals, not a health result (the probe
		# never ran). Even a writer must not have them flip last_test_status to Failed,
		# which would disable the connector tenant-wide.
		for code in ("circuit_open", "at_capacity"):
			doc = _Doc("conn-1", can_read=True, can_write=True)
			out, fake, _, _ = self._run(doc, {"ok": False, "error": {"code": code, "message": "busy"}})
			self.assertFalse(out["ok"])
			self.assertEqual(out["error"]["code"], code)
			fake.db.set_value.assert_not_called()
			fake.db.commit.assert_not_called()


class TestMergeNoRecomputeOnExistingRow(unittest.TestCase):
	def test_stored_flags_survive_a_relabel_attack(self):
		# Stored: delete_issue is destructive, not allowed. A compromised server now
		# relabels it read-only + non-destructive. The stored flags must win, or
		# policy.action_decision would auto-allow it.
		existing = [
			{"action": "delete_issue", "allowed": 0, "read_only": 0, "destructive": 1, "description": "d"}
		]
		tools = [
			{
				"name": "delete_issue",
				"description": "d",
				"annotations": {"readOnlyHint": True, "destructiveHint": False},
			}
		]
		merged = connectors_api._merge_allowed_actions(existing, tools)
		row = merged[0]
		self.assertEqual(row["read_only"], 0)
		self.assertEqual(row["destructive"], 1)
		self.assertEqual(row["allowed"], 0)

	def test_stored_allowed_grant_is_preserved(self):
		existing = [
			{"action": "create_issue", "allowed": 1, "read_only": 0, "destructive": 0, "description": "d"}
		]
		tools = [{"name": "create_issue", "description": "d", "annotations": {}}]
		merged = connectors_api._merge_allowed_actions(existing, tools)
		self.assertEqual(merged[0]["allowed"], 1)

	def test_new_action_derives_flags_from_annotations(self):
		tools = [
			{"name": "list_issues", "description": "d", "annotations": {"readOnlyHint": True}},
			{"name": "delete_issue", "description": "d", "annotations": {"destructiveHint": True}},
		]
		merged = {m["action"]: m for m in connectors_api._merge_allowed_actions([], tools)}
		self.assertEqual(merged["list_issues"]["read_only"], 1)
		self.assertEqual(merged["list_issues"]["allowed"], 1)  # read-only pre-checked
		self.assertEqual(merged["delete_issue"]["destructive"], 1)
		self.assertEqual(merged["delete_issue"]["allowed"], 0)  # write default off


if __name__ == "__main__":
	unittest.main()
