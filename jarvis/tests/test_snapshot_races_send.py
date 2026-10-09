"""Snapshot-isolation races on the ``Jarvis Conversation`` row (plan rows W4, W5, W6).

THE SHARED BUG. The conversation row is a shared write surface: a chat send, the
macro/continuation path, auto-titling, the model/thinking override endpoints, and
a File Box tool's canvas append can all touch the SAME row inside a request or job
that opened its snapshot earlier. Under MariaDB 11.6+'s snapshot isolation, the
LOSER of such a race gets ER_CHECKREAD 1020 (``frappe.QueryDeadlockError``), and
the server aborts the WHOLE transaction, including, for a send, the user's
just-inserted message.

W4 (``jarvis.chat.api.send_message`` / ``_enqueue_turn``): replaced a
whole-document ``conv_doc.save()`` placed AFTER ``msg_doc.insert()`` with one
row-locked, targeted ``frappe.db.set_value`` of exactly the fields the call
changed, placed BEFORE the insert (nothing else has written in the transaction
yet at that point) and wrapped in ``jarvis.chat.txn.replay_on_conflict``.

W5 (``jarvis.chat.title.maybe_autotitle``, ``set_conversation_model`` /
``set_conversation_thinking``): the autotitle job's gateway call is a 2-8s round
trip that can outlive its own snapshot, so the title write now runs
``txn.fresh_snapshot()`` first (job-only) and re-checks "still unnamed" INSIDE the
replayable unit. The two override endpoints wrap their existing single-field
``set_value`` in ``txn.replay_on_conflict`` (they already write ``update_modified=
False``; they were just never protected against losing the race itself).

W6 (``jarvis.api._maybe_attach_artifact``): the canvas read-append-write used to be
a plain, unlocked read-modify-write: two file tools finishing together could each
read the same canvas, and the second writer's blind ``set_value`` would silently
drop the first card (a lost update, not even an exception). It is now a
``SELECT canvas ... FOR UPDATE`` read-append-write, one replayable unit, with the
realtime publish moved after the winning commit.

Every test arms the real race (``race_harness``): a plain read opens the snapshot
the request/job would hold, a second connection commits the competing write, then
the real function runs. ``assert_fix_closes_race`` runs each scenario with the
defences in ``jarvis.chat.txn`` switched off (the race must bite, as
``frappe.QueryDeadlockError``) and on (the call must succeed, exactly once, with
the competing write intact).
"""

from collections.abc import Callable
from unittest.mock import patch

import frappe

import jarvis.api as jarvis_api
from jarvis.chat import api as chat_api
from jarvis.chat import title
from jarvis.chat.api import (
	CONV,
	MSG,
	archive_conversation,
	create_conversation,
	rename_conversation,
	send_message,
	set_conversation_model,
	set_conversation_thinking,
	set_star,
)
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests._transport_helpers import provision_legacy_site
from jarvis.tests.race_harness import (
	as_job,
	assert_fix_closes_race,
	open_read_view,
	other_connection,
	snapshot_isolation_off,
	snapshot_isolation_on,
)
from jarvis.tests.test_chat_api import TEST_USER, _ChatTestCase


def setUpModule():
	install_synthetic_runtime_profile()


def _race_once(real: Callable, *, after: Callable | None = None, before: Callable | None = None) -> Callable:
	"""Wrap ``real`` so its FIRST call runs the race around it: ``before`` ahead of
	the call, ``after`` right after it. Later calls pass straight through. Mirrors
	the identically-named helper in test_snapshot_races_pump.py (kept local here so
	this W4/W5/W6 module has no cross-module test coupling)."""
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


def _compete_autotitle(conv: str, new_title: str) -> None:
	"""Commit a title + modified change from a second connection, the way
	``jarvis.chat.title.maybe_autotitle`` (or a Desk edit, or an override
	endpoint) would land on the SAME row a send is about to lock. Bumping
	``modified`` explicitly stands in for any writer, autotitle included,
	that used a plain ``db_set``/``set_value`` without ``update_modified=False``."""
	with other_connection() as other:
		other.sql(f"UPDATE `tab{CONV}` SET title=%s, modified=NOW(6) WHERE name=%s", (new_title, conv))
		other.commit()


def _call_or_raced(fn, *args, **kwargs) -> dict:
	"""Run an endpoint call for a 10.x-mode (snapshot isolation off) scenario.

	There the lost race surfaces as frappe.TimestampMismatchError, not the engine's
	QueryDeadlockError, and assert_fix_closes_race only recognises QueryDeadlockError
	as proof the race was armed. Catching it here and returning a rejectable sentinel
	instead lets the SAME check() prove both that the fix-off run is wrong (the
	sentinel has no "ok") and the fix-on run is right (also=(TimestampMismatchError,)
	on the replay_on_conflict call replays it, so no exception reaches here)."""
	try:
		return fn(*args, **kwargs)
	except frappe.TimestampMismatchError:
		return {"ok": False, "reason": "timestamp_mismatch"}


def _compete_touch(conv: str) -> None:
	"""Commit an unrelated field bump from a second connection (e.g. a send's own
	``last_active_at`` write, or an override endpoint's ``model_override``),
	landing on the conversation row without touching ``title``, the way it would
	during the autotitle job's 2-8s gateway round trip."""
	with other_connection() as other:
		other.sql(f"UPDATE `tab{CONV}` SET last_active_at=NOW(6), modified=NOW(6) WHERE name=%s", (conv,))
		other.commit()


class TestSendMessageConversationRace(_ChatTestCase):
	"""W4, site 1: ``jarvis.chat.api.send_message``."""

	def setUp(self):
		super().setUp()
		# CDX-10: force the legacy dispatch path so a bare ``frappe.enqueue`` patch
		# is enough (mirrors ``TestSendMessage`` in test_chat_api.py); the admission
		# machine's own writes all happen AFTER this function's commit and are not
		# part of what this race test exercises.
		provision_legacy_site(self)

	def test_a_competing_autotitle_between_load_and_write_replays_and_lands(self):
		def scenario():
			conv = create_conversation()
			with snapshot_isolation_on():
				# The request's transaction fixes its snapshot at its first read,
				# same as _get_owned_conversation's plain frappe.get_doc.
				open_read_view()
				_compete_autotitle(conv, "Renamed by a concurrent auto-title")
				with (
					patch.object(chat_api, "_ensure_session_key", return_value="agent:fake"),
					patch("frappe.enqueue"),
					# The send gate asks admin whether the subscription is suspended, from a
					# short cache. When that cache is cold on a site with an admin URL, the
					# (stubbed) call fails and logs Error Log rows in this transaction, which
					# rightly makes the unit unsafe to replay. Not what this test is about.
					patch("jarvis.chat.policy._subscription_suspended", return_value=False),
				):
					# thinking_override proves a field THIS send actually changed landed
					# (last_active_at alone would be vacuous: before_insert already set
					# it at creation).
					result = send_message(conv, "hello again", thinking_override="low")
			return conv, result

		def check(res):
			conv, result = res
			self.assertTrue(result.get("ok"), result)
			row = frappe.db.get_value(
				CONV, conv, ["title", "last_active_at", "thinking_override"], as_dict=True
			)
			self.assertEqual(
				row.title,
				"Renamed by a concurrent auto-title",
				"the competing writer's own field must survive: a targeted write, "
				"never a whole-document save that would revert it",
			)
			self.assertEqual(row.thinking_override, "low", "the field THIS send changed must land")
			self.assertIsNotNone(row.last_active_at)
			self.assertEqual(
				frappe.db.count(MSG, {"conversation": conv, "role": "user"}),
				1,
				"the user's own message must exist exactly once, never dropped by an aborted transaction",
			)
			content = frappe.db.get_value(MSG, {"conversation": conv, "role": "user"}, "content")
			self.assertEqual(content, "hello again")

		assert_fix_closes_race(self, scenario, check)


class TestEnqueueTurnConversationRace(_ChatTestCase):
	"""W4, site 1: ``jarvis.chat.api._enqueue_turn`` (the macro/continuation twin)."""

	def setUp(self):
		super().setUp()
		provision_legacy_site(self)

	def test_a_competing_autotitle_between_load_and_write_replays_and_lands(self):
		def scenario():
			conv = create_conversation()
			with snapshot_isolation_on():
				open_read_view()
				_compete_autotitle(conv, "Renamed mid-continuation")
				with patch.object(chat_api, "_dispatch_turn", side_effect=lambda *a, **k: None):
					out = chat_api._enqueue_turn(
						conv, "macro step", hidden=True, origin="macro", thinking_override="low"
					)
			return conv, out

		def check(res):
			conv, out = res
			self.assertTrue(frappe.db.exists(MSG, out["message_id"]))
			row = frappe.db.get_value(
				CONV, conv, ["title", "last_active_at", "thinking_override"], as_dict=True
			)
			self.assertEqual(
				row.title,
				"Renamed mid-continuation",
				"the competing writer's own field must survive the targeted write",
			)
			self.assertEqual(row.thinking_override, "low", "the field THIS call changed must land")
			self.assertIsNotNone(row.last_active_at)
			self.assertEqual(frappe.db.count(MSG, {"conversation": conv, "role": "user"}), 1)

		assert_fix_closes_race(self, scenario, check)


class TestAutotitleRace(_ChatTestCase):
	"""W5, site 1: ``jarvis.chat.title.maybe_autotitle``.

	The autotitle job's gateway call is patched to be instant, but it still models
	the real 2-8s exposure: as its side effect, a second connection commits an
	unrelated field change on the SAME conversation row (a send's ``last_active_at``,
	an override endpoint) BEFORE the title write runs. ``title = frappe.db.get_value``
	at the top of ``maybe_autotitle`` is the job's own first read: its snapshot is
	what goes stale while the (here, instant) "gateway call" runs."""

	def test_a_competing_write_during_the_gateway_call_still_lands_the_title(self):
		def scenario():
			conv = create_conversation()
			with snapshot_isolation_on(), as_job():

				def gen(gateway_url, source_text, *, model, provider):
					# Stands in for the 2-8s gateway round trip: some other writer
					# (a send, an override endpoint) touches this row while we wait.
					_compete_touch(conv)
					return "Password Reset Help"

				with (
					patch.object(title, "_opening_user_messages", return_value="reset my password"),
					patch.object(title, "_generate_via_gateway", side_effect=gen),
					patch.object(title, "publish_to_user"),
				):
					title.maybe_autotitle(conv, TEST_USER, gateway_url="ws://x", model="m", provider="p")
			return conv

		def check(conv):
			row = frappe.db.get_value(CONV, conv, ["title", "modified"], as_dict=True)
			self.assertEqual(row.title, "Password Reset Help", "the title must land despite the race")

		assert_fix_closes_race(self, scenario, check)


class TestOverrideEndpointRace(_ChatTestCase):
	"""W5, site 2: ``jarvis.chat.api.set_conversation_thinking`` / ``set_conversation_model``
	(outside a job: a web request never calls ``txn.fresh_snapshot()``, only
	``replay_on_conflict``)."""

	def test_set_conversation_thinking_replays_a_competing_write(self):
		def scenario():
			conv = create_conversation()
			with snapshot_isolation_on():
				open_read_view()
				_compete_autotitle(conv, "Renamed mid-flight")
				result = set_conversation_thinking(conv, "low")
			return conv, result

		def check(res):
			conv, result = res
			self.assertTrue(result.get("ok"), result)
			row = frappe.db.get_value(CONV, conv, ["title", "thinking_override"], as_dict=True)
			self.assertEqual(
				row.title,
				"Renamed mid-flight",
				"the competing writer's own field survives: a single targeted set_value, "
				"never a whole-document save",
			)
			self.assertEqual(row.thinking_override, "low")

		assert_fix_closes_race(self, scenario, check)

	def test_set_conversation_model_replays_a_competing_write(self):
		def scenario():
			conv = create_conversation()
			with snapshot_isolation_on():
				open_read_view()
				_compete_autotitle(conv, "Renamed while pinning a model")
				# Bypasses the real catalog lookup (irrelevant to the race being tested):
				# only the write path after validation is under test here.
				with patch.object(chat_api, "_allowed_pin_models", return_value={"gpt-5.6-terra"}):
					result = set_conversation_model(conv, "gpt-5.6-terra")
			return conv, result

		def check(res):
			conv, result = res
			self.assertTrue(result.get("ok"), result)
			row = frappe.db.get_value(CONV, conv, ["title", "model_override"], as_dict=True)
			self.assertEqual(
				row.title,
				"Renamed while pinning a model",
				"the competing writer's own field survives: a single targeted set_value, "
				"never a whole-document save",
			)
			self.assertEqual(row.model_override, "gpt-5.6-terra")

		assert_fix_closes_race(self, scenario, check)


class TestConversationEditRace(_ChatTestCase):
	"""Scope addition: rename_conversation, archive_conversation, set_star.

	Each endpoint's load (``_get_owned_conversation``) + modify + ``doc.save()`` is
	now one ``txn.replay_on_conflict`` unit, and the unit RELOADS the doc on every
	attempt (see the fix): after a lost race the transaction is gone, so a stale
	in-memory doc would both trip Document.save's own check_if_latest again and,
	worse, overwrite whatever the competing writer just landed with the value that
	field held at the ORIGINAL load. The reload picks up the competing writer's
	value first, then applies THIS request's edit on top, so both survive.
	``doc.save()`` is kept (not replaced with a targeted set_value, unlike W4's
	send path) so these edits stay in Jarvis Conversation's Version history."""

	def test_rename_conversation_replays_a_competing_write(self):
		def scenario():
			conv = create_conversation()
			original_last_active = frappe.db.get_value(CONV, conv, "last_active_at")
			with snapshot_isolation_on():
				# Document.save's own check_if_latest re-reads the row FOR UPDATE, so
				# a plain read here is enough to fix the stale snapshot it will race.
				open_read_view()
				# An unrelated field, not title, so it never collides with THIS
				# request's own edit.
				_compete_touch(conv)
				result = rename_conversation(conv, "Renamed by the user")
			return conv, result, original_last_active

		def check(res):
			conv, result, original_last_active = res
			self.assertTrue(result.get("ok"), result)
			row = frappe.db.get_value(CONV, conv, ["title", "last_active_at"], as_dict=True)
			self.assertEqual(row.title, "Renamed by the user", "the user's own edit must land")
			self.assertNotEqual(
				row.last_active_at,
				original_last_active,
				"the competing writer's touch must survive: the reload must pick it up, "
				"not resave the stale value the request first loaded",
			)

		assert_fix_closes_race(self, scenario, check)

	def test_rename_conversation_replays_a_competing_write_on_10x(self):
		"""MariaDB 10.x mode (snapshot_isolation_off): the race surfaces as
		Document.save's own check_if_latest, not the engine's 1020. See
		_call_or_raced for how that is turned into something assert_fix_closes_race
		can still judge."""

		def scenario():
			conv = create_conversation()
			original_last_active = frappe.db.get_value(CONV, conv, "last_active_at")
			with snapshot_isolation_off():
				open_read_view()
				_compete_touch(conv)
				result = _call_or_raced(rename_conversation, conv, "Renamed by the user")
			return conv, result, original_last_active

		def check(res):
			conv, result, original_last_active = res
			self.assertTrue(result.get("ok"), result)
			row = frappe.db.get_value(CONV, conv, ["title", "last_active_at"], as_dict=True)
			self.assertEqual(row.title, "Renamed by the user", "the user's own edit must land")
			self.assertNotEqual(
				row.last_active_at,
				original_last_active,
				"the competing writer's touch must survive the reload + resave",
			)

		assert_fix_closes_race(self, scenario, check)

	def test_archive_conversation_replays_a_competing_write(self):
		def scenario():
			conv = create_conversation()
			with snapshot_isolation_on():
				open_read_view()
				_compete_autotitle(conv, "Renamed while archiving")
				result = archive_conversation(conv)
			return conv, result

		def check(res):
			conv, result = res
			self.assertTrue(result.get("ok"), result)
			row = frappe.db.get_value(CONV, conv, ["status", "title"], as_dict=True)
			self.assertEqual(row.status, "Archived", "the user's own edit must land")
			self.assertEqual(
				row.title,
				"Renamed while archiving",
				"the competing writer's own title must survive the reload + resave",
			)

		assert_fix_closes_race(self, scenario, check)

	def test_archive_conversation_replays_a_competing_write_on_10x(self):
		"""MariaDB 10.x mode: see test_rename_conversation_replays_a_competing_write_on_10x."""

		def scenario():
			conv = create_conversation()
			with snapshot_isolation_off():
				open_read_view()
				_compete_autotitle(conv, "Renamed while archiving")
				result = _call_or_raced(archive_conversation, conv)
			return conv, result

		def check(res):
			conv, result = res
			self.assertTrue(result.get("ok"), result)
			row = frappe.db.get_value(CONV, conv, ["status", "title"], as_dict=True)
			self.assertEqual(row.status, "Archived", "the user's own edit must land")
			self.assertEqual(
				row.title,
				"Renamed while archiving",
				"the competing writer's own title must survive the reload + resave",
			)

		assert_fix_closes_race(self, scenario, check)

	def test_set_star_replays_a_competing_write(self):
		def scenario():
			conv = create_conversation()
			with snapshot_isolation_on():
				open_read_view()
				_compete_autotitle(conv, "Renamed while starring")
				result = set_star(conv, "1")
			return conv, result

		def check(res):
			conv, result = res
			self.assertTrue(result.get("ok"), result)
			row = frappe.db.get_value(CONV, conv, ["starred", "title"], as_dict=True)
			self.assertEqual(int(row.starred), 1, "the user's own edit must land")
			self.assertEqual(
				row.title,
				"Renamed while starring",
				"the competing writer's own title must survive the reload + resave",
			)

		assert_fix_closes_race(self, scenario, check)

	def test_set_star_replays_a_competing_write_on_10x(self):
		"""MariaDB 10.x mode: see test_rename_conversation_replays_a_competing_write_on_10x."""

		def scenario():
			conv = create_conversation()
			with snapshot_isolation_off():
				open_read_view()
				_compete_autotitle(conv, "Renamed while starring")
				result = _call_or_raced(set_star, conv, "1")
			return conv, result

		def check(res):
			conv, result = res
			self.assertTrue(result.get("ok"), result)
			row = frappe.db.get_value(CONV, conv, ["starred", "title"], as_dict=True)
			self.assertEqual(int(row.starred), 1, "the user's own edit must land")
			self.assertEqual(
				row.title,
				"Renamed while starring",
				"the competing writer's own title must survive the reload + resave",
			)

		assert_fix_closes_race(self, scenario, check)

	def test_replay_exhaustion_is_a_friendly_error_not_a_raw_500(self):
		"""Round-2 review: a genuine, PERSISTENT write conflict (a real
		frappe.QueryDeadlockError on every attempt, not just a lost race a retry
		then wins) exhausts replay_on_conflict. _save_conv_or_friendly_error must
		catch that at the endpoint boundary and turn it into a friendly, catchable
		error, the same way admission.py's W1/W2 endpoints already do, instead of
		letting the raw QueryDeadlockError through as an unreadable 500.

		Forces exhaustion by patching _get_owned_conversation (each _save unit's
		own first read) to raise the real exception class on every call, rather
		than arming a genuine two-connection race: the point here is the
		EXHAUSTED path, already distinct from the replay-wins path covered above.
		Neither the frontend nor the pwa checks a resolved {"ok": False} shape
		for these three calls (see _save_conv_or_friendly_error's docstring), so
		the friendly outcome is a controlled, catchable exception, not a dict."""
		cases = [
			(rename_conversation, ("Renamed",), "rename"),
			(archive_conversation, (), "delete"),
			(set_star, ("1",), "update"),
		]
		for fn, extra_args, expected_word in cases:
			with self.subTest(endpoint=fn.__name__):
				conv = create_conversation()
				with patch.object(
					chat_api, "_get_owned_conversation", side_effect=frappe.QueryDeadlockError("stuck")
				):
					with self.assertRaises(frappe.ValidationError) as ctx:
						fn(conv, *extra_args)
				self.assertNotIsInstance(ctx.exception, frappe.QueryDeadlockError)
				self.assertIn(expected_word, str(ctx.exception).lower())


def _mk_streaming_assistant_msg(conv: str) -> str:
	"""A bare in-flight assistant row for W6's canvas race: no canvas yet, the way
	a fresh reply looks before its first tool result lands."""
	doc = frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conv,
			"seq": 1,
			"role": "assistant",
			"content": "",
			"streaming": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _compete_attach_card(msg_name: str, file_url: str, name: str) -> None:
	"""Commit a canvas with ONE card from a second connection: a competing file
	tool's own ``_maybe_attach_artifact`` (or a duplicate callback for the SAME
	tool result) landing on the SAME reply row."""
	card = {"name": name, "title": name, "type": "sheet", "file_url": file_url}
	with other_connection() as other:
		other.sql(f"UPDATE `tab{MSG}` SET canvas=%s WHERE name=%s", (frappe.as_json([card]), msg_name))
		other.commit()


class TestArtifactAttachRace(_ChatTestCase):
	"""W6: ``jarvis.api._maybe_attach_artifact``.

	``_maybe_attach_artifact`` is called right after ``persist_tool_receipt``'s own
	commit (api.py:704, with only a network-only realtime publish in between), so
	``_write_canvas``'s OWN transaction's snapshot-fixing read is its first
	statement, ``frappe.get_all`` (finding the target assistant message), NOT
	anything the test calls before ``_maybe_attach_artifact`` starts. Both tests
	below arm the race by hooking ``frappe.get_all`` with ``_race_once(after=...)``:
	the competing connection commits right after that read fixes our snapshot, and
	right before our own ``FOR UPDATE``."""

	def test_two_interleaved_attaches_keep_both_cards(self):
		def scenario():
			conv = create_conversation()
			msg = _mk_streaming_assistant_msg(conv)
			result_a = {"ok": True, "data": {"file_url": "/private/files/a.xlsx", "filename": "a.xlsx"}}

			def race():
				# A second file tool's OWN attach (a different card) lands on the
				# same reply row while our snapshot (fixed by the get_all below) is
				# already open.
				_compete_attach_card(msg, "/private/files/b.xlsx", "b.xlsx")

			fenced = _race_once(frappe.get_all, after=race)
			with (
				snapshot_isolation_on(),
				patch("frappe.get_all", fenced),
				patch("frappe.publish_realtime") as pub,
			):
				jarvis_api._maybe_attach_artifact(conv, TEST_USER, result_a)
			return conv, msg, pub.call_count

		def check(res):
			_conv, msg, publishes = res
			items = frappe.parse_json(frappe.db.get_value(MSG, msg, "canvas") or "[]")
			urls = {i.get("file_url") for i in items}
			self.assertEqual(
				urls,
				{"/private/files/a.xlsx", "/private/files/b.xlsx"},
				"neither file tool's card may be lost to the other's write",
			)
			self.assertEqual(publishes, 1, "exactly one canvas event, published after the winning commit")

		assert_fix_closes_race(self, scenario, check)

	def test_a_duplicate_callback_racing_the_first_lands_the_card_exactly_once(self):
		"""Engine-armed: the competing write is a duplicate landing of the SAME
		card (a retried gateway callback / two workers racing one tool_call_id),
		not a different file, the dedupe check inside the locked unit must still
		see it and refuse a second copy, on the replay that follows the lost race."""

		def scenario():
			conv = create_conversation()
			msg = _mk_streaming_assistant_msg(conv)
			result = {"ok": True, "data": {"file_url": "/private/files/dup.xlsx", "filename": "dup.xlsx"}}

			def race():
				_compete_attach_card(msg, "/private/files/dup.xlsx", "dup.xlsx")

			fenced = _race_once(frappe.get_all, after=race)
			with (
				snapshot_isolation_on(),
				patch("frappe.get_all", fenced),
				patch("frappe.publish_realtime") as pub,
			):
				jarvis_api._maybe_attach_artifact(conv, TEST_USER, result)
			return conv, msg, pub.call_count

		def check(res):
			_conv, msg, publishes = res
			items = frappe.parse_json(frappe.db.get_value(MSG, msg, "canvas") or "[]")
			urls = [i.get("file_url") for i in items]
			self.assertEqual(urls, ["/private/files/dup.xlsx"], "exactly one card, never a duplicate")
			self.assertEqual(publishes, 0, "a deduped attach (already on the row) must not publish again")

		assert_fix_closes_race(self, scenario, check)
