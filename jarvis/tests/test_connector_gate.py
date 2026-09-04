"""Tests for the ACTION-AWARE confirm-first gate for ``call_connector``.

``call_connector`` stays in ``_GATED_WRITES`` (and ``_ARMED_SKIP_NEVER`` /
``_SKILL_AUTORUN_NEVER``), so a connector WRITE parks for a human click exactly
as before. But ``jarvis.api._connector_call_is_safe_read`` exempts one case: a
connector action the user has EXPLICITLY ALLOWED that is marked read-only and
non-destructive runs directly, with no card - still audited (call_connector is a
``_WRITE_TOOL``). FAIL SAFE: any uncertainty (write, destructive, not-allowed,
unknown action, unresolved/disabled connector, malformed args, preview=True, or
any error in the lookup) parks.

The helper decision matrix + the gate short-circuit are HERMETIC (they patch
``broker.resolve_for_status`` / ``api._dispatch_and_wrap``, so no bench). The
row-lookup class is a ``FrappeTestCase`` that inserts a real ``Jarvis Connector``
and drives the helper off its stored ``allowed_actions`` flags - it runs on CI.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api

CONNECTOR = "Jarvis Connector"


class _Row:
	"""Minimal row/child stand-in exposing ``.get(field)`` like a Frappe doc,
	mirroring test_connector_policy's fixture."""

	def __init__(self, **fields):
		self._f = fields

	def get(self, key, default=None):
		return self._f.get(key, default)


def _row(enabled=1, actions=None):
	children = [_Row(**a) for a in (actions or [])]
	return _Row(enabled=enabled, allowed_actions=children)


# --------------------------------------------------------------------------- #
# HERMETIC: the helper's decision matrix (broker.resolve_for_status mocked)
# --------------------------------------------------------------------------- #
class TestConnectorSafeReadHelper(unittest.TestCase):
	def _is_safe(self, args, row):
		# resolve_for_status is imported inside the helper (from jarvis.connectors
		# import broker), so it is looked up on the module at call time - patch there.
		with patch("jarvis.connectors.broker.resolve_for_status", return_value=row) as res:
			result = api._connector_call_is_safe_read(args)
		return result, res

	def test_allowed_read_only_non_destructive_is_safe(self):
		row = _row(actions=[{"action": "search", "allowed": 1, "read_only": 1, "destructive": 0}])
		ok, _ = self._is_safe({"connector": "github", "action": "search"}, row)
		self.assertTrue(ok)

	def test_write_action_not_read_only_parks(self):
		# allowed but not read-only == a write the user permitted; still confirms.
		row = _row(actions=[{"action": "create_issue", "allowed": 1, "read_only": 0, "destructive": 0}])
		ok, _ = self._is_safe({"connector": "github", "action": "create_issue"}, row)
		self.assertFalse(ok)

	def test_destructive_action_always_parks(self):
		# Even allowed + read_only, a destructive flag must never skip the card.
		row = _row(actions=[{"action": "wipe", "allowed": 1, "read_only": 1, "destructive": 1}])
		ok, _ = self._is_safe({"connector": "github", "action": "wipe"}, row)
		self.assertFalse(ok)

	def test_not_allowed_read_only_parks(self):
		# read_only but the user did NOT enable it -> confirm (broker also denies it).
		row = _row(actions=[{"action": "search", "allowed": 0, "read_only": 1, "destructive": 0}])
		ok, _ = self._is_safe({"connector": "github", "action": "search"}, row)
		self.assertFalse(ok)

	def test_unknown_action_parks(self):
		row = _row(actions=[{"action": "other", "allowed": 1, "read_only": 1, "destructive": 0}])
		ok, _ = self._is_safe({"connector": "github", "action": "search"}, row)
		self.assertFalse(ok)

	def test_disabled_connector_parks(self):
		row = _row(enabled=0, actions=[{"action": "search", "allowed": 1, "read_only": 1, "destructive": 0}])
		ok, _ = self._is_safe({"connector": "github", "action": "search"}, row)
		self.assertFalse(ok)

	def test_connector_not_found_parks(self):
		ok, _ = self._is_safe({"connector": "nope", "action": "search"}, None)
		self.assertFalse(ok)

	def test_malformed_args_park(self):
		# Not a dict, missing keys, and non-string values all fail safe.
		for bad in (
			None,
			"not-a-dict",
			{},
			{"connector": "github"},
			{"action": "search"},
			{"connector": "", "action": "search"},
			{"connector": 1, "action": "search"},
			{"connector": "github", "action": None},
		):
			with patch("jarvis.connectors.broker.resolve_for_status") as res:
				self.assertFalse(api._connector_call_is_safe_read(bad), bad)
			# A malformed call must not even reach row resolution.
			res.assert_not_called()

	def test_preview_true_parks(self):
		# preview=True is a category error the park block answers legibly; decline
		# the carve-out so it lands there instead of silently running.
		row = _row(actions=[{"action": "search", "allowed": 1, "read_only": 1, "destructive": 0}])
		with patch("jarvis.connectors.broker.resolve_for_status", return_value=row):
			self.assertFalse(
				api._connector_call_is_safe_read({"connector": "github", "action": "search", "preview": True})
			)

	def test_resolution_error_fails_safe(self):
		with patch("jarvis.connectors.broker.resolve_for_status", side_effect=RuntimeError("boom")):
			self.assertFalse(api._connector_call_is_safe_read({"connector": "github", "action": "search"}))


# --------------------------------------------------------------------------- #
# HERMETIC: the gate short-circuit in _run_tool (dispatch mocked)
# --------------------------------------------------------------------------- #
class TestGateShortCircuit(unittest.TestCase):
	def test_safe_read_dispatches_and_does_not_park(self):
		sentinel = {"ok": True, "data": {"ok": True, "result": "SENTINEL"}}
		with (
			patch.object(api, "_connector_call_is_safe_read", return_value=True),
			patch.object(api, "_dispatch_and_wrap", return_value=sentinel) as disp,
			patch.object(api.telemetry, "record_tool"),
		):
			r = api._run_tool("call_connector", {"connector": "github", "action": "search"})
		disp.assert_called_once()
		self.assertEqual(disp.call_args.args[0], "call_connector")
		# is_write stays True on this path (call_connector is a _WRITE_TOOL) so the
		# fall-through dispatch audits it - the third positional arg is is_write.
		self.assertTrue(disp.call_args.args[2])
		self.assertNotEqual((r.get("data") or {}).get("status"), "pending_confirmation")
		self.assertEqual(r["data"]["result"], "SENTINEL")

	def test_non_connector_tool_never_pays_the_lookup(self):
		# The ``tool == "call_connector"`` guard short-circuits, so a non-connector
		# read never resolves a connector row. get_doc is not a write and not gated,
		# so it also falls through to dispatch (mocked here to stay hermetic).
		with (
			patch("jarvis.connectors.broker.resolve_for_status") as res,
			patch.object(api, "_dispatch_and_wrap", return_value={"ok": True, "data": {}}),
			patch.object(api.telemetry, "record_tool"),
		):
			api._run_tool("get_doc", {"doctype": "ToDo", "name": "x"})
		res.assert_not_called()


# --------------------------------------------------------------------------- #
# FrappeTestCase: the helper against a REAL inserted connector row (runs on CI)
# --------------------------------------------------------------------------- #
class TestConnectorSafeReadRowLookup(FrappeTestCase):
	def setUp(self):
		self._orig_user = frappe.session.user
		self._connectors: list[str] = []

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for name in self._connectors:
			if frappe.db.exists(CONNECTOR, name):
				frappe.delete_doc(CONNECTOR, name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _mk(self, key: str, actions: list[dict], enabled: int = 1) -> str:
		"""Insert a Shared connector as Administrator so resolve_for_status
		(Personal-wins-over-Shared) resolves it for any caller."""
		prev = frappe.session.user
		frappe.set_user("Administrator")
		try:
			doc = frappe.get_doc(
				{
					"doctype": CONNECTOR,
					"key": key,
					"label": f"gate-{key}",
					"scope": "Shared",
					"enabled": enabled,
					"base_url": "https://example.invalid/mcp",
					"allowed_actions": actions,
				}
			).insert(ignore_permissions=True)
			self._connectors.append(doc.name)
			frappe.db.commit()
			return doc.name
		finally:
			frappe.set_user(prev)

	def test_real_row_safe_read_and_write(self):
		key = "gate-github"
		self._mk(
			key,
			actions=[
				{"action": "search_repositories", "allowed": 1, "read_only": 1, "destructive": 0},
				{"action": "create_issue", "allowed": 1, "read_only": 0, "destructive": 0},
				{"action": "delete_repo", "allowed": 1, "read_only": 1, "destructive": 1},
				{"action": "list_secrets", "allowed": 0, "read_only": 1, "destructive": 0},
			],
		)
		# Read the stored flags off the real row through the real broker resolution.
		self.assertTrue(api._connector_call_is_safe_read({"connector": key, "action": "search_repositories"}))
		self.assertFalse(api._connector_call_is_safe_read({"connector": key, "action": "create_issue"}))
		self.assertFalse(api._connector_call_is_safe_read({"connector": key, "action": "delete_repo"}))
		self.assertFalse(api._connector_call_is_safe_read({"connector": key, "action": "list_secrets"}))
		self.assertFalse(api._connector_call_is_safe_read({"connector": key, "action": "no_such_action"}))

	def test_real_row_disabled_connector_parks(self):
		key = "gate-disabled"
		self._mk(
			key,
			actions=[{"action": "search_repositories", "allowed": 1, "read_only": 1, "destructive": 0}],
			enabled=0,
		)
		self.assertFalse(
			api._connector_call_is_safe_read({"connector": key, "action": "search_repositories"})
		)

	def test_run_tool_safe_read_skips_card_and_is_audited(self):
		# End-to-end through _run_tool on a REAL safe-read row: no card is parked,
		# the call dispatches, and it is AUDITED (call_connector stays a _WRITE_TOOL).
		# dispatch() is patched to a sentinel so the tool's own network/kill-switch
		# internals do not run - the point here is the GATE decision, not the call.
		key = "gate-e2e-read"
		self._mk(
			key,
			actions=[{"action": "search_repositories", "allowed": 1, "read_only": 1, "destructive": 0}],
		)
		with (
			patch("jarvis.api.dispatch", return_value={"ok": True, "result": "R"}) as disp,
			patch("jarvis.api.audit.record") as aud,
			patch("jarvis.chat.pending_confirm.mint") as mint,
			patch.object(api.telemetry, "record_tool"),
		):
			r = api._run_tool("call_connector", {"connector": key, "action": "search_repositories"})
		self.assertNotEqual((r.get("data") or {}).get("status"), "pending_confirmation")
		disp.assert_called_once()
		mint.assert_not_called()  # no confirmation card was minted
		aud.assert_called()  # the read was still audited as a write

	def test_run_tool_write_parks_and_does_not_execute(self):
		# A connector WRITE on a real row still parks: a card is minted and dispatch
		# is never reached.
		key = "gate-e2e-write"
		self._mk(
			key,
			actions=[{"action": "create_issue", "allowed": 1, "read_only": 0, "destructive": 0}],
		)
		with (
			patch("jarvis.api.dispatch") as disp,
			patch("jarvis.chat.pending_confirm.mint", return_value="tok") as mint,
			patch("jarvis.chat.events.publish_to_user"),
			patch.object(api.telemetry, "record_tool"),
		):
			r = api._run_tool("call_connector", {"connector": key, "action": "create_issue"})
		self.assertEqual((r.get("data") or {}).get("status"), "pending_confirmation")
		mint.assert_called_once()
		disp.assert_not_called()


if __name__ == "__main__":
	unittest.main()
