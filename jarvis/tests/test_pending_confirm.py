"""Tests for jarvis.chat.pending_confirm - the cache-backed token store that
parks a pending mutating tool call until a human confirms it.

No gate wiring here (that is a later task) - just mint/peek/consume and the
args_hash used to bind a token to the exact call.
"""

import contextvars
import threading
from unittest.mock import patch

import frappe
import redis.exceptions
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import pending_confirm

CONV = "conv-1"
OWNER = "owner@example.invalid"
TOOL = "create_doc"
ARGS = {"doctype": "Task", "subject": "hello", "nested": {"b": 2, "a": 1}}
RUN_ID = "run-1"


class TestArgsHash(FrappeTestCase):
	def test_stable_across_key_ordering(self):
		a = {"x": 1, "y": 2, "nested": {"b": 2, "a": 1}}
		b = {"nested": {"a": 1, "b": 2}, "y": 2, "x": 1}
		self.assertEqual(
			pending_confirm.args_hash("some_tool", a),
			pending_confirm.args_hash("some_tool", b),
		)

	def test_differs_when_a_value_changes(self):
		a = {"x": 1}
		b = {"x": 2}
		self.assertNotEqual(
			pending_confirm.args_hash("some_tool", a),
			pending_confirm.args_hash("some_tool", b),
		)

	def test_differs_when_tool_changes(self):
		self.assertNotEqual(
			pending_confirm.args_hash("tool_a", ARGS),
			pending_confirm.args_hash("tool_b", ARGS),
		)


class TestMint(FrappeTestCase):
	def test_two_mints_are_distinct_tokens(self):
		t1 = pending_confirm.mint(
			conversation=CONV,
			owner=OWNER,
			tool=TOOL,
			args=ARGS,
			run_id=RUN_ID,
		)
		t2 = pending_confirm.mint(
			conversation=CONV,
			owner=OWNER,
			tool=TOOL,
			args=ARGS,
			run_id=RUN_ID,
		)
		self.assertNotEqual(t1, t2)
		# Both are independently live.
		self.assertIsNotNone(pending_confirm.peek(t1))
		self.assertIsNotNone(pending_confirm.peek(t2))


class TestMintReliableIndex(FrappeTestCase):
	"""C1: a token can NEVER be persisted without being findable by
	list_for_owner. An unindexed record is invisible to the resync endpoint -> a
	silent, unrecoverable confirmation card (the bulk-create card that never
	rendered). So the owner-index write is required, not best-effort: if it fails
	the record is rolled back (no orphan) and the failure is logged LOUDLY (it was
	a silent try/except: pass)."""

	_A = "owner-c1@example.invalid"

	def setUp(self):
		# The per-owner index lives in Redis (not rolled back with the DB); clear
		# it so a prior run's tokens don't mask these assertions.
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self._A)

	def _mint(self):
		return pending_confirm.mint(
			conversation="conv-c1",
			owner=self._A,
			tool="create_doc",
			args={"docs": [{"doctype": "ToDo", "values": {"description": "c1"}}]},
			run_id="",
			preview={"preview": True, "would": {"created": [{"doctype": "ToDo", "name": "x"}]}},
		)

	def test_mint_persists_and_indexes(self):
		"""A normal mint is BOTH peekable AND retrievable by resync (regression
		guard for the persisted<->indexed invariant)."""
		token = self._mint()
		self.assertIsNotNone(pending_confirm.peek(token))
		self.assertIn(token, {r["token"] for r in pending_confirm.list_for_owner(self._A)})

	def test_index_write_failure_is_logged_not_silent(self):
		"""An owner-index write failure must be LOUD (was swallowed by
		try/except: pass), so the mode is observable instead of invisible."""
		with patch.object(
			frappe.cache(), "sadd", side_effect=redis.exceptions.ConnectionError("simulated redis blip")
		):
			with patch.object(frappe, "log_error") as mock_log:
				self._mint()
		self.assertTrue(mock_log.called, "index-write failure must be logged, not swallowed")

	def test_index_write_failure_leaves_no_orphan(self):
		"""If the token can't be indexed it must NOT be left persisted (invisible
		to resync) - the record is rolled back so no unrecoverable orphan survives
		for the full TTL."""
		with patch.object(
			frappe.cache(), "sadd", side_effect=redis.exceptions.ConnectionError("simulated redis blip")
		):
			with patch.object(frappe, "log_error"):
				token = self._mint()
		self.assertIsNone(pending_confirm.peek(token))
		self.assertNotIn(token, {r["token"] for r in pending_confirm.list_for_owner(self._A)})

	def test_index_write_failure_returns_none(self):
		"""A park that can't be indexed must SIGNAL failure by returning None - NOT
		a token. Returning a token whose record was rolled back made the gate
		publish an un-confirmable card that wedged the turn on an 'expired' toast
		(the whole point of this fix). None lets the gate surface a retryable tool
		error instead."""
		with patch.object(
			frappe.cache(), "sadd", side_effect=redis.exceptions.ConnectionError("simulated redis blip")
		):
			with patch.object(frappe, "log_error"):
				self.assertIsNone(self._mint())

	def test_persist_verify_failure_returns_none(self):
		"""set_value SUPPRESSES a transient redis ConnectionError, so a record can
		silently fail to persist while the index write succeeds. mint reads the
		record back and, finding it absent, must treat the park as failed (return
		None + log) rather than let a card be published against a token whose record
		never landed."""
		with patch.object(pending_confirm, "peek", return_value=None):
			with patch.object(frappe, "log_error") as mock_log:
				self.assertIsNone(self._mint())
		self.assertTrue(mock_log.called, "a persist-verify miss must be logged, not swallowed")

	def test_failed_park_does_not_bump_cards_open_gauge(self):
		"""The cards_open gauge is observability, not authority: a rolled-back park
		must never leave the gauge over-counting a card the user can't see."""
		before = pending_confirm.cards_open_gauge()
		with patch.object(
			frappe.cache(), "sadd", side_effect=redis.exceptions.ConnectionError("simulated redis blip")
		):
			with patch.object(frappe, "log_error"):
				self._mint()
		self.assertEqual(pending_confirm.cards_open_gauge(), before)

	def test_mint_survives_missing_expire_key_on_frappe_v15(self):
		"""Frappe 15 has no ``RedisWrapper.expire_key``. The owner-index
		TTL refresh must not depend on it: when it is unavailable the park still
		succeeds (record persisted + peekable + indexed) instead of the AttributeError
		being caught by the park's try/except and rolling back a good record - which
		took down EVERY gated confirmation card on v15 benches."""
		with patch.object(
			frappe.cache(),
			"expire_key",
			create=True,
			side_effect=AttributeError("'RedisWrapper' object has no attribute 'expire_key'"),
		):
			with patch.object(frappe, "log_error") as mock_log:
				token = self._mint()
		self.assertIsNotNone(token, "park must not depend on expire_key, which is absent on Frappe 15")
		self.assertFalse(mock_log.called, "a missing expire_key must not trip the error/rollback path")
		self.assertIsNotNone(pending_confirm.peek(token))
		self.assertIn(token, {r["token"] for r in pending_confirm.list_for_owner(self._A)})

	def test_mint_refreshes_owner_index_ttl_via_portable_path(self):
		"""The owner-index set gets a fresh TTL on every mint (so an emptied set
		self-expires) via the version-portable raw ``expire`` on the make_key'd key -
		the SAME key ``sadd`` wrote to. Guards the hygiene from silently regressing to
		a no-op (e.g. targeting an un-namespaced or otherwise wrong key)."""
		self._mint()
		cache = frappe.cache()
		ttl = cache.ttl(cache.make_key(pending_confirm._owner_key(self._A)))
		self.assertGreater(ttl, 0, "owner-index set must carry a positive TTL after mint")
		self.assertLessEqual(ttl, pending_confirm._TTL_S)

	def test_mint_rolls_back_when_set_value_swallows_a_write_error(self):
		"""_read_record's local-cache pop is LOAD-BEARING: set_value writes
		frappe.local.cache BEFORE its ConnectionError-suppressed Redis SET, so a
		swallowed write leaves a stale LOCAL copy while Redis holds nothing. mint()'s
		post-persist verify must still detect the miss - _read_record's pop forces a
		fresh Redis read - and roll back rather than publish a card whose record never
		landed. Guards the pop from being dropped (without it the verify reads the
		stale local copy and falsely passes). Simulate by making the raw redis SET
		raise ConnectionError, which set_value suppresses."""
		# set_value writes a TTL key. Frappe 16 always routes that through
		# cache.set(ex=...), but Frappe 15 calls cache.setex() for expiring writes
		# and never touches cache.set, so patching set alone would leave the real
		# SETEX to succeed on v15. Patch both; setex is simply never hit on v16.
		with (
			patch.object(frappe.cache(), "set", side_effect=redis.exceptions.ConnectionError("write blip")),
			patch.object(frappe.cache(), "setex", side_effect=redis.exceptions.ConnectionError("write blip")),
		):
			with patch.object(frappe, "log_error") as mock_log:
				token = self._mint()
		self.assertIsNone(token, "a swallowed set_value write must be caught by the verify -> rollback")
		self.assertTrue(mock_log.called, "a persist-verify miss must be logged, not swallowed")

	def test_mint_retries_a_transient_verification_read_before_rollback(self):
		"""A one-off GET timeout must not destroy a record and owner index that
		both persisted successfully. The strict retry recovers the good park; a
		sustained failure still follows the fail-closed rollback path."""
		cache = frappe.cache()
		real_get = cache.get
		calls = 0

		def _first_read_times_out(*args, **kwargs):
			nonlocal calls
			calls += 1
			if calls == 1:
				raise redis.exceptions.TimeoutError("first verify reply lost")
			return real_get(*args, **kwargs)

		with patch.object(cache, "get", side_effect=_first_read_times_out):
			with patch.object(frappe, "logger") as mock_logger:
				with patch.object(frappe, "log_error") as mock_log_error:
					token = self._mint()
		self.assertIsNotNone(token)
		self.assertEqual(calls, 2)
		self.assertTrue(mock_logger.return_value.error.called)
		self.assertFalse(mock_log_error.called, "a recovered verification read must not roll back")
		self.assertIsNotNone(pending_confirm.peek(token))

	def test_list_for_owner_does_not_prune_a_live_token_on_transient_read_blip(self):
		"""A transient read blip (TimeoutError/ResponseError) on one token during the
		resync loop must NOT prune it from the owner index: the record is still live,
		so pruning orphans it (invisible to every future resync for its TTL = the
		invisible-card class this module guards). The member is skipped this round and
		re-surfaces on a later clean resync."""
		token = self._mint()
		self.assertIn(token, {r["token"] for r in pending_confirm.list_for_owner(self._A)})

		def _timeout(*a, **k):
			raise redis.exceptions.TimeoutError("read blip")

		with patch.object(frappe.cache(), "get", side_effect=_timeout):
			with patch.object(frappe, "logger") as mock_logger:
				blipped = pending_confirm.list_for_owner(self._A)
		self.assertNotIn(token, {r["token"] for r in blipped}, "blipped token is skipped this round")
		self.assertTrue(mock_logger.return_value.error.called, "the skipped card must be observable")
		# NOT orphaned: a later clean resync still finds it (proves it was not pruned).
		self.assertIn(token, {r["token"] for r in pending_confirm.list_for_owner(self._A)})

	def test_strict_list_reports_read_failure_without_pruning_or_returning_empty_success(self):
		"""User-facing resync is strict: a cache outage is not an authoritative
		empty list, because replacing the UI from that result clears a live card."""
		token = self._mint()
		with patch.object(frappe.cache(), "get", side_effect=redis.exceptions.TimeoutError("read blip")):
			with patch.object(frappe, "logger"):
				with self.assertRaises(pending_confirm.PendingConfirmStorageError):
					pending_confirm.list_for_owner(self._A, strict=True)
		self.assertIn(token, {r["token"] for r in pending_confirm.list_for_owner(self._A)})

	def test_strict_list_reports_owner_index_failure(self):
		"""A failed SMEMBERS cannot masquerade as 'this user has no cards'."""
		self._mint()
		with patch.object(
			frappe.cache(), "smembers", side_effect=redis.exceptions.ConnectionError("index blip")
		):
			with patch.object(frappe, "logger"):
				with self.assertRaises(pending_confirm.PendingConfirmStorageError):
					pending_confirm.list_for_owner(self._A, strict=True)

	def test_mint_survives_ttl_refresh_failure(self):
		"""The owner-index TTL refresh is best-effort and sits OUTSIDE the fatal park
		try; if cache.expire() itself fails (a blip, or a Redis ACL that permits SADD
		but denies EXPIRE) the park must still succeed and must NOT hit the
		error/rollback path. Guards against a future edit folding the TTL refresh back
		into the fatal try (the regression class this PR fixes)."""
		with patch.object(frappe.cache(), "expire", side_effect=redis.exceptions.ConnectionError("blip")):
			with patch.object(frappe, "log_error") as mock_log:
				token = self._mint()
		self.assertIsNotNone(token, "a TTL-refresh failure must not roll back a good park")
		self.assertFalse(mock_log.called, "TTL-refresh failure must not trip the park error/rollback path")
		self.assertIsNotNone(pending_confirm.peek(token))


class TestExecUser(FrappeTestCase):
	def test_exec_user_stored_and_returned(self):
		token = pending_confirm.mint(
			conversation=CONV,
			owner=OWNER,
			tool=TOOL,
			args=ARGS,
			run_id=RUN_ID,
			exec_user="tool-user@example.invalid",
		)
		record = pending_confirm.peek(token)
		self.assertEqual(record["owner"], OWNER)
		self.assertEqual(record["exec_user"], "tool-user@example.invalid")
		# consume returns it too.
		got = pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertEqual(got["exec_user"], "tool-user@example.invalid")

	def test_exec_user_defaults_to_owner(self):
		# Managed mode / back-compat: omitting exec_user binds execution to the
		# owner (no behavior change from the pre-exec_user record shape).
		token = pending_confirm.mint(
			conversation=CONV,
			owner=OWNER,
			tool=TOOL,
			args=ARGS,
			run_id=RUN_ID,
		)
		self.assertEqual(pending_confirm.peek(token)["exec_user"], OWNER)


class TestPreview(FrappeTestCase):
	def test_preview_stored_and_returned(self):
		"""F2: the park-time preview is stored so resync can return it verbatim
		(instead of re-running the side-effecting dry-run)."""
		preview = {"preview": True, "would": {"doctype": "Task", "subject": "hi"}}
		token = pending_confirm.mint(
			conversation=CONV,
			owner=OWNER,
			tool=TOOL,
			args=ARGS,
			run_id=RUN_ID,
			preview=preview,
		)
		self.assertEqual(pending_confirm.peek(token)["preview"], preview)
		# consume returns it too.
		self.assertEqual(
			pending_confirm.consume(token, owner=OWNER, conversation=CONV)["preview"],
			preview,
		)

	def test_preview_defaults_to_none(self):
		token = pending_confirm.mint(
			conversation=CONV,
			owner=OWNER,
			tool=TOOL,
			args=ARGS,
			run_id=RUN_ID,
		)
		self.assertIsNone(pending_confirm.peek(token).get("preview"))


class TestListForOwner(FrappeTestCase):
	_A = "owner-a@example.invalid"
	_B = "owner-b@example.invalid"

	def setUp(self):
		# The per-owner index lives in Redis (not rolled back with the DB), so
		# clear these owners' sets to isolate token-count assertions from prior
		# methods/runs.
		for o in (self._A, self._B):
			frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + o)

	def _mint(self, owner, conversation, desc):
		return pending_confirm.mint(
			conversation=conversation,
			owner=owner,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": desc}},
			run_id="",
		)

	def test_returns_only_callers_live_tokens(self):
		t1 = self._mint(self._A, "conv-a1", "la-1")
		t2 = self._mint(self._A, "conv-a2", "la-2")
		self._mint(self._B, "conv-b1", "lb-1")  # another owner's token

		got = pending_confirm.list_for_owner(self._A)
		tokens = {r["token"] for r in got}
		self.assertEqual(tokens, {t1, t2})
		# Every record carries its token + owner and never leaks owner B's.
		for r in got:
			self.assertEqual(r["owner"], self._A)

	def test_filtered_by_conversation(self):
		t1 = self._mint(self._A, "conv-a1", "fc-1")
		self._mint(self._A, "conv-a2", "fc-2")
		got = pending_confirm.list_for_owner(self._A, conversation="conv-a1")
		self.assertEqual([r["token"] for r in got], [t1])

	def test_conversationless_record_surfaces_under_any_filter(self):
		"""F1: a token minted without a resolvable conversation ("") carries no
		binding, so resync (which always passes the SPA's current conversation)
		must still return it - else the card is confirmable live but lost on
		reload for its TTL. A bound record is still filtered out."""
		t_unbound = self._mint(self._A, "", "cl-unbound")
		self._mint(self._A, "conv-other", "cl-bound")
		got = {r["token"] for r in pending_confirm.list_for_owner(self._A, conversation="conv-current")}
		self.assertIn(t_unbound, got)  # surfaces despite the filter
		# The bound record for a different conversation is still excluded.
		self.assertEqual(len(got), 1)

	def test_excludes_expired_and_consumed(self):
		t_live = self._mint(self._A, "conv-a1", "ex-live")
		t_expired = self._mint(self._A, "conv-a1", "ex-expired")
		t_consumed = self._mint(self._A, "conv-a1", "ex-consumed")
		# Expire one by dropping its record; consume another.
		frappe.cache().delete_value(pending_confirm._PREFIX + t_expired)
		pending_confirm.consume(t_consumed, owner=self._A, conversation="conv-a1")

		got_tokens = {r["token"] for r in pending_confirm.list_for_owner(self._A)}
		self.assertEqual(got_tokens, {t_live})
		self.assertNotIn(t_expired, got_tokens)
		self.assertNotIn(t_consumed, got_tokens)

	def test_empty_for_unknown_owner(self):
		self.assertEqual(pending_confirm.list_for_owner("nobody@example.invalid"), [])

	def test_list_items_for_owner_clean_client_shape(self):
		"""C2: the shared item builder returns the client-facing shape and NEVER
		leaks internal fields (args/exec_user/args_hash) - the SAME shape the resync
		endpoint and the run:end terminal both use, so they cannot drift."""
		t = self._mint(self._A, "conv-a1", "items-clean")
		items = pending_confirm.list_items_for_owner(self._A)
		self.assertEqual([it["token"] for it in items], [t])
		it = items[0]
		self.assertEqual(
			set(it.keys()),
			{"token", "tool", "preview", "summary", "conversation", "run_id", "expires_at"},
		)
		self.assertEqual(it["tool"], "create_doc")
		for internal in ("args", "exec_user", "args_hash"):
			self.assertNotIn(internal, it)

	def test_list_items_for_owner_filtered_by_conversation(self):
		self._mint(self._A, "conv-a1", "i-1")
		t2 = self._mint(self._A, "conv-a2", "i-2")
		items = pending_confirm.list_items_for_owner(self._A, conversation="conv-a2")
		self.assertEqual([it["token"] for it in items], [t2])

	def test_list_items_summary_failure_still_surfaces_card(self):
		"""A confirmable card must NEVER be dropped because the COSMETIC summary
		(_describe_call) throws on one odd record - that is the exact invisible-card
		bug this whole fix exists to close. The item still surfaces; only the summary
		degrades to ""."""
		t = self._mint(self._A, "conv-a1", "sum-throws")
		with patch("jarvis.api._describe_call", side_effect=RuntimeError("boom")):
			with patch.object(frappe, "log_error"):
				items = pending_confirm.list_items_for_owner(self._A)
		self.assertEqual([it["token"] for it in items], [t])
		self.assertEqual(items[0]["summary"], "")
		# The rest of the client-facing shape is intact.
		self.assertEqual(items[0]["tool"], "create_doc")
		self.assertIn("preview", items[0])

	def test_clear_for_conversation_removes_only_that_conversation(self):
		"""F6: clearing a conversation's tokens (on stop_run) deletes its own live
		tokens and leaves other conversations - and conversation-less tokens - alone."""
		t_x1 = self._mint(self._A, "conv-x", "cfc-1")
		t_x2 = self._mint(self._A, "conv-x", "cfc-2")
		t_y = self._mint(self._A, "conv-y", "cfc-3")
		t_cl = pending_confirm.mint(
			conversation="",
			owner=self._A,
			tool="delete_doc",
			args={"doctype": "ToDo", "name": "z"},
			run_id="",
		)
		n = pending_confirm.clear_for_conversation(self._A, "conv-x")
		self.assertEqual(n, 2)
		self.assertIsNone(pending_confirm.peek(t_x1))
		self.assertIsNone(pending_confirm.peek(t_x2))
		self.assertIsNotNone(pending_confirm.peek(t_y))  # other conversation kept
		self.assertIsNotNone(pending_confirm.peek(t_cl))  # conv-less kept

	def test_clear_for_conversation_run_id_scoping(self):
		"""F6: with a run_id, a token that carries a run_id is swept
		only when it matches; a managed token (run_id "") is always swept."""
		t_r1 = pending_confirm.mint(
			conversation="conv-z",
			owner=self._A,
			tool="delete_doc",
			args={"doctype": "ToDo", "name": "a"},
			run_id="r1",
		)
		t_r2 = pending_confirm.mint(
			conversation="conv-z",
			owner=self._A,
			tool="delete_doc",
			args={"doctype": "ToDo", "name": "b"},
			run_id="r2",
		)
		t_managed = pending_confirm.mint(
			conversation="conv-z",
			owner=self._A,
			tool="delete_doc",
			args={"doctype": "ToDo", "name": "c"},
			run_id="",
		)
		n = pending_confirm.clear_for_conversation(self._A, "conv-z", run_id="r1")
		self.assertEqual(n, 2)  # the r1 card + the run_id-less (managed) card
		self.assertIsNone(pending_confirm.peek(t_r1))
		self.assertIsNone(pending_confirm.peek(t_managed))
		self.assertIsNotNone(pending_confirm.peek(t_r2))  # sibling run kept


class TestPeek(FrappeTestCase):
	def test_unknown_token_is_none(self):
		self.assertIsNone(pending_confirm.peek("does-not-exist"))

	def test_live_token_returns_full_record(self):
		token = pending_confirm.mint(
			conversation=CONV,
			owner=OWNER,
			tool=TOOL,
			args=ARGS,
			run_id=RUN_ID,
		)
		record = pending_confirm.peek(token)
		self.assertIsNotNone(record)
		self.assertEqual(record["conversation"], CONV)
		self.assertEqual(record["owner"], OWNER)
		self.assertEqual(record["tool"], TOOL)
		self.assertEqual(record["args"], ARGS)
		self.assertEqual(record["run_id"], RUN_ID)
		self.assertEqual(record["args_hash"], pending_confirm.args_hash(TOOL, ARGS))

	def test_expired_token_is_none(self):
		"""Simulate expiry directly (waiting out the real 900s TTL is not
		practical in a test): mint, then drop the underlying cache key, then
		peek must report it gone same as a token that never existed."""
		token = pending_confirm.mint(
			conversation=CONV,
			owner=OWNER,
			tool=TOOL,
			args=ARGS,
			run_id=RUN_ID,
		)
		frappe.cache().delete_value(pending_confirm._PREFIX + token)
		self.assertIsNone(pending_confirm.peek(token))


class TestConsume(FrappeTestCase):
	def _mint(self):
		return pending_confirm.mint(
			conversation=CONV,
			owner=OWNER,
			tool=TOOL,
			args=ARGS,
			run_id=RUN_ID,
		)

	def test_happy_path_returns_record_and_is_single_use(self):
		token = self._mint()
		record = pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertIsNotNone(record)
		self.assertEqual(record["tool"], TOOL)
		self.assertEqual(record["args"], ARGS)
		# Second consume of the same token: already burned.
		self.assertIsNone(pending_confirm.consume(token, owner=OWNER, conversation=CONV))
		# And it is really gone, not just "consumed once but still peekable".
		self.assertIsNone(pending_confirm.peek(token))

	def test_wrong_owner_returns_none_and_does_not_burn_token(self):
		token = self._mint()
		self.assertIsNone(
			pending_confirm.consume(token, owner="someone-else@example.invalid", conversation=CONV)
		)
		# Token still lives - a later correct consume still works.
		record = pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertIsNotNone(record)

	def test_wrong_conversation_returns_none_and_does_not_burn_token(self):
		token = self._mint()
		self.assertIsNone(pending_confirm.consume(token, owner=OWNER, conversation="conv-other"))
		record = pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertIsNotNone(record)

	def test_empty_stored_conversation_is_confirmable_by_owner(self):
		"""F1: a token minted with an unresolvable conversation ("") carries no
		conversation binding, so an owner-matched consume must still succeed even
		when the caller passes its current (non-empty) conversation id. Regression
		for the session_key-miss case
		where the card was delivered but every Confirm click failed the
		conversation check and showed a misleading 'expired' toast."""
		token = pending_confirm.mint(conversation="", owner=OWNER, tool=TOOL, args=ARGS, run_id=RUN_ID)
		record = pending_confirm.consume(token, owner=OWNER, conversation="conv-current")
		self.assertIsNotNone(record)
		self.assertEqual(record["tool"], TOOL)

	def test_empty_stored_conversation_still_enforces_owner(self):
		"""F1: owner is still the real boundary for a conversation-less token."""
		token = pending_confirm.mint(conversation="", owner=OWNER, tool=TOOL, args=ARGS, run_id=RUN_ID)
		self.assertIsNone(
			pending_confirm.consume(token, owner="intruder@example.invalid", conversation="conv-current")
		)
		# Not burned - the real owner can still consume it.
		self.assertIsNotNone(pending_confirm.consume(token, owner=OWNER, conversation="conv-current"))

	def test_unknown_token_returns_none(self):
		self.assertIsNone(pending_confirm.consume("does-not-exist", owner=OWNER, conversation=CONV))

	def test_expired_token_returns_none(self):
		token = self._mint()
		frappe.cache().delete_value(pending_confirm._PREFIX + token)
		self.assertIsNone(pending_confirm.consume(token, owner=OWNER, conversation=CONV))

	def test_concurrent_consumes_exactly_one_wins(self):
		"""Two threads race to consume the same legitimate token. The atomic
		get-and-delete (_get_and_delete's MULTI/EXEC) is serialized server-side,
		so exactly one of the two concurrent consumes must get the record back;
		the other must get None - never both, never neither.

		Without a barrier, nothing forces the two threads to actually overlap:
		the OS could just run them back-to-back, in which case a naive
		non-atomic get-then-delete would also pass this test by accident. A
		threading.Barrier(2) is spliced in front of the real _get_and_delete call
		(the only place consume() mutates the store) so neither thread's burn can
		return until BOTH threads have completed their ownership-check read
		(the plain get_value earlier in consume()) and are standing right at
		the delete. That is the actual race window consume()'s docstring
		claims is safe - this test now forces it open on every run instead of
		hoping the scheduler happens to create it.
		"""
		token = self._mint()
		results = [None, None]
		barrier = threading.Barrier(2)
		real_get_and_delete = pending_confirm._get_and_delete

		def _synced_get_and_delete(*args, **kwargs):
			# Both threads land here only after their own ownership-check
			# read already matched, so this rendezvous pins both threads
			# past that read before either delete is allowed to fire.
			barrier.wait(timeout=5)
			return real_get_and_delete(*args, **kwargs)

		def _consume(i):
			results[i] = pending_confirm.consume(token, owner=OWNER, conversation=CONV)

		# threading.Thread does not inherit frappe.local (a contextvar-backed
		# thread local); copy_context().run propagates it into each thread so
		# frappe.cache() works there too.
		ctx1 = contextvars.copy_context()
		ctx2 = contextvars.copy_context()
		t1 = threading.Thread(target=ctx1.run, args=(_consume, 0))
		t2 = threading.Thread(target=ctx2.run, args=(_consume, 1))

		# _get_and_delete is a module-level function, so patching it on the
		# pending_confirm module affects both threads (they call the same one).
		with patch.object(pending_confirm, "_get_and_delete", side_effect=_synced_get_and_delete):
			t1.start()
			t2.start()
			t1.join(timeout=5)
			t2.join(timeout=5)

		self.assertFalse(t1.is_alive(), "thread 1 did not finish - barrier likely deadlocked")
		self.assertFalse(t2.is_alive(), "thread 2 did not finish - barrier likely deadlocked")

		winners = [r for r in results if r is not None]
		self.assertEqual(len(winners), 1)
		self.assertIsNone(pending_confirm.peek(token))

	def test_atomic_burn_precommit_error_is_typed_and_does_not_burn_token(self):
		"""A definite pre-commit storage failure remains retryable, but is not
		misreported to the endpoint as an expired/invalid token."""
		token = self._mint()
		cache = frappe.cache()
		pipe = cache.pipeline(transaction=True)
		with patch.object(
			pipe, "watch", side_effect=redis.exceptions.ConnectionError("failed before commit")
		):
			with patch.object(cache, "pipeline", return_value=pipe):
				with self.assertRaises(pending_confirm.PendingConfirmStorageError):
					pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertIsNotNone(pending_confirm.peek(token))
		record = pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertIsNotNone(record)
		self.assertEqual(record["tool"], TOOL)

	def test_atomic_burn_recovers_when_exec_commits_but_reply_is_lost(self):
		"""The request that durably claimed the token may execute after losing the
		EXEC reply; a second request must still lose. This is the ambiguity the old
		GET+DEL transaction incorrectly classified as 'token was not burned'."""
		token = self._mint()
		cache = frappe.cache()
		pipe = cache.pipeline(transaction=True)
		real_execute = pipe.execute

		def _commit_then_lose_reply(*args, **kwargs):
			real_execute(*args, **kwargs)
			raise redis.exceptions.TimeoutError("reply lost after EXEC")

		with patch.object(pipe, "execute", side_effect=_commit_then_lose_reply):
			with patch.object(cache, "pipeline", return_value=pipe):
				with patch.object(frappe, "logger"):
					record = pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertIsNotNone(record)
		self.assertEqual(record["tool"], TOOL)
		self.assertIsNone(pending_confirm.peek(token))
		self.assertIsNone(pending_confirm.consume(token, owner=OWNER, conversation=CONV))

	def test_atomic_burn_reports_unknown_when_committed_claim_cannot_be_read(self):
		"""If EXEC's reply and the recovery read are both lost, never dispatch from
		the pre-read snapshot and never call the token merely expired."""
		token = self._mint()
		cache = frappe.cache()
		pipe = cache.pipeline(transaction=True)
		real_execute = pipe.execute

		def _commit_then_lose_reply(*args, **kwargs):
			real_execute(*args, **kwargs)
			raise redis.exceptions.TimeoutError("reply lost after EXEC")

		with patch.object(pipe, "execute", side_effect=_commit_then_lose_reply):
			with patch.object(cache, "pipeline", return_value=pipe):
				with patch.object(
					cache, "hmget", side_effect=redis.exceptions.TimeoutError("recovery read lost")
				):
					with self.assertRaises(pending_confirm.PendingConfirmOutcomeUnknown):
						pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertIsNone(pending_confirm.peek(token))

	def test_consume_does_not_require_getdel_redis_62(self):
		"""GETDEL is a redis-server >= 6.2 command; an older bench (e.g. v6.0)
		rejects it with ResponseError. A Confirm click must still succeed there, so
		consume() must NOT depend on GETDEL. Simulate the old server by making the
		cache's getdel raise ResponseError; the consume must still burn the token and
		return the record via the portable MULTI/EXEC path (which never calls
		GETDEL)."""
		token = self._mint()
		with patch.object(
			frappe.cache(),
			"getdel",
			create=True,
			side_effect=redis.exceptions.ResponseError("ERR unknown command 'GETDEL'"),
		):
			record = pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertIsNotNone(record, "consume must not depend on the redis>=6.2 GETDEL command")
		self.assertEqual(record["tool"], TOOL)
		# Really burned (single-use) via the portable path, not left dangling.
		self.assertIsNone(pending_confirm.peek(token))

	def test_consume_read_timeout_is_typed_and_does_not_burn_token(self):
		"""The endpoint must distinguish a failed ownership read from expiry and
		keep the still-live confirmation available for retry."""
		token = self._mint()

		def _timeout(*args, **kwargs):
			raise redis.exceptions.TimeoutError("read timed out")

		with patch.object(frappe.cache(), "get", side_effect=_timeout):
			with self.assertRaises(pending_confirm.PendingConfirmStorageError):
				pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		# Not burned - a later consume against a healthy cache still succeeds.
		self.assertIsNotNone(pending_confirm.peek(token))
		record = pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertIsNotNone(record)
		self.assertEqual(record["tool"], TOOL)

	def test_confirm_flow_works_without_get_value_use_local_cache_kwarg(self):
		"""Frappe 15's RedisWrapper.get_value has NO ``use_local_cache`` kwarg;
		passing it raises TypeError on a real v15 bench. In mint() that TypeError is
		caught by the park try/except -> rollback -> None (no card renders - the exact
		v15 symptom this module exists to fix); in consume() the ownership read is
		unguarded -> an uncaught 500. Simulate the v15 signature and assert the whole
		mint+consume flow still works (i.e. the code never passes use_local_cache)."""
		real_get_value = frappe.cache().get_value

		def v15_get_value(key, *args, **kwargs):
			if "use_local_cache" in kwargs:
				raise TypeError("get_value() got an unexpected keyword argument 'use_local_cache'")
			return real_get_value(key, *args, **kwargs)

		with patch.object(frappe.cache(), "get_value", side_effect=v15_get_value):
			token = self._mint()
			self.assertIsNotNone(token, "mint must not pass use_local_cache (TypeErrors on Frappe v15)")
			record = pending_confirm.consume(token, owner=OWNER, conversation=CONV)
		self.assertIsNotNone(record, "consume must not pass use_local_cache (500s on Frappe v15)")
		self.assertEqual(record["tool"], TOOL)
