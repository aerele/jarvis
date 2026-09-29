"""Snapshot-isolation races in the chat pump, against the real engine.

THE BUG THESE PIN. A spreadsheet export stalled ~80 s on "Finishing in the
background". The web request that ran ``jarvis__export_query`` wrote the file card on
the reply row (``api._maybe_attach_artifact``) and committed; 27 ms later the pump
applied the tool's end event and locked the same row (``tool_ownership.
record_tool_owner``) on a snapshot taken earlier in its 0.2 s slice. MariaDB 11.6+
refused the lock with ER_CHECKREAD 1020, the relay quarantined the turn, and only the
next pump hop recovered it (20 stalls in the local logs, 11 traced to this lock).

Every test arms the real race (``race_harness``): a plain read opens the snapshot the
pump would hold, a second connection commits the competing write, then the pump code
runs. ``assert_fix_closes_race`` runs each scenario with the defences in
``jarvis.chat.txn`` switched off (the race must bite) and on (the outcome must be
right), so none of these can pass without the fix.
"""

import json
from unittest.mock import patch

import frappe

from jarvis.chat import pump, settlement
from jarvis.chat import turn_state as ts
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.race_harness import (
	as_job,
	assert_fix_closes_race,
	open_read_view,
	other_connection,
	snapshot_isolation_on,
)
from jarvis.tests.test_pump import CONV, MSG, PUMP, TARGET_PREFIX, TEST_USER, TURN, _PumpTestCase, _Recorder

CARD = json.dumps(
	[{"file_url": "/private/files/Overdue-Sales-Invoices.xlsx", "name": "Overdue-Sales-Invoices.xlsx"}]
)


def setUpModule():
	install_synthetic_runtime_profile()


def _compete(sql: str, params: tuple) -> None:
	"""Commit ``sql`` from a second connection, the way a web request would."""
	with other_connection() as other:
		other.sql(sql, params)
		other.commit()


def _attach_card(amsg: str) -> None:
	"""What ``api._maybe_attach_artifact`` does for an export: the file card lands on the
	reply row and is committed."""
	_compete(f"UPDATE `tab{MSG}` SET canvas=%s WHERE name=%s", (CARD, amsg))


def _published(rec: _Recorder) -> list[str]:
	return [c[0][1] for c in rec.calls]


def _race_once(real, after=None, before=None):
	"""Wrap ``real`` so its FIRST call runs the race around it: ``before`` ahead of the
	call, ``after`` right after it. Later calls (the replay) pass straight through."""
	fired: list = []

	def wrapper(*args, **kwargs):
		first = not fired
		fired.append(1)
		if first and before:
			before()
		result = real(*args, **kwargs)
		if first and after:
			after()
		return result

	return wrapper


class _RaceCase(_PumpTestCase):
	def _new_shard(self) -> None:
		"""A fresh shard per scenario run: ``assert_fix_closes_race`` runs each scenario twice
		and the first run's lease (or a hop that died holding it) must not block the second.
		Same setup as ``_PumpTestCase.setUp``; ``_cleanup`` removes every ``pmpx_`` shard."""
		self._target = f"{TARGET_PREFIX}{frappe.generate_hash(length=10)}"
		ts._ensure_control_row(self._target)
		frappe.db.set_value(PUMP, self._target, "transport_mode", pump._MODE_PUMP, update_modified=False)
		frappe.db.commit()
		pump._LIFECYCLE_MODE_CACHE.pop(self._target, None)
		ts.reset_lock_tracking()

	def _streaming_turn(self, state: str = "streaming"):
		self._new_shard()
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="Export my overdue sales invoices to an Excel file.")
		amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
		ctx = self._make_ctx(self._deps(), with_mux=False)
		rid = f"pmpr_{frappe.generate_hash(length=8)}"
		self._mk_turn(
			conv, rid, seed, state, version=4, pump_epoch=ctx.epoch, assistant_message=amsg, last_event_seq=0
		)
		rs = pump._RunState(
			run_id=rid,
			conversation=conv,
			owner=TEST_USER,
			assistant_message=amsg,
			session_key=f"sess-{rid}",
			version=4,
			gateway_run_id=rid,
		)
		return ctx, rs, conv, amsg


class TestToolEventRace(_RaceCase):
	def test_the_export_stall_tool_end_after_the_file_card_lands(self):
		def scenario():
			ctx, rs, _conv, amsg = self._streaming_turn()
			pub = _Recorder()
			with snapshot_isolation_on(), as_job(), patch.object(ts, "publish_fenced", pub):
				open_read_view()  # the slice's _dispatch_ready read
				_attach_card(amsg)  # the tool's web request commits the file card
				pump._default_apply_tool(
					ctx,
					rs,
					{
						"phase": "end",
						"tool_name": "jarvis__export_query",
						"tool_call_id": "call_export",
						"event_seq": 5,
					},
				)
			return rs, amsg, pub

		def check(res):
			rs, amsg, pub = res
			row = frappe.db.get_value(MSG, amsg, ["canvas", "tool_call_ids"], as_dict=True)
			self.assertEqual(json.loads(row.tool_call_ids or "[]"), ["call_export"])
			self.assertEqual(row.canvas, CARD, "the file card must survive the tool event")
			self.assertEqual(int(self._val(rs.run_id, "last_event_seq")), 5)
			self.assertEqual(_published(pub), ["tool:end"])

		assert_fix_closes_race(self, scenario, check)

	def test_a_race_inside_the_fenced_step_is_replayed_once_with_no_duplicates(self):
		# A built-in tool's start takes the conversation lock too (need_lock), so this
		# also proves the replay re-takes it cleanly under the lock-order assertion.
		def scenario():
			ctx, rs, conv, amsg = self._streaming_turn()
			pub = _Recorder()

			def race():
				open_read_view()
				_attach_card(amsg)

			fence = _race_once(ts.apply_tool_fenced, after=race)
			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(ts, "publish_fenced", pub),
				patch.object(ts, "apply_tool_fenced", fence),
			):
				pump._default_apply_tool(
					ctx,
					rs,
					{"phase": "start", "tool_name": "browser", "tool_call_id": "call_browse", "event_seq": 3},
				)
			return rs, conv, amsg, pub

		def check(res):
			rs, conv, amsg, pub = res
			self.assertEqual(
				json.loads(frappe.db.get_value(MSG, amsg, "tool_call_ids") or "[]"), ["call_browse"]
			)
			self.assertEqual(
				frappe.db.count(MSG, {"conversation": conv, "role": "tool", "tool_call_id": "call_browse"}), 1
			)
			self.assertEqual(int(self._val(rs.run_id, "last_event_seq")), 3)
			self.assertEqual(int(self._val(rs.run_id, "version")), 5, "the fence advanced exactly once")
			self.assertEqual(rs.version, 5)
			self.assertEqual(_published(pub), ["tool:start"])

		assert_fix_closes_race(self, scenario, check)


class TestSliceAndHop(_RaceCase):
	def test_a_slice_enters_dispatch_without_the_prelude_snapshot(self):
		def scenario():
			self._new_shard()
			conv = self._mk_conv()
			ctx = self._make_ctx(self._deps(double=self._double()))
			probed: list = []

			def probe_dispatch(block_s=0):
				# Stands in for any event handler: a web request committed a write to a row
				# since the slice began, and the handler now locks that row.
				_compete(f"UPDATE `tab{CONV}` SET title=%s WHERE name=%s", ("renamed", conv))
				frappe.db.sql(f"SELECT name FROM `tab{CONV}` WHERE name=%s FOR UPDATE", conv)
				frappe.db.commit()
				probed.append(1)
				return 0

			with snapshot_isolation_on(), as_job(), patch.object(ctx.mux, "dispatch", probe_dispatch):
				pump.drain_slice(ctx)
			return probed

		assert_fix_closes_race(self, scenario, lambda probed: self.assertEqual(probed, [1]))

	def test_a_lost_race_in_a_slice_step_does_not_end_the_hop(self):
		def scenario():
			self._new_shard()
			conv = self._mk_conv()
			calls: list = []
			real = pump.drain_slice

			def racing_slice(ctx):
				calls.append(1)
				if len(calls) == 1:
					open_read_view()
					_compete(f"UPDATE `tab{CONV}` SET title=%s WHERE name=%s", ("renamed", conv))
					frappe.db.sql(f"SELECT name FROM `tab{CONV}` WHERE name=%s FOR UPDATE", conv)
				return real(ctx)

			deps = self._deps(double=self._double())
			with snapshot_isolation_on(), as_job(), patch.object(pump, "drain_slice", racing_slice):
				out = pump.run_pump_hop(self._target, deps=deps, max_slices=3)
			return out, calls

		def check(res):
			out, calls = res
			self.assertTrue(out["acquired"])
			self.assertNotEqual(out["exit"], "lease_lost")
			self.assertGreaterEqual(len(calls), 2, "the hop must go on to the next slice")

		assert_fix_closes_race(self, scenario, check)

	def test_a_race_that_keeps_winning_backs_off_then_stops_retrying(self):
		# Loop control around the slice-level replay, with a real engine conflict on EVERY
		# slice: it must pause a little longer each time and stop after the cap instead of
		# spinning against a hot row.
		self._new_shard()
		conv = self._mk_conv()
		calls: list = []
		sleeps: list = []

		def always_racing(ctx):
			calls.append(1)
			open_read_view()
			_compete(f"UPDATE `tab{CONV}` SET title=%s WHERE name=%s", (f"renamed {len(calls)}", conv))
			frappe.db.sql(f"SELECT name FROM `tab{CONV}` WHERE name=%s FOR UPDATE", conv)
			return "continue"

		deps = self._deps(double=self._double())
		with (
			snapshot_isolation_on(),
			as_job(),
			patch.object(pump, "drain_slice", always_racing),
			patch.object(pump, "_sleep", sleeps.append),
		):
			try:
				out = pump.run_pump_hop(self._target, deps=deps, max_slices=50)
			except frappe.QueryDeadlockError:
				out = None  # no hop-level catch-all yet: the capped conflict propagates
		self.assertEqual(len(calls), pump.WRITE_CONFLICT_ATTEMPTS)
		self.assertEqual(
			sleeps, [pump.WRITE_CONFLICT_BACKOFF_S * n for n in range(1, pump.WRITE_CONFLICT_ATTEMPTS)]
		)
		if out is not None:
			# With the M-01 hop-level catch-all (#1526) the capped conflict ends the hop as a crash.
			self.assertEqual(out["exit"], "crashed")


class TestTerminalRaces(_RaceCase):
	def test_settlement_replays_a_race_on_the_reply_projection(self):
		def scenario():
			ctx, rs, conv, amsg = self._streaming_turn()
			frappe.db.set_value(
				TURN,
				rs.run_id,
				{"state": "terminal_observed", "version": 5, "terminal_kind": "relay:final"},
				update_modified=False,
			)
			frappe.db.commit()
			deps = self._deps()
			pub = _Recorder()
			read = _race_once(ts.read_turn, after=lambda: _attach_card(amsg))
			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(ts, "publish_fenced", pub),
				patch.object(ts, "read_turn", read),
			):
				settlement.invoke_settlement(
					rs.run_id,
					relay_target_id=self._target,
					epoch=ctx.epoch,
					version=5,
					terminal_kind="relay:final",
					terminal_payload={"text": "Here is your export."},
					assistant_message=amsg,
					owner=TEST_USER,
					conversation=conv,
					deps=deps,
				)
			return rs, amsg, deps, pub

		def check(res):
			rs, amsg, deps, pub = res
			self.assertEqual(self._state(rs.run_id), "finalizing")
			row = frappe.db.get_value(MSG, amsg, ["content", "canvas", "streaming"], as_dict=True)
			self.assertEqual(row.content, "Here is your export.")
			self.assertEqual(row.canvas, CARD)
			self.assertEqual(int(row.streaming), 0)
			self.assertEqual(deps.enqueue_finalize.count, 1)
			self.assertEqual([k for k in _published(pub) if k.startswith("run:")], ["run:end"])

		assert_fix_closes_race(self, scenario, check)

	def test_a_terminal_settles_after_another_writer_touched_the_turn(self):
		def scenario():
			ctx, rs, _conv, _amsg = self._streaming_turn()
			settle = _Recorder()
			ctx.deps.invoke_settlement = settle
			with snapshot_isolation_on(), as_job():
				open_read_view()
				_compete(f"UPDATE `tab{TURN}` SET modified=NOW(6) WHERE name=%s", (rs.run_id,))
				pump._settle_terminal(ctx, rs, "relay:final", {"text": "Here is your export."})
			return rs, settle

		def check(res):
			rs, settle = res
			self.assertEqual(self._state(rs.run_id), "terminal_observed")
			self.assertEqual(settle.count, 1)

		assert_fix_closes_race(self, scenario, check)

	def test_a_race_at_the_terminal_cas_is_replayed_not_quarantined(self):
		# Races AT the CAS statement: a plain read reopens a snapshot after the fresh one
		# and the competing write lands in between. Only the replay can save this one.
		def scenario():
			ctx, rs, _conv, _amsg = self._streaming_turn()
			settle = _Recorder()
			ctx.deps.invoke_settlement = settle

			def race():
				open_read_view()
				_compete(f"UPDATE `tab{TURN}` SET modified=NOW(6) WHERE name=%s", (rs.run_id,))

			observe = _race_once(ts.mark_terminal_observed, before=race)
			with snapshot_isolation_on(), as_job(), patch.object(ts, "mark_terminal_observed", observe):
				pump._settle_terminal(ctx, rs, "relay:final", {"text": "Here is your export."})
			return rs, settle

		def check(res):
			rs, settle = res
			self.assertEqual(self._state(rs.run_id), "terminal_observed")
			self.assertEqual(int(self._val(rs.run_id, "version")), 5, "the CAS landed exactly once")
			self.assertEqual(rs.version, 5)
			self.assertEqual(settle.count, 1)

		assert_fix_closes_race(self, scenario, check)

	def test_the_last_text_flush_replays_a_race_on_the_reply_row(self):
		def scenario():
			ctx, rs, _conv, amsg = self._streaming_turn()
			rs.pending_seq, rs.pending_text, rs.pending_delta = (
				2,
				"Here is your export.",
				"Here is your export.",
			)
			pub = _Recorder()

			def race():
				open_read_view()
				_attach_card(amsg)

			delta = _race_once(ts.apply_delta, before=race)
			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(ts, "publish_fenced", pub),
				patch.object(ts, "apply_delta", delta),
			):
				pump._flush_deltas(ctx, rs)
			return rs, amsg, pub

		def check(res):
			rs, amsg, pub = res
			row = frappe.db.get_value(MSG, amsg, ["content", "canvas"], as_dict=True)
			self.assertEqual(row.content, "Here is your export.")
			self.assertEqual(row.canvas, CARD)
			self.assertEqual(int(self._val(rs.run_id, "last_event_seq")), 2)
			self.assertEqual(rs.version, 5)
			self.assertEqual(_published(pub), ["assistant:delta"])

		assert_fix_closes_race(self, scenario, check)


class TestNoSwallowedRace(_RaceCase):
	"""Two paths used to swallow a failed stamp and commit anyway. A snapshot race had
	already aborted their CAS server-side, so the commit landed nothing while the code
	carried on as if it had. With the defences off the outcome is wrong, not an error."""

	def test_a_recovered_answer_settles_even_if_its_stamps_lose_a_race(self):
		def scenario():
			ctx, rs, conv, amsg = self._streaming_turn()
			frappe.db.set_value(MSG, amsg, "recovering", 1, update_modified=False)
			frappe.db.commit()
			settle = _Recorder()
			ctx.deps.invoke_settlement = settle

			def race():
				open_read_view()
				_attach_card(amsg)

			observe = _race_once(ts.mark_terminal_observed, after=race)
			r = {"run_id": rs.run_id, "version": 4, "conversation": conv, "assistant_message": amsg}
			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(ts, "mark_terminal_observed", observe),
				patch.object(frappe, "log_error"),
			):
				pump._settle_recovered_final(ctx, r, "Recovered answer.")
			return rs, amsg, settle

		def check(res):
			rs, amsg, settle = res
			self.assertEqual(self._state(rs.run_id), "terminal_observed")
			self.assertEqual(int(self._val(rs.run_id, "was_recovered")), 1)
			self.assertEqual(int(frappe.db.get_value(MSG, amsg, "recovering")), 0)
			self.assertEqual(settle.count, 1)

		assert_fix_closes_race(self, scenario, check)

	def test_a_rejected_dispatch_is_errored_even_if_its_stamp_loses_a_race(self):
		def scenario():
			ctx, rs, _conv, amsg = self._streaming_turn(state="dispatching")
			pub = _Recorder()

			def race():
				open_read_view()
				_attach_card(amsg)

			errored = _race_once(ts.dispatch_errored, after=race)
			exc = AgentUnreachableError(
				"chat.send rejected: policy_denied: not allowed", code="policy_denied"
			)
			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(ts, "publish_fenced", pub),
				patch.object(ts, "dispatch_errored", errored),
				patch.object(pump, "_seal_file_box_sheet"),
				patch.object(frappe, "log_error"),
			):
				pump._handle_ack_failure(ctx, rs, exc)
			return rs, amsg, pub

		def check(res):
			rs, amsg, pub = res
			self.assertEqual(self._state(rs.run_id), "errored")
			row = frappe.db.get_value(MSG, amsg, ["streaming", "error"], as_dict=True)
			self.assertEqual(int(row.streaming), 0)
			self.assertIn("policy_denied", row.error or "")
			self.assertEqual(_published(pub), ["run:error"])

		assert_fix_closes_race(self, scenario, check)
