"""Snapshot-isolation races in job/background-worker code, against the real engine
(see ``jarvis.chat.txn`` and ``jarvis.tests.race_harness``).

Three sites (plan rows J4, J5, L1):

* J4 ``compaction.run_compact`` / ``write_compaction_result`` - the compact job's
  transaction owns its snapshot across ``sess.compact_session(...)`` (up to 200 s);
  ``usage.refresh_session_snapshots`` (the admin "sync from agent" sweep) or another
  writer can commit to the SAME ``tabJarvis Chat Session`` row in that window.
* J5 ``cards_enrich.enrich_message`` - a finalize effect: up to 20 record-summary reads
  sit between the read of a reply's ``content`` and the write of the enriched result; a
  competing write on the SAME reply row (a file card, another content write) in that
  window used to be swallowed silently with no log.
* L1 ``turn_handler`` (the legacy worker, live when ``admission.turn_machine_enabled()``
  is false for a shard - ``jarvis.chat.worker.run_agent_turn``): the ``record_tool_owner``
  call and the terminal writes are all in an RQ job that owns its transaction.

Every test arms the real race: a plain read (or the unit's own first read) opens the
snapshot the job would hold, a competing write commits from a genuine second connection
at the exact interleaving point, then the real unit runs. ``assert_fix_closes_race`` runs
each scenario with the defences in ``jarvis.chat.txn`` switched off (the race must bite,
or an outcome the unpatched code swallows must fail ``check``) and on (the outcome must
be correct), so none of these can pass without the fix.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import _record_summary, cards_enrich, compaction, turn_handler
from jarvis.chat.worker import run_agent_turn
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.race_harness import (
	as_job,
	assert_fix_closes_race,
	open_read_view,
	other_connection,
	snapshot_isolation_on,
)
from jarvis.tests.test_chat_api import TEST_USER as WORKER_TEST_USER
from jarvis.tests.test_chat_api import _cleanup_user_conversations, _ensure_test_user
from jarvis.tests.test_chat_worker import _fake_event_stream, _make_conversation_with_user_message

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
CHAT_SESSION = "Jarvis Chat Session"

# A dedicated user for the J4/J5/L1(a) fixtures below (L1(b) reuses test_chat_worker's
# own TEST_USER + conversation-creation helpers, imported as WORKER_TEST_USER - that
# infra already builds a real conversation + user message the right way).
JOBS_TEST_USER = "jarvis-race-jobs@example.com"

# A file card, standing for what api._maybe_attach_artifact writes on an export
# (jarvis#1449, the production stall these fixes close).
CARD = json.dumps([{"file_url": "/private/files/race.xlsx", "name": "race.xlsx"}])


def setUpModule():
	install_synthetic_runtime_profile()


def _ensure_jobs_test_user() -> None:
	if frappe.db.exists("User", JOBS_TEST_USER):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": JOBS_TEST_USER,
			"first_name": "Race",
			"last_name": "Jobs",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.add_roles("System Manager", "Jarvis User")
	frappe.db.commit()


def _cleanup_jobs_fixtures() -> None:
	"""The units under test commit (background job code), so a FrappeTestCase rollback
	cannot undo their fixtures - delete explicitly, by owner/prefix, same as
	test_pump.py's ``_cleanup``."""
	convs = frappe.get_all(CONV, filters={"owner": JOBS_TEST_USER}, pluck="name")
	if convs:
		frappe.db.delete(MSG, {"conversation": ["in", convs]})
	for name in convs:
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	for name in frappe.get_all(
		CHAT_SESSION, filters={"session_key": ["like", "race:compact:%"]}, pluck="name"
	):
		frappe.delete_doc(CHAT_SESSION, name, ignore_permissions=True, force=True)
	frappe.db.commit()


def _block(cards: list[dict], title: str = "Records") -> str:
	"""A ```jarvis-cards fence carrying ``cards``, as the agent would emit it."""
	payload = json.dumps({"title": title, "cards": cards})
	return f"Here you go:\n\n```jarvis-cards\n{payload}\n```"


def _first_card(content: str) -> dict:
	m = cards_enrich._CARDS_RE.search(content)
	return json.loads(m.group(1))["cards"][0]


class TestCompactionWriteRace(FrappeTestCase):
	"""J4: ``jarvis/chat/compaction.py`` ``run_compact`` (its transaction owns the
	snapshot from its first read, the conversation lookup at the top of the function,
	across ``sess.compact_session``'s up to ``COMPACT_RPC_TIMEOUT_S`` = 200 s RPC) and
	``write_compaction_result``. ``usage._write_budget_fields`` (jarvis/chat/usage.py:
	313-345) and ``write_compaction_result``'s own ``compaction_count`` UPDATE are both
	absolute/GREATEST writes keyed by ``session_key`` - no read-modify-write of a value
	cached earlier in the transaction - so a replay from a fresh snapshot is correct,
	not merely a best-effort retry."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_jobs_test_user()

	def setUp(self):
		# set_user_and_timestamps overwrites an "owner" passed in the insert dict with
		# frappe.session.user on every insert - so _cleanup_jobs_fixtures' `owner ==
		# JOBS_TEST_USER` filter only matches fixtures when THIS is the session user.
		self._orig_user = frappe.session.user
		frappe.set_user(JOBS_TEST_USER)

	def tearDown(self):
		_cleanup_jobs_fixtures()
		frappe.set_user(self._orig_user)

	def _mk_conv(self, session_key: str) -> str:
		conv = frappe.get_doc(
			{
				"doctype": CONV,
				"title": "snapshot-race compact",
				"session_key": session_key,
			}
		)
		conv.insert(ignore_permissions=True)
		frappe.get_doc({"doctype": CHAT_SESSION, "session_key": session_key, "user": JOBS_TEST_USER}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		return conv.name

	def test_admin_sync_write_during_compact_rpc_is_replayed(self):
		def scenario():
			key = f"race:compact:{frappe.generate_hash(length=8)}"
			conv = self._mk_conv(key)
			sess = MagicMock()
			sess.list_sessions.return_value = [
				{
					"key": key,
					"totalTokens": 58000,
					"contextTokens": 200000,
					"contextBudgetStatus": {"route": "fits", "reserveTokens": 20000},
				}
			]

			def _compact(*_a, **_kw):
				# The RPC itself: while it is "in flight" the admin sync sweep
				# (usage.refresh_session_snapshots) commits a write to the SAME
				# session row from a second connection - the real interleaving the
				# transaction is exposed to for up to COMPACT_RPC_TIMEOUT_S.
				with other_connection() as other:
					other.sql(
						f"UPDATE `tab{CHAT_SESSION}` SET budget_route=%s WHERE session_key=%s",
						("admin-sync", key),
					)
					other.commit()
				return {"state": "final", "text": "⚙️ Compacted (58k before)", "run_id": "r"}

			sess.compact_session.side_effect = _compact
			pub = MagicMock()
			with (
				snapshot_isolation_on(),
				as_job(),
				patch("jarvis.chat.agent_session_pool.checkout") as co,
				patch("jarvis.chat.compaction.publish_to_user", pub),
				patch.object(frappe, "log_error"),
			):
				co.return_value.__enter__.return_value = sess
				open_read_view()  # stands for run_compact's own first read (the conversation lookup)
				compaction.run_compact(conv, JOBS_TEST_USER, "keep invoices")
			return key, pub

		def check(res):
			key, pub = res
			row = frappe.db.get_value(
				CHAT_SESSION,
				{"session_key": key},
				["compaction_count", "last_compacted_at", "budget_route", "reserve_tokens"],
				as_dict=True,
			)
			self.assertEqual(row.compaction_count, 1)
			self.assertIsNotNone(row.last_compacted_at)
			# The compact result is the newer, authoritative write and must win over the
			# mid-flight admin-sync write, not be lost to (or merged with) it.
			self.assertEqual(row.budget_route, "fits")
			self.assertEqual(row.reserve_tokens, 20000)
			kinds = [c.args[1]["kind"] for c in pub.call_args_list]
			self.assertIn("context:compacted", kinds)
			self.assertNotIn("context:compact_failed", kinds)

		assert_fix_closes_race(self, scenario, check)


class TestCompactionClearLockRace(FrappeTestCase):
	"""J4 continued: ``compaction._clear_lock``, called from ``run_compact``'s
	``finally`` after up to ``COMPACT_RPC_TIMEOUT_S`` (200 s) of RPC time. On the
	declined/failed/exception paths nothing has refreshed the job's snapshot before
	this write, so a competing write to the SAME conversation row (autotitle, another
	compact attempt's own CAS) in that window used to be able to leave the
	``compacting_since`` lock stuck for the full ``COMPACT_LOCK_SECONDS`` (300 s)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_jobs_test_user()

	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user(JOBS_TEST_USER)

	def tearDown(self):
		_cleanup_jobs_fixtures()
		frappe.set_user(self._orig_user)

	def _mk_locked_conv(self) -> str:
		conv = frappe.get_doc({"doctype": CONV, "title": "snapshot-race clear lock"})
		conv.insert(ignore_permissions=True)
		frappe.db.set_value(
			CONV, conv.name, "compacting_since", frappe.utils.now_datetime(), update_modified=False
		)
		frappe.db.commit()
		return conv.name

	def test_clear_lock_races_a_title_write_and_is_replayed(self):
		def scenario():
			conv = self._mk_locked_conv()

			def _race():
				with other_connection() as other:
					other.sql(f"UPDATE `tab{CONV}` SET title=%s WHERE name=%s", ("renamed mid-compact", conv))
					other.commit()

			with snapshot_isolation_on(), as_job(), patch.object(frappe, "log_error"):
				open_read_view()  # stands for run_compact's own first read
				_race()
				compaction._clear_lock(conv)
			return conv

		def check(conv):
			row = frappe.db.get_value(CONV, conv, ["compacting_since", "title"], as_dict=True)
			self.assertIsNone(row.compacting_since, "the lock must clear despite the race")
			self.assertEqual(row.title, "renamed mid-compact", "the competing title write must survive")

		assert_fix_closes_race(self, scenario, check)


class TestCardsEnrichWriteRace(FrappeTestCase):
	"""J5: ``jarvis/chat/cards_enrich.py`` ``enrich_message``, a finalize effect (the
	finalize runner commits before each effect, so this job owns its transaction). Up to
	``_MAX_CARDS`` (20) permission-checked record-summary reads sit between the read of
	``content`` and the write of the enriched result; a competing write on the SAME reply
	row in that window (a file card from ``api._maybe_attach_artifact``, or another
	content write) used to be swallowed by a bare ``except Exception:
	frappe.clear_messages()`` with no log, losing the enrichment silently forever."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_jobs_test_user()

	def setUp(self):
		# See TestCompactionWriteRace.setUp: set_user_and_timestamps stamps owner from
		# the session user on insert regardless of what the dict says.
		self._orig_user = frappe.session.user
		frappe.set_user(JOBS_TEST_USER)

	def tearDown(self):
		_cleanup_jobs_fixtures()
		frappe.set_user(self._orig_user)

	def _mk_message(self, content: str) -> tuple[str, str]:
		conv = frappe.get_doc({"doctype": CONV, "title": "snapshot-race cards"})
		conv.insert(ignore_permissions=True)
		msg = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv.name,
				"seq": 1,
				"role": "assistant",
				"content": content,
			}
		)
		msg.insert(ignore_permissions=True)
		frappe.db.commit()
		return conv.name, msg.name

	def test_content_write_races_a_canvas_write_mid_enrichment_and_is_replayed(self):
		def scenario():
			content = _block([{"doctype": "ToDo", "name": "does-not-matter", "title": "does-not-matter"}])
			_conv, amsg = self._mk_message(content)
			rows = [{"label": "Status", "value": "Open"}]
			fired: list = []

			def _race():
				with other_connection() as other:
					other.sql(f"UPDATE `tab{MSG}` SET canvas=%s WHERE name=%s", (CARD, amsg))
					other.commit()

			def _summary_rows(_doctype, _name):
				# Stands in for the up-to-20 record-summary reads: the competing write
				# lands DURING this window, between our read of `content` (already
				# done by this point) and our write of the enriched result - the real
				# shape of the race (an export's file card lands on this reply row
				# while cards_enrich is still reading records to fill an id-only
				# card). Fires once so the replay's second pass runs clean.
				if not fired:
					fired.append(1)
					_race()
				return {"title": "n", "rows": rows}

			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(_record_summary, "summary_rows", _summary_rows),
				patch.object(frappe, "log_error"),
			):
				open_read_view()  # stands for the transaction's first read, reused by enrich_message's own
				cards_enrich.enrich_message(amsg, owner=JOBS_TEST_USER)
			return amsg, rows

		def check(res):
			amsg, rows = res
			row = frappe.db.get_value(MSG, amsg, ["content", "canvas"], as_dict=True)
			card = _first_card(row.content)
			self.assertEqual(card.get("fields"), rows, "enrichment must land despite the race")
			self.assertEqual(row.canvas, CARD, "the competing canvas write must survive")

		assert_fix_closes_race(self, scenario, check)


class TestTurnHandlerToolOwnerRace(FrappeTestCase):
	"""L1(a): ``jarvis/chat/turn_handler.py`` ``_handle_event_inner``, the
	``record_tool_owner`` unit. The legacy worker (``jarvis.chat.worker.run_agent_turn``,
	live when ``admission.turn_machine_enabled()`` is false for a shard) is an RQ job, so
	it owns its transaction. ``batcher.flush()`` just above the unit can be a no-op
	(nothing pending), leaving ``record_tool_owner``'s ``FOR UPDATE`` on the
	transaction's existing snapshot; a web request's jarvis__* tool commits the export
	file card on the SAME reply row (``api._maybe_attach_artifact``) a few ms before the
	tool's own end event arrives - the exact interleaving that stalled exports ~80 s in
	production (jarvis#1449)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_jobs_test_user()

	def setUp(self):
		# See TestCompactionWriteRace.setUp: set_user_and_timestamps stamps owner from
		# the session user on insert regardless of what the dict says.
		self._orig_user = frappe.session.user
		frappe.set_user(JOBS_TEST_USER)

	def tearDown(self):
		_cleanup_jobs_fixtures()
		frappe.set_user(self._orig_user)

	def _mk_conv_and_msg(self) -> tuple[str, str]:
		conv = frappe.get_doc({"doctype": CONV, "title": "snapshot-race tool owner"})
		conv.insert(ignore_permissions=True)
		msg = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv.name,
				"seq": 1,
				"role": "assistant",
				"content": "",
			}
		)
		msg.insert(ignore_permissions=True)
		frappe.db.commit()
		return conv.name, msg.name

	def test_record_tool_owner_races_a_file_card_write_and_is_replayed(self):
		def scenario():
			conv, amsg = self._mk_conv_and_msg()
			event = {
				"kind": "tool",
				"phase": "end",
				"tool_name": "jarvis__export_query",
				"tool_call_id": "call_export",
			}

			def _race():
				with other_connection() as other:
					other.sql(f"UPDATE `tab{MSG}` SET canvas=%s WHERE name=%s", (CARD, amsg))
					other.commit()

			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(frappe, "log_error"),
				patch("jarvis.chat.worker.publish_to_user"),
			):
				open_read_view()  # stands for the batcher.flush() no-op that precedes this unit
				_race()
				turn_handler._handle_event_inner(
					event,
					conversation_id=conv,
					assistant_msg_name=amsg,
					tool_msg_by_call_id={},
					user=JOBS_TEST_USER,
					run_id="race-tool-owner",
					batcher=turn_handler._AssistantContentBatcher(amsg),
				)
			return amsg

		def check(amsg):
			row = frappe.db.get_value(MSG, amsg, ["tool_call_ids", "canvas"], as_dict=True)
			self.assertEqual(json.loads(row.tool_call_ids or "[]"), ["call_export"])
			self.assertEqual(row.canvas, CARD, "the competing file-card write must survive")

		assert_fix_closes_race(self, scenario, check)


class TestTurnHandlerBatcherFlushRace(FrappeTestCase):
	"""L1(b): ``jarvis/chat/turn_handler.py`` ``_AssistantContentBatcher.flush``. Drives
	the batcher directly (no gateway/session plumbing needed - flush is a standalone
	unit): buffer pending text, open the transaction's snapshot, commit a competing
	write on the SAME reply row from a second connection, then flush. The buffered text
	can span up to ``_ASSISTANT_BATCH_INTERVAL_MS`` since the transaction's last commit,
	so a competing write in that window (a jarvis__* tool's file card, another content
	write) must be replayed, not lost."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_jobs_test_user()

	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user(JOBS_TEST_USER)

	def tearDown(self):
		_cleanup_jobs_fixtures()
		frappe.set_user(self._orig_user)

	def _mk_conv_and_msg(self) -> tuple[str, str]:
		conv = frappe.get_doc({"doctype": CONV, "title": "snapshot-race batcher flush"})
		conv.insert(ignore_permissions=True)
		msg = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv.name,
				"seq": 1,
				"role": "assistant",
				"content": "",
				"streaming": 1,
			}
		)
		msg.insert(ignore_permissions=True)
		frappe.db.commit()
		return conv.name, msg.name

	def test_flush_races_a_file_card_write_and_is_replayed(self):
		def scenario():
			_conv, amsg = self._mk_conv_and_msg()
			batcher = turn_handler._AssistantContentBatcher(amsg)
			batcher.delta("streamed answer so far")

			def _race():
				with other_connection() as other:
					other.sql(f"UPDATE `tab{MSG}` SET canvas=%s WHERE name=%s", (CARD, amsg))
					other.commit()

			with snapshot_isolation_on(), as_job():
				open_read_view()  # stands for whatever earlier read opened this transaction's snapshot
				_race()
				batcher.flush()
			return amsg

		def check(amsg):
			row = frappe.db.get_value(MSG, amsg, ["content", "canvas"], as_dict=True)
			self.assertEqual(row.content, "streamed answer so far")
			self.assertEqual(row.canvas, CARD, "the competing file-card write must survive")

		assert_fix_closes_race(self, scenario, check)


class TestTurnHandlerTerminalWriteRace(FrappeTestCase):
	"""L1(b): ``jarvis/chat/turn_handler.py`` ``handle_chat_send``, the "stopped"
	terminal-write unit (``terminal.get("state") == "aborted"``). The worker is an RQ
	job (owns its transaction) that can run for the whole turn since the transaction's
	last commit before this write; without a fresh snapshot, a competing write on the
	SAME reply row escapes to the outer ``except Exception`` backstop and marks a turn
	errored that actually stopped cleanly. Drives ``jarvis.chat.worker.run_agent_turn``
	end to end - the smallest real function boundary that reaches this write without
	reimplementing the session/stream plumbing - same fixtures as
	``test_chat_worker.TestRunAgentTurnRelayTerminals``."""

	def setUp(self):
		from jarvis.chat import agent_session_pool

		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(WORKER_TEST_USER)

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_stopped_write_races_a_file_card_write_and_is_replayed(self):
		def scenario():
			from jarvis.chat import agent_session_pool

			# assert_fix_closes_race calls scenario() TWICE in this one test method;
			# setUp's _POOL.clear() only covers the FIRST call. Without clearing again
			# here, the second call's checkout() hands back the FIRST call's pooled
			# entry (a "healthy", now-exhausted fake_sess) instead of calling the
			# freshly-patched AgentSession.connect below - relay_turn_events then
			# yields nothing, the turn falls through to relay:interrupted instead of
			# the aborted/stopped branch under test, and this looks like the fix not
			# working when it is really a stale pooled connection.
			agent_session_pool._POOL.clear()
			_cleanup_user_conversations()
			conv, user_msg = _make_conversation_with_user_message()

			def _race():
				amsg = frappe.db.get_value(MSG, {"conversation": conv, "role": "assistant"}, "name")
				with other_connection() as other:
					other.sql(f"UPDATE `tab{MSG}` SET canvas=%s WHERE name=%s", (CARD, amsg))
					other.commit()

			fake_sess = MagicMock()

			def _send(_sk, _msg, idem, **_kw):
				# chat_send is the ack RPC: the assistant placeholder row already
				# exists and is committed by this point (_create_assistant_
				# placeholder, well before this call), so a competing writer (a
				# jarvis__* tool's web request) can land on it here - between the
				# transaction's last commit and the terminal write below.
				_race()
				return {"runId": idem, "status": "started"}

			fake_sess.chat_send.side_effect = _send
			fake_sess.relay_turn_events.return_value = _fake_event_stream(
				[{"kind": "relay:error", "state": "aborted", "text": "partial answer"}]
			)
			with (
				snapshot_isolation_on(),
				as_job(),
				patch("jarvis.chat.agent_session_pool.AgentSession.connect", return_value=fake_sess),
				patch("jarvis.chat.worker.publish_to_user") as pub,
			):
				run_agent_turn(conv, user_msg, run_id="race-stopped")
			amsg = frappe.db.get_value(MSG, {"conversation": conv, "role": "assistant"}, "name")
			return amsg, pub

		def check(res):
			amsg, pub = res
			row = frappe.db.get_value(MSG, amsg, ["stopped", "streaming", "canvas"], as_dict=True)
			self.assertEqual(int(row.stopped), 1)
			self.assertEqual(int(row.streaming), 0)
			self.assertEqual(row.canvas, CARD, "the competing file-card write must survive")
			kinds = [c.args[1]["kind"] for c in pub.call_args_list]
			self.assertIn("run:end", kinds)

		assert_fix_closes_race(self, scenario, check)
