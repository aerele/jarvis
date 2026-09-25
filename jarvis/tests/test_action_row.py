"""Tests for the durable action-row (PR 1 of the action-card overhaul).

The gated-write confirmation card becomes a durable ``role="tool"`` Jarvis Chat
Message (``tool_status="pending"``) — the pre-action twin of the receipt chip —
delivered and reloaded by the message pipeline. The Redis ``pending_confirm``
token stays the SOLE execution authority; the row is display-only for execution.

Spec: docs/superpowers/specs/2026-09-21-action-card-overhaul-design.md (§9).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm, session_lifecycle
from jarvis.chat.actions_api import confirm_tool, dismiss_tool
from jarvis.chat.api import get_conversation


def _make_conversation() -> str:
	return (
		frappe.get_doc({"doctype": "Jarvis Conversation", "title": "action-row test"})
		.insert(ignore_permissions=True)
		.name
	)


class TestActionRowDoctype(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.conv = _make_conversation()
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)
		)

	def test_pending_status_and_fields_exist(self):
		"""AC-C1 (spec §9.4 AC-migrate): a role="tool" row accepts tool_status="pending"
		(Select option present) and carries pending_card + expires_at."""
		m = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conv,
				"seq": 1,
				"role": "tool",
				"tool_status": "pending",
				"tool_call_id": "tok_actionrow_test",
				"pending_card": frappe.as_json({"kind": "verb", "verb": "submit"}),
				"expires_at": frappe.utils.now_datetime(),
			}
		)
		m.flags.jarvis_server_write = True  # a server-written fixture (P0a guard)
		m.insert(ignore_permissions=True)  # must NOT raise InvalidSelectError
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Chat Message", m.name, force=True, ignore_permissions=True)
		)
		row = frappe.db.get_value(
			"Jarvis Chat Message",
			m.name,
			["tool_status", "pending_card", "expires_at"],
			as_dict=True,
		)
		self.assertEqual(row.tool_status, "pending")
		self.assertTrue(row.pending_card)
		self.assertIsNotNone(row.expires_at)


class TestParkActionRow(FrappeTestCase):
	"""Task 1.1: parking a gated write writes a durable pending row (fail-closed)."""

	def setUp(self):
		super().setUp()
		self.owner = frappe.session.user  # Administrator under the test runner
		# Redis is NOT rolled back by FrappeTestCase — clear the owner index so
		# list_for_owner assertions are isolated from other suites' parked tokens.
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
		self.conv = _make_conversation()
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		for rec in pending_confirm.list_for_owner(self.owner):
			try:
				pending_confirm.consume(
					rec["token"], owner=self.owner, conversation=rec.get("conversation") or ""
				)
			except Exception:
				pass
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
		frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)

	def _pending_rows(self):
		return frappe.get_all(
			"Jarvis Chat Message",
			filters={"conversation": self.conv, "role": "tool", "tool_status": "pending"},
			fields=["name", "pending_card", "tool_args", "tool_call_id", "expires_at"],
		)

	def test_park_writes_pending_row_no_raw_args(self):
		"""AC-R1 groundwork: a gated park writes exactly one pending row carrying the
		card + token + expiry, and NOT raw tool_args (an unconfirmed row never exposes
		them). Nothing executes — the Redis token is the authority, the row is display-only."""
		r = api._run_tool(
			"create_doc",
			{"doctype": "ToDo", "values": {"description": "actionrow-park-xyz"}},
			conversation=self.conv,
		)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		rows = self._pending_rows()
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0].pending_card)  # card delivered on the durable row
		self.assertFalse(rows[0].tool_args)  # NO raw args on a pending row
		self.assertTrue(rows[0].tool_call_id)  # bound to the token
		self.assertIsNone(rows[0].expires_at)  # PR-3b: a pending-action card never expires
		self.assertFalse(frappe.db.exists("ToDo", {"description": "actionrow-park-xyz"}))

	def test_park_row_failure_rolls_back_token(self):
		"""AC-park-fail: if the row insert fails after a successful mint, the gate rolls
		the token back (no orphan in the owner index), returns the retryable error, and
		executes nothing."""
		with patch("jarvis.api.persist_pending_action", side_effect=Exception("db blip")):
			r = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "actionrow-fail-xyz"}},
				conversation=self.conv,
			)
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "ConfirmationUnavailableError")
		self.assertEqual(pending_confirm.list_for_owner(self.owner, self.conv), [])
		self.assertEqual(len(self._pending_rows()), 0)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "actionrow-fail-xyz"}))


class TestFlipActionRow(FrappeTestCase):
	"""Task 1.2: confirm/discard/approve flip the parked pending row into the receipt
	IN PLACE (flip-else-insert), so there is exactly one terminal row per token."""

	def setUp(self):
		super().setUp()
		self.owner = frappe.session.user
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
		self.conv = _make_conversation()
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		for rec in pending_confirm.list_for_owner(self.owner):
			try:
				pending_confirm.consume(
					rec["token"], owner=self.owner, conversation=rec.get("conversation") or ""
				)
			except Exception:
				pass
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
		frappe.db.delete("ToDo", {"description": ["like", "flip-%"]})
		frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)

	def _rows(self):
		return frappe.get_all(
			"Jarvis Chat Message",
			filters={"conversation": self.conv, "role": "tool"},
			fields=["name", "tool_status", "action_outcome", "tool_call_id", "pending_card", "tool_args"],
		)

	def _pending_rows(self):
		return [r for r in self._rows() if r.tool_status == "pending"]

	def _park(self, desc):
		api._run_tool(
			"create_doc",
			{"doctype": "ToDo", "values": {"description": desc}},
			conversation=self.conv,
		)
		pending = self._pending_rows()
		self.assertEqual(len(pending), 1)
		return pending[0].name, pending[0].tool_call_id

	def test_confirm_flips_pending_row_in_place(self):
		"""AC-flip: confirm flips the SAME row to 'confirmed' (no duplicate), clears the
		card, and the write actually ran."""
		name, token = self._park("flip-confirm-xyz")
		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(token, conversation=self.conv)
		self.assertTrue(res["ok"])
		same = [r for r in self._rows() if r.tool_call_id == token]
		self.assertEqual(len(same), 1)  # exactly one terminal row, no orphan/dup
		self.assertEqual(same[0].name, name)  # SAME row, flipped in place
		self.assertEqual(same[0].action_outcome, "confirmed")
		self.assertFalse(same[0].pending_card)  # minimized on flip
		self.assertTrue(frappe.db.exists("ToDo", {"description": "flip-confirm-xyz"}))

	def test_direct_mint_confirm_falls_back_to_insert(self):
		"""AC-flip fallback: a directly-minted token (no gate -> no pending row) still
		yields exactly one confirmed receipt row (insert path). A LEGACY token (flag 0):
		a pending action's mint inserts its display row itself."""
		flag = patch.dict(frappe.conf, {"jarvis_pa_chat_cards": 0})
		flag.start()
		self.addCleanup(flag.stop)
		token = pending_confirm.mint(
			conversation=self.conv,
			owner=self.owner,
			exec_user=self.owner,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "flip-directmint-xyz"}},
			run_id="",
		)
		self.assertEqual(len(self._pending_rows()), 0)  # nothing parked a row
		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(token, conversation=self.conv)
		self.assertTrue(res["ok"])
		rows = [r for r in self._rows() if r.tool_call_id == token]
		self.assertEqual(len(rows), 1)  # inserted exactly one
		self.assertEqual(rows[0].action_outcome, "confirmed")

	def test_discard_flips_pending_row_to_discarded(self):
		"""AC-flip: discard flips the SAME row to 'discarded' + clears the card; nothing runs."""
		name, token = self._park("flip-discard-xyz")
		res = dismiss_tool(token, conversation=self.conv)
		self.assertTrue(res["ok"])
		same = [r for r in self._rows() if r.tool_call_id == token]
		self.assertEqual(len(same), 1)
		self.assertEqual(same[0].name, name)
		self.assertEqual(same[0].action_outcome, "discarded")
		self.assertFalse(same[0].pending_card)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "flip-discard-xyz"}))

	def test_get_conversation_delivers_pending_row(self):
		"""AC-R1: get_conversation returns the pending row with the card (parsed), token,
		and expiry — so a reload (independent of Redis) shows a confirmable card; after
		confirm the flipped row carries no pending_card."""
		_name, token = self._park("flip-getconv-xyz")
		conv = get_conversation(self.conv)
		rows = [m for m in conv["messages"] if m.get("tool_call_id") == token]
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["tool_status"], "pending")
		self.assertIsInstance(rows[0]["pending_card"], dict)  # parsed to a real object
		self.assertTrue(rows[0]["tool_call_id"])
		self.assertIsNone(rows[0]["expires_at"])  # PR-3b: no countdown, the card never expires
		self.assertTrue(rows[0]["recent"])  # parked since the last human message
		with patch("jarvis.chat.api._dispatch_turn"):
			confirm_tool(token, conversation=self.conv)
		conv2 = get_conversation(self.conv)
		flipped = [m for m in conv2["messages"] if m.get("tool_call_id") == token]
		self.assertEqual(len(flipped), 1)
		self.assertEqual(flipped[0]["action_outcome"], "confirmed")
		self.assertFalse(flipped[0]["pending_card"])

	def test_stop_sweep_flips_pending_row_to_cancelled(self):
		"""Task 1.4 (spec §9.3): the stop-run sweep flips a parked pending row to a
		terminal 'cancelled' receipt (not a dangling 'pending' row), and nothing ran."""
		_name, token = self._park("flip-cancel-xyz")
		# The Stop pair: a pending action's row flips when the card sweep settles it.
		pending_confirm.clear_for_conversation(self.owner, self.conv)
		api.cancel_pending_action_rows(self.conv)
		same = [r for r in self._rows() if r.tool_call_id == token]
		self.assertEqual(len(same), 1)
		self.assertEqual(same[0].action_outcome, "cancelled")
		self.assertEqual(same[0].tool_status, "")  # nothing executed
		self.assertFalse(same[0].pending_card)  # minimized
		self.assertEqual(len(self._pending_rows()), 0)  # no dangling pending row
		self.assertFalse(frappe.db.exists("ToDo", {"description": "flip-cancel-xyz"}))


class TestActionCardHealth(FrappeTestCase):
	"""PR 4 (Phase 5, AC-detect): the action-card DELIVERY-health reconciliation and
	the manual-re-check RESCUE signal. Nothing here flips a row or changes execution;
	it is measurement + alerting so a recurrence of the invisible-card bug is DETECTED
	(hourly via the gauge, immediately via the rescue signal) instead of silent.

	Spec: docs/superpowers/specs/2026-09-21-action-card-overhaul-design.md (§9)."""

	def setUp(self):
		super().setUp()
		self.owner = frappe.session.user  # Administrator under the test runner
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
		self.conv = _make_conversation()
		self._seq = 0
		self._minted = []
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		# Redis is not rolled back by FrappeTestCase - drop any live tokens we minted
		# and the owner index so the site-wide gauge/list stay isolated.
		for token in self._minted:
			try:
				pending_confirm.consume(token, owner=self.owner, conversation=self.conv)
			except Exception:
				pass
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
		frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)

	def _insert_pending_row(self, token, expires_at):
		"""Insert a durable role="tool" pending action-row bound to `token`, expiring at
		`expires_at`. Un-committed, so it is visible to the reconcile SQL in this same
		transaction and rolled back at test end."""
		self._seq += 1
		m = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conv,
				"seq": self._seq,
				"role": "tool",
				"tool_status": "pending",
				"tool_call_id": token,
				"pending_card": frappe.as_json({"kind": "verb", "verb": "submit"}),
				"expires_at": expires_at,
			}
		)
		m.flags.jarvis_server_write = True  # a server-written fixture (P0a guard)
		m.insert(ignore_permissions=True)
		return m.name

	def _stranded_ts(self):
		"""An expiry just past the buffer (so it counts) and near NOW (so it sorts to the
		top of the DESC scan, inside the LIMIT, regardless of site-wide leftovers).

		The delta assertions assume the test site holds fewer than _RECONCILE_SCAN_MAX
		in-window pending rows; on a fresh jarvis.test that always holds. If it ever did
		not, inserting a near-now probe could displace the oldest in-window row out of the
		LIMIT and skew the delta - use a scoped fixture then."""
		return frappe.utils.add_to_date(
			frappe.utils.now_datetime(), seconds=-(session_lifecycle._STRANDED_BUFFER_S + 60)
		)

	def _mint_live(self):
		token = pending_confirm.mint(
			conversation=self.conv,
			owner=self.owner,
			tool="submit_doc",
			args={"doctype": "ToDo", "name": "x"},
			run_id="",
			preview={"preview": True},
		)
		self.assertTrue(token)
		self._minted.append(token)
		return token

	def test_reconcile_counts_stranded_dead_token_row(self):
		"""A 'pending' row whose token is DEAD (unknown to Redis) and that expired within
		the rolling window (past the buffer) is counted as stranded - the F6 divergence a
		delivery/resolution failure leaves behind."""
		before = session_lifecycle.reconcile_action_cards()["stranded"]
		self._insert_pending_row("dead_tok_stranded_xyz", self._stranded_ts())
		after = session_lifecycle.reconcile_action_cards()["stranded"]
		self.assertEqual(after, before + 1)

	def test_reconcile_ignores_live_token_row(self):
		"""A 'pending' row past the buffer but whose token is STILL LIVE is a legit
		in-flight card (we raced its expires_at), NOT stranded - the dead-token
		discriminator is what separates a failure from a slow confirm."""
		token = self._mint_live()
		before = session_lifecycle.reconcile_action_cards()["stranded"]
		self._insert_pending_row(token, self._stranded_ts())
		after = session_lifecycle.reconcile_action_cards()["stranded"]
		self.assertEqual(after, before)

	def test_reconcile_ignores_fresh_row(self):
		"""A 'pending' row that expired only recently (inside the buffer grace) is NOT yet
		stranded - we never race a just-expired card."""
		fresh = frappe.utils.add_to_date(
			frappe.utils.now_datetime(), seconds=-(session_lifecycle._STRANDED_BUFFER_S // 2)
		)
		before = session_lifecycle.reconcile_action_cards()["stranded"]
		self._insert_pending_row("dead_tok_fresh_xyz", fresh)
		after = session_lifecycle.reconcile_action_cards()["stranded"]
		self.assertEqual(after, before)

	def test_reconcile_ignores_row_outside_window(self):
		"""A long-expired 'pending' row (older than the rolling window) is NOT counted, so
		'stranded' is a RATE of recent failures, not a monotonic backlog of ancient cruft."""
		old = frappe.utils.add_to_date(
			frappe.utils.now_datetime(), seconds=-(session_lifecycle._STRANDED_WINDOW_S + 3600)
		)
		before = session_lifecycle.reconcile_action_cards()["stranded"]
		self._insert_pending_row("dead_tok_ancient_xyz", old)
		after = session_lifecycle.reconcile_action_cards()["stranded"]
		self.assertEqual(after, before)

	def test_reconcile_returns_cards_open_from_gauge(self):
		"""The reconcile result carries the live open-card gauge (a minted card registers
		on it), so the hourly tick measures both open + stranded."""
		before = session_lifecycle.reconcile_action_cards()["cards_open"]
		self._mint_live()
		after = session_lifecycle.reconcile_action_cards()["cards_open"]
		self.assertEqual(after, before + 1)

	def test_reconcile_alerts_over_threshold(self):
		"""Past the stranded threshold, the tick raises a Google-able alert (frappe.log_error
		with a fixed title) so a delivery-failure spike pages someone."""
		self._insert_pending_row("dead_tok_alert_xyz", self._stranded_ts())
		# Force not-deduped so the assertion is isolated from any real Error Log the cron may
		# have left on this site within the dedup window.
		with (
			patch.object(session_lifecycle, "_STRANDED_ALERT", 0),
			patch.object(session_lifecycle, "_action_card_alert_deduped", return_value=False),
			patch.object(frappe, "log_error") as le,
		):
			session_lifecycle.reconcile_action_cards()
		titles = [(c.kwargs.get("title") or (c.args[0] if c.args else "")) for c in le.call_args_list]
		self.assertIn(session_lifecycle._ACTION_CARD_ALERT_TITLE, titles)

	def test_reconcile_alert_suppressed_when_deduped(self):
		"""When an alert already fired within the dedup window, an over-threshold tick does
		NOT re-alert (no hourly Error Log / email spam on a sustained condition)."""
		self._insert_pending_row("dead_tok_dedup_xyz", self._stranded_ts())
		with (
			patch.object(session_lifecycle, "_STRANDED_ALERT", 0),
			patch.object(session_lifecycle, "_action_card_alert_deduped", return_value=True),
			patch.object(frappe, "log_error") as le,
		):
			session_lifecycle.reconcile_action_cards()
		titles = [(c.kwargs.get("title") or (c.args[0] if c.args else "")) for c in le.call_args_list]
		self.assertNotIn(session_lifecycle._ACTION_CARD_ALERT_TITLE, titles)

	def test_reconcile_redis_outage_does_not_fabricate_stranded(self):
		"""C1 fail-safe: during a Redis OUTAGE the token store's default read SWALLOWS the
		error and returns None (indistinguishable from a dead token). reconcile must use the
		strict read (which RAISES) so a recently-expired pending row is NOT counted as
		stranded - an outage must never fabricate a false 'delivery-failure' alert."""

		def _peek_outage(token, *, strict=False):
			if strict:
				raise pending_confirm.PendingConfirmStorageError("redis down")
			return None  # the buggy swallow the strict read must avoid

		self._insert_pending_row("dead_tok_outage_xyz", self._stranded_ts())
		with patch("jarvis.chat.pending_confirm.peek", side_effect=_peek_outage):
			result = session_lifecycle.reconcile_action_cards()
		self.assertEqual(result["stranded"], 0)  # nothing counted while the store is down

	def test_reconcile_no_alert_under_threshold(self):
		"""Under both thresholds the tick is silent (logs the metric, raises no alert)."""
		self._insert_pending_row("dead_tok_quiet_xyz", self._stranded_ts())
		with (
			patch.object(session_lifecycle, "_STRANDED_ALERT", 10**9),
			patch.object(frappe, "log_error") as le,
		):
			session_lifecycle.reconcile_action_cards()
		titles = [(c.kwargs.get("title") or (c.args[0] if c.args else "")) for c in le.call_args_list]
		self.assertNotIn("action-card health threshold exceeded", titles)

	def test_recheck_source_emits_rescue_signal(self):
		"""A manual re-check (source="recheck") that SURFACES a card emits the
		action_card_rescue signal - the live delivery path missed it, and now that miss is
		measured, not asserted."""
		from jarvis.chat.actions_api import list_pending_confirmations

		self._mint_live()
		with patch("jarvis.chat.latency.get_logger") as gl:
			r = list_pending_confirmations(conversation=self.conv, source="recheck")
		self.assertTrue(r["ok"])
		self.assertTrue(r["data"]["pending"])  # a card was surfaced
		msgs = [c.args[0] for c in gl.return_value.info.call_args_list if c.args]
		self.assertTrue(any("action_card_rescue" in m for m in msgs))

	def test_no_source_emits_no_rescue_signal(self):
		"""An ordinary resync (no source) never emits the rescue signal, even when it
		surfaces a card - only the human-driven lever counts as a rescue."""
		from jarvis.chat.actions_api import list_pending_confirmations

		self._mint_live()
		with patch("jarvis.chat.latency.get_logger") as gl:
			r = list_pending_confirmations(conversation=self.conv)
		self.assertTrue(r["ok"])
		self.assertTrue(r["data"]["pending"])
		msgs = [c.args[0] for c in gl.return_value.info.call_args_list if c.args]
		self.assertFalse(any("action_card_rescue" in m for m in msgs))
