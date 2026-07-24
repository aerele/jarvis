"""Tests for jarvis.chat.api - whitelisted endpoints.

These tests run as a **dedicated test user** (``TEST_USER``) so they never
touch Administrator's data. Running the suite against a dev site that has
real chat history previously wiped that history; the fixture user keeps
test cleanups scoped to disposable rows.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import admission, pump
from jarvis.chat.api import (
	_AGENT_TURN_WORKER_TIMEOUT,
	archive_conversation,
	create_conversation,
	create_or_focus_empty,
	get_canvas,
	get_conversation,
	list_conversations,
	rename_conversation,
	retry_message,
	set_conversation_model,
	set_conversation_thinking,
	set_star,
)

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
PUMP = "Jarvis Relay Pump"
TEST_USER = "jarvis-test@example.com"

# CDX-10: these classes assert the LEGACY dispatch path — provision an honestly-legacy site
# (transport_mode ROW = 'legacy' + explicit conf jarvis_pump_enabled=0), restored after.
from jarvis.tests._transport_helpers import provision_legacy_site as _provision_legacy_site


def _ensure_test_user(user: str = TEST_USER) -> None:
	"""Create the fixture user if missing; idempotent."""
	if frappe.db.exists("User", user):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": user,
			"first_name": "Jarvis",
			"last_name": "Test",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	# Grant System Manager so the test user can dispatch every tool path.
	doc.add_roles("System Manager")
	frappe.db.commit()


def _cleanup_user_conversations(user: str = TEST_USER) -> None:
	"""Delete all conversations owned by `user` (and their messages).

	Defaults to the test fixture user - callers should NOT pass
	``Administrator`` here; doing so wipes real chat history on the dev site.
	"""
	names = frappe.get_all(CONV, filters={"owner": user}, pluck="name")
	for name in names:
		for child in frappe.get_all(MSG, filters={"conversation": name}, pluck="name"):
			frappe.delete_doc(MSG, child, ignore_permissions=True, force=True)
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	frappe.db.commit()


class _ChatTestCase(FrappeTestCase):
	"""Base class that switches to the fixture user for the test lifetime
	and restores the original session user on teardown.
	"""

	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)


class TestCreateConversation(_ChatTestCase):
	def test_creates_a_row_owned_by_current_user(self):
		name = create_conversation()
		doc = frappe.get_doc(CONV, name)
		self.assertEqual(doc.owner, TEST_USER)
		self.assertEqual(doc.status, "Active")
		self.assertIsNotNone(doc.last_active_at)

	def test_title_defaults_to_new_chat(self):
		name = create_conversation()
		doc = frappe.get_doc(CONV, name)
		self.assertEqual(doc.title, "New chat")


class TestListConversations(_ChatTestCase):
	def test_returns_empty_when_no_conversations(self):
		result = list_conversations()
		self.assertEqual(result, [])

	def _add_message(self, conversation, seq=1):
		frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conversation,
				"seq": seq,
				"role": "user",
				"content": "hi",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def test_returns_active_conversations_for_current_user_only(self):
		a = create_conversation()
		b = create_conversation()
		# Only conversations with at least one message appear in the list.
		self._add_message(a)
		self._add_message(b)
		result = list_conversations()
		names = {c["name"] for c in result}
		self.assertIn(a, names)
		self.assertIn(b, names)

	def test_excludes_archived_by_default(self):
		a = create_conversation()
		self._add_message(a)  # non-empty, so exclusion is due to archiving
		archive_conversation(a)
		result = list_conversations()
		names = {c["name"] for c in result}
		self.assertNotIn(a, names)

	def test_hides_empty_conversations(self):
		# A "New Chat" opened and abandoned (no message) never clutters the list;
		# it surfaces once it has a message.
		empty = create_conversation()
		filled = create_conversation()
		self._add_message(filled)
		names = {c["name"] for c in list_conversations()}
		self.assertNotIn(empty, names)
		self.assertIn(filled, names)

	def test_includes_message_count(self):
		a = create_conversation()
		b = create_conversation()
		# Add one message to `a` only; `b` stays empty (and thus hidden).
		self._add_message(a)

		result = {c["name"]: c for c in list_conversations()}
		self.assertEqual(result[a]["message_count"], 1)
		self.assertNotIn(b, result)


class TestCreateOrFocusEmpty(_ChatTestCase):
	def test_creates_when_no_conversations_exist(self):
		name = create_or_focus_empty()
		self.assertTrue(frappe.db.exists(CONV, name))
		self.assertEqual(frappe.db.get_value(CONV, name, "owner"), TEST_USER)

	def test_returns_existing_empty_instead_of_creating(self):
		existing = create_conversation()
		returned = create_or_focus_empty()
		self.assertEqual(returned, existing)
		# Reuse, not a duplicate: still exactly one conversation row for the user
		# (it is empty, so it does not show in list_conversations).
		self.assertEqual(frappe.db.count(CONV, {"owner": TEST_USER}), 1)

	def test_creates_new_when_only_non_empty_exist(self):
		filled = create_conversation()
		frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": filled,
				"seq": 1,
				"role": "user",
				"content": "hi",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		returned = create_or_focus_empty()
		self.assertNotEqual(returned, filled)
		# A second (empty) conversation row now exists in the DB...
		self.assertTrue(frappe.db.exists(CONV, returned))
		self.assertEqual(frappe.db.count(CONV, {"owner": TEST_USER}), 2)
		# ...but the list shows only the non-empty one (the new empty is hidden).
		all_names = {c["name"] for c in list_conversations()}
		self.assertIn(filled, all_names)
		self.assertNotIn(returned, all_names)

	def test_prefers_most_recent_empty(self):
		older = create_conversation()
		# Force older to have an earlier last_active_at
		frappe.db.set_value(CONV, older, "last_active_at", "2020-01-01 00:00:00")
		newer = create_conversation()
		returned = create_or_focus_empty()
		self.assertEqual(returned, newer)

	def test_reuse_bumps_last_active_at(self):
		# Focusing an old empty resets its idle clock so the empty-reaper
		# (session_lifecycle) can't delete it out from under a freshly-opened tab.
		old = create_conversation()
		frappe.db.set_value(CONV, old, "last_active_at", "2020-01-01 00:00:00")
		returned = create_or_focus_empty()
		self.assertEqual(returned, old)
		bumped = frappe.utils.get_datetime(frappe.db.get_value(CONV, old, "last_active_at"))
		self.assertGreater(bumped, frappe.utils.get_datetime("2020-06-01 00:00:00"))

	def test_skips_filebox_drop_when_reusing_empty(self):
		# A failed File-Box drop is a 0-message file_box conversation. Reusing it as
		# a "New Chat" would silently inherit the file_box confirm-card bypass, so
		# it must be skipped in favour of a genuinely blank chat.
		fb = create_conversation()
		frappe.db.set_value(CONV, fb, "file_box", 1)
		frappe.db.commit()
		returned = create_or_focus_empty()
		self.assertNotEqual(returned, fb)
		self.assertEqual(frappe.db.get_value(CONV, returned, "file_box"), 0)

	def test_skips_empty_with_attached_file_when_reusing(self):
		# Belt-and-suspenders: any empty carrying an attached File is skipped so a
		# reuse never adopts a stray upload (delete-cascade / bypass concerns).
		withfile = create_conversation()
		f = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "coe-attach.txt",
				"attached_to_doctype": CONV,
				"attached_to_name": withfile,
				"content": "x",
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("File", f.name, force=True, ignore_permissions=True))
		frappe.db.commit()
		returned = create_or_focus_empty()
		self.assertNotEqual(returned, withfile)


class TestGetConversation(_ChatTestCase):
	def test_returns_conversation_with_empty_messages(self):
		name = create_conversation()
		result = get_conversation(name)
		self.assertEqual(result["conversation"]["name"], name)
		self.assertEqual(result["messages"], [])

	def test_returns_messages_in_seq_order(self):
		name = create_conversation()
		# Manually insert messages out of seq order
		for seq, role, content in [(2, "assistant", "B"), (1, "user", "A")]:
			doc = frappe.get_doc(
				{
					"doctype": MSG,
					"conversation": name,
					"seq": seq,
					"role": role,
					"content": content,
				}
			)
			doc.insert(ignore_permissions=True)
		frappe.db.commit()
		result = get_conversation(name)
		self.assertEqual([m["seq"] for m in result["messages"]], [1, 2])

	def test_raises_for_unknown_conversation(self):
		with self.assertRaises(frappe.DoesNotExistError):
			get_conversation("JCONV-99999")


class TestArchiveConversation(_ChatTestCase):
	def test_sets_status_to_archived(self):
		name = create_conversation()
		archive_conversation(name)
		doc = frappe.get_doc(CONV, name)
		self.assertEqual(doc.status, "Archived")


from jarvis.chat.api import send_message


class TestSendMessage(_ChatTestCase):
	def setUp(self):
		super().setUp()
		self.conv = create_conversation()
		# CDX-10: these tests assert the LEGACY worker enqueue — provision a legacy site.
		_provision_legacy_site(self)

	def test_rejects_when_policy_says_no(self):
		with patch(
			"jarvis.chat.api.validate_can_send",
			return_value=(False, "no credits"),
		):
			result = send_message(self.conv, "hi")
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason"], "no credits")

	def test_rejects_empty_message(self):
		result = send_message(self.conv, "   ")
		self.assertFalse(result["ok"])
		self.assertIn("empty", result["reason"].lower())

	def test_human_send_to_missing_conversation_falls_back(self):
		# A stale/reaped conversation id from a human send lands in a fresh chat
		# instead of dead-ending on DoesNotExistError.
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"), patch("frappe.enqueue"):
			result = send_message("JCONV-99999", "hi")
		self.assertTrue(result["ok"])
		self.assertNotEqual(result["conversation_id"], "JCONV-99999")
		self.assertTrue(frappe.db.exists(CONV, result["conversation_id"]))
		# The message landed in the fallback conversation.
		self.assertTrue(frappe.db.exists(MSG, {"conversation": result["conversation_id"], "role": "user"}))

	def test_delegated_send_to_missing_conversation_raises(self):
		# Delegated/system flows pass a real conversation; a genuine not-found is
		# a real error, never silently retargeted.
		frappe.flags.jarvis_delegated_send = True
		try:
			with self.assertRaises(frappe.DoesNotExistError):
				send_message("JCONV-99999", "hi")
		finally:
			frappe.flags.jarvis_delegated_send = False

	def test_persists_user_message_and_enqueues_worker(self):
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"):
			with patch("frappe.enqueue") as enqueue:
				result = send_message(self.conv, "list 3 customers")
		self.assertTrue(result["ok"])
		# A user message row exists
		rows = frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "role": "user"},
			fields=["name", "seq", "content"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["seq"], 1)
		self.assertEqual(rows[0]["content"], "list 3 customers")
		# The worker was enqueued with the right kwargs
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(kwargs["method"], "jarvis.chat.worker.run_agent_turn")
		self.assertEqual(kwargs["conversation_id"], self.conv)
		self.assertEqual(kwargs["message_id"], result["message_id"])

	def test_enqueues_with_300s_timeout(self):
		"""Sprint-2: worst-case worker budget = pair (<=90s) + WS connect
		(10s) + turn (180s) = 280s. The previous 200s timeout caused
		RQ SIGKILL bypassing the worker's try/finally, stranding the
		placeholder row with streaming=1 forever. Pin the new 300s
		budget so a future "cleanup" cannot regress it back."""
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"):
			with patch("frappe.enqueue") as enqueue:
				send_message(self.conv, "hi")
		_, kwargs = enqueue.call_args
		self.assertEqual(
			kwargs["timeout"],
			_AGENT_TURN_WORKER_TIMEOUT,
			"RQ worker budget must cover pair+connect+turn worst case",
		)

	def test_seq_increments_across_calls(self):
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"):
			with patch("frappe.enqueue"):
				send_message(self.conv, "a")
				send_message(self.conv, "b")
		seqs = frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "role": "user"},
			fields=["seq"],
			order_by="seq asc",
		)
		self.assertEqual([r["seq"] for r in seqs], [1, 2])

	def test_first_message_does_not_title_from_raw_text(self):
		"""send_message must NOT set the conversation title from the raw first
		message. The title stays "New chat"; the worker generates a concise
		LLM-summarised title after the first substantive turn and pushes it via
		a conversation:renamed event, so the sidebar never flashes the raw prompt.
		"""
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"):
			with patch("frappe.enqueue"):
				send_message(self.conv, "How many invoices last quarter?")
		doc = frappe.get_doc(CONV, self.conv)
		self.assertEqual(doc.title, "New chat")

	def test_bumps_last_active_at(self):
		before = frappe.utils.get_datetime(frappe.get_value(CONV, self.conv, "last_active_at"))
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"):
			with patch("frappe.enqueue"):
				send_message(self.conv, "hi")
		after = frappe.utils.get_datetime(frappe.get_value(CONV, self.conv, "last_active_at"))
		self.assertGreaterEqual(after, before)


class TestRetryMessage(_ChatTestCase):
	"""retry_message re-runs the worker for the user turn that preceded an
	errored assistant message.
	"""

	def _make_turn(
		self, conv: str, user_text: str = "list 3 customers", with_error: bool = False
	) -> tuple[str, str]:
		"""Seed a conversation with a user message + assistant message at the
		next two seq values. Returns (user_message_name, assistant_message_name).
		"""
		base_seq = frappe.db.sql(
			"SELECT COALESCE(MAX(seq), 0) FROM `tabJarvis Chat Message` WHERE conversation = %s",
			(conv,),
		)[0][0]
		user_doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": base_seq + 1,
				"role": "user",
				"content": user_text,
			}
		)
		user_doc.insert()
		asst_payload = {
			"doctype": MSG,
			"conversation": conv,
			"seq": base_seq + 2,
			"role": "assistant",
			"content": "",
		}
		if with_error:
			asst_payload["error"] = "rate limit"
		asst_doc = frappe.get_doc(asst_payload)
		asst_doc.insert()
		frappe.db.commit()
		return user_doc.name, asst_doc.name

	def setUp(self):
		super().setUp()
		self.conv = create_conversation()
		# retry_message now shares send_message's entitlement gate. Pin it to
		# "entitled" so these tests don't depend on the live control plane's
		# current subscription state; the suspended case is tested explicitly.
		gate = patch("jarvis.account._admin_chat_gate", return_value={"ready": True, "reason": None})
		gate.start()
		self.addCleanup(gate.stop)
		# CDX-10: these tests assert the LEGACY worker enqueue / shared dispatcher — provision legacy.
		_provision_legacy_site(self)

	def test_routes_through_shared_dispatcher(self):
		"""Retry must use the SAME dispatcher as send_message (after-commit
		publish on Path B, RQ otherwise). It previously duplicated the
		branch inline with a synchronous publish, keeping the
		mid-transaction race the shared dispatcher fixes - both dispatch
		flows must behave identically for every turn source."""
		user_id, asst_id = self._make_turn(self.conv, with_error=True)
		with patch("jarvis.chat.api._dispatch_turn") as dispatch:
			result = retry_message(asst_id)
		self.assertTrue(result["ok"])
		dispatch.assert_called_once()
		payload = dispatch.call_args[0][0]
		self.assertEqual(payload["conversation_id"], self.conv)
		self.assertEqual(payload["message_id"], user_id)
		self.assertEqual(payload["run_id"], result["run_id"])

	def test_enqueues_worker_against_preceding_user_message(self):
		user_id, asst_id = self._make_turn(self.conv, with_error=True)
		with patch("frappe.enqueue") as enqueue:
			result = retry_message(asst_id)
		self.assertTrue(result["ok"])
		self.assertIn("run_id", result)
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(kwargs["method"], "jarvis.chat.worker.run_agent_turn")
		self.assertEqual(kwargs["conversation_id"], self.conv)
		self.assertEqual(kwargs["message_id"], user_id)
		# Sprint-2: parity with send_message - the worker timeout covers worst case.
		self.assertEqual(kwargs["timeout"], _AGENT_TURN_WORKER_TIMEOUT)

	def test_rejects_non_assistant_message(self):
		user_id, _asst_id = self._make_turn(self.conv, with_error=True)
		result = retry_message(user_id)
		self.assertFalse(result["ok"])
		self.assertIn("assistant", result["reason"].lower())

	def test_rejects_message_without_error(self):
		_user_id, asst_id = self._make_turn(self.conv, with_error=False)
		result = retry_message(asst_id)
		self.assertFalse(result["ok"])
		self.assertIn("error", result["reason"].lower())

	def test_rejects_if_no_preceding_user_message(self):
		"""An orphan errored assistant (somehow inserted without a user) - refuse."""
		asst_doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv,
				"seq": 1,
				"role": "assistant",
				"content": "",
				"error": "boom",
			}
		)
		asst_doc.insert()
		frappe.db.commit()
		result = retry_message(asst_doc.name)
		self.assertFalse(result["ok"])
		self.assertIn("preceding", result["reason"].lower())

	def test_picks_immediately_preceding_user_message(self):
		"""When a conversation has multiple user turns, retry should target the
		one right before the errored assistant, not an older one.
		"""
		_u1, _a1 = self._make_turn(self.conv, user_text="first turn", with_error=False)
		u2, _a2 = self._make_turn(self.conv, user_text="second turn", with_error=False)
		# Insert a fresh errored assistant after u2
		seq_max = frappe.db.sql(
			"SELECT MAX(seq) FROM `tabJarvis Chat Message` WHERE conversation = %s",
			(self.conv,),
		)[0][0]
		errored = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv,
				"seq": seq_max + 1,
				"role": "assistant",
				"content": "",
				"error": "rate limit",
			}
		)
		errored.insert()
		frappe.db.commit()

		with patch("frappe.enqueue") as enqueue:
			retry_message(errored.name)
		_, kwargs = enqueue.call_args
		self.assertEqual(kwargs["message_id"], u2)

	def test_bumps_conversation_last_active_at(self):
		_u, asst_id = self._make_turn(self.conv, with_error=True)
		before = frappe.utils.get_datetime(frappe.get_value(CONV, self.conv, "last_active_at"))
		with patch("frappe.enqueue"):
			retry_message(asst_id)
		after = frappe.utils.get_datetime(frappe.get_value(CONV, self.conv, "last_active_at"))
		self.assertGreaterEqual(after, before)

	def test_rejects_when_subscription_suspended(self):
		"""The Retry button sits on the exact error a stopped container produces.
		A suspended retry must be refused HERE, not dispatched to the worker to
		grind the 25s WS-open loop and error identically all over again."""
		_u, asst_id = self._make_turn(self.conv, with_error=True)
		suspended = {"ready": False, "reason": "subscription_suspended", "detail": "expired"}
		with patch("jarvis.account._admin_chat_gate", return_value=suspended):
			with patch("jarvis.chat.api._dispatch_turn") as dispatch:
				result = retry_message(asst_id)
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason"], "subscription_suspended")
		dispatch.assert_not_called()


class TestSetConversationModel(_ChatTestCase):
	"""Per-conversation model override endpoint.

	Validates against jarvis.chat.api._SUBSCRIPTION_MODELS for the
	customer's current llm_provider. Empty/None clears the override.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._settings_snap = {
			"llm_auth_mode": settings.llm_auth_mode,
			"llm_provider": settings.llm_provider,
			"llm_model": settings.llm_model,
		}
		settings.db_set("llm_auth_mode", "oauth", update_modified=False)
		settings.db_set("llm_provider", "OpenAI", update_modified=False)
		settings.db_set("llm_model", "gpt-5.5", update_modified=False)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		settings = frappe.get_single("Jarvis Settings")
		for k, v in cls._settings_snap.items():
			settings.db_set(k, v, update_modified=False)
		frappe.db.commit()
		super().tearDownClass()

	def test_set_known_model_succeeds(self):
		conv = create_conversation()
		out = set_conversation_model(conv, "gpt-5.4-mini")
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["effective_model"], "gpt-5.4-mini")
		self.assertEqual(
			frappe.db.get_value(CONV, conv, "model_override"),
			"gpt-5.4-mini",
		)

	def test_clear_override_reverts_to_settings(self):
		conv = create_conversation()
		set_conversation_model(conv, "gpt-5.4-mini")
		out = set_conversation_model(conv, None)
		self.assertTrue(out["ok"])
		# Settings model is gpt-5.5 in this test class's setup
		self.assertEqual(out["data"]["effective_model"], "gpt-5.5")
		self.assertFalse(frappe.db.get_value(CONV, conv, "model_override"))

	def test_empty_string_clears_override(self):
		conv = create_conversation()
		set_conversation_model(conv, "gpt-5.4-mini")
		out = set_conversation_model(conv, "")
		self.assertTrue(out["ok"])
		self.assertFalse(frappe.db.get_value(CONV, conv, "model_override"))

	def test_unknown_model_rejected(self):
		conv = create_conversation()
		out = set_conversation_model(conv, "gpt-4o")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "unknown_model")
		# DB unchanged
		self.assertFalse(frappe.db.get_value(CONV, conv, "model_override"))

	def test_unknown_conversation_rejected(self):
		out = set_conversation_model("missing-conv-id-xyz", "gpt-5.5")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "unknown_conversation")


class TestSendMessageWithModelOverride(_ChatTestCase):
	"""send_message accepts an optional model_override that gets applied
	to the conversation BEFORE the worker is enqueued - so the first
	turn lands on the picked model without a race against the worker.

	CDX-10: the persist-before-enqueue capture asserts the LEGACY worker enqueue fires, so
	these run on a legacy-provisioned site (see setUp). The override-persistence invariant the
	pump path ALSO satisfies is covered by TestSendModelOverridePumpTwin below."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._settings_snap = {
			"llm_auth_mode": settings.llm_auth_mode,
			"llm_provider": settings.llm_provider,
			"llm_model": settings.llm_model,
		}
		settings.db_set("llm_auth_mode", "oauth", update_modified=False)
		settings.db_set("llm_provider", "OpenAI", update_modified=False)
		settings.db_set("llm_model", "gpt-5.5", update_modified=False)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		settings = frappe.get_single("Jarvis Settings")
		for k, v in cls._settings_snap.items():
			settings.db_set(k, v, update_modified=False)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		self.conv = create_conversation()
		# CDX-10: the persist-before-enqueue capture asserts the LEGACY worker enqueue.
		_provision_legacy_site(self)

	def test_valid_override_persists_before_enqueue(self):
		"""When model_override is passed, conv.model_override is set
		before frappe.enqueue is called (so the worker sees the right value)."""
		from jarvis.chat.api import send_message

		written = {}

		def capture(*a, **kw):
			# Snapshot the DB value at the moment enqueue is called
			written["override"] = frappe.db.get_value(CONV, self.conv, "model_override")

		with (
			patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"),
			patch("frappe.enqueue", side_effect=capture),
		):
			result = send_message(self.conv, "hi", model_override="gpt-5.4-mini")
		self.assertTrue(result["ok"])
		self.assertEqual(written["override"], "gpt-5.4-mini")

	def test_unknown_override_rejected(self):
		"""Invalid model name yields ok:false with no DB write or enqueue."""
		from jarvis.chat.api import send_message

		with patch("frappe.enqueue") as enqueue:
			result = send_message(self.conv, "hi", model_override="gpt-4o")
		self.assertFalse(result["ok"])
		self.assertIn("gpt-4o", result["reason"])
		enqueue.assert_not_called()
		# Conversation unchanged
		self.assertFalse(frappe.db.get_value(CONV, self.conv, "model_override"))

	def test_no_override_keeps_existing(self):
		"""Calling send_message without model_override doesn't touch
		conv.model_override (so per-conversation settings persist)."""
		from jarvis.chat.api import send_message

		# Pre-set an override
		frappe.db.set_value(CONV, self.conv, "model_override", "gpt-5.4")
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"), patch("frappe.enqueue"):
			send_message(self.conv, "hi")
		self.assertEqual(
			frappe.db.get_value(CONV, self.conv, "model_override"),
			"gpt-5.4",
		)


class TestSendModelOverridePumpTwin(_ChatTestCase):
	"""CDX-10 pump-mode twin: the override-persistence invariant the legacy test asserts ALSO
	holds on the PUMP path — send_message writes conv.model_override BEFORE the transport
	decision, so a pump-owned turn still lands on the picked model. Provisions the default pump
	site (transport_mode ROW = pump, conf absent = default-ON) and stubs the pump wake so no
	real hop enqueues."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._settings_snap = {
			"llm_auth_mode": settings.llm_auth_mode,
			"llm_provider": settings.llm_provider,
			"llm_model": settings.llm_model,
		}
		settings.db_set("llm_auth_mode", "oauth", update_modified=False)
		settings.db_set("llm_provider", "OpenAI", update_modified=False)
		settings.db_set("llm_model", "gpt-5.5", update_modified=False)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		settings = frappe.get_single("Jarvis Settings")
		for k, v in cls._settings_snap.items():
			settings.db_set(k, v, update_modified=False)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		self.conv = create_conversation()
		target = admission.DEFAULT_RELAY_TARGET
		self._prior_mode = frappe.db.get_value(PUMP, target, "transport_mode")
		pump.set_transport_mode(target, pump._MODE_PUMP)  # the default-ON site the fenced read decides on
		frappe.db.commit()
		self.addCleanup(self._restore_mode)

	def _restore_mode(self):
		frappe.db.set_value(
			PUMP, admission.DEFAULT_RELAY_TARGET, "transport_mode", self._prior_mode, update_modified=False
		)
		frappe.db.commit()

	def test_override_persists_on_pump_path(self):
		from jarvis.chat.api import send_message

		with (
			patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"),
			patch.object(pump, "ensure_pump", lambda *a, **k: None),
			patch.object(pump, "lpush_wake", lambda *a, **k: None),
		):
			result = send_message(self.conv, "hi", model_override="gpt-5.4-mini")
		self.assertTrue(result["ok"])
		# Pump path: the override was persisted to the conversation BEFORE dispatch — the same
		# invariant the legacy test asserts, holding cross-transport.
		self.assertEqual(frappe.db.get_value(CONV, self.conv, "model_override"), "gpt-5.4-mini")


class TestSendMessageThinkingOverride(_ChatTestCase):
	"""send_message accepts an optional thinking_override that persists to
	conv.thinking_override BEFORE the worker is enqueued - mirroring the
	model_override path."""

	def setUp(self):
		super().setUp()
		self.conv = create_conversation()
		# CDX-10: the persist-before-enqueue capture asserts the LEGACY worker enqueue.
		_provision_legacy_site(self)

	def test_valid_thinking_override_persists_before_enqueue(self):
		"""thinking_override='low' is written to conv before frappe.enqueue fires."""
		from jarvis.chat.api import send_message

		written = {}

		def capture(*a, **kw):
			written["thinking"] = frappe.db.get_value(CONV, self.conv, "thinking_override")

		with (
			patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"),
			patch("frappe.enqueue", side_effect=capture),
		):
			result = send_message(self.conv, "hi", thinking_override="low")
		self.assertTrue(result["ok"])
		self.assertEqual(written["thinking"], "low")

	def test_empty_string_clears_thinking_override(self):
		"""An empty string clears thinking_override (unlike model_override which
		ignores empty strings)."""
		from jarvis.chat.api import send_message

		frappe.db.set_value(CONV, self.conv, "thinking_override", "high")
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"), patch("frappe.enqueue"):
			result = send_message(self.conv, "hi", thinking_override="")
		self.assertTrue(result["ok"])
		self.assertEqual(frappe.db.get_value(CONV, self.conv, "thinking_override"), "")

	def test_none_keeps_existing_thinking_override(self):
		"""None for thinking_override does not touch conv.thinking_override."""
		from jarvis.chat.api import send_message

		frappe.db.set_value(CONV, self.conv, "thinking_override", "medium")
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"), patch("frappe.enqueue"):
			result = send_message(self.conv, "hi")
		self.assertTrue(result["ok"])
		self.assertEqual(
			frappe.db.get_value(CONV, self.conv, "thinking_override"),
			"medium",
		)

	def test_invalid_thinking_override_rejected(self):
		"""Invalid thinking level yields ok:false with no DB write or enqueue."""
		from jarvis.chat.api import send_message

		with patch("frappe.enqueue") as enqueue:
			result = send_message(self.conv, "hi", thinking_override="ultra")
		self.assertFalse(result["ok"])
		self.assertIn("ultra", result["reason"])
		enqueue.assert_not_called()


class TestThinkingOverride(FrappeTestCase):
	def setUp(self):
		self.conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "t"}).insert()

	def tearDown(self):
		frappe.delete_doc("Jarvis Conversation", self.conv.name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_set_thinking_persists_valid_level(self):
		from jarvis.chat import api

		out = api.set_conversation_thinking(self.conv.name, "low")
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["effective_thinking"], "low")
		self.assertEqual(
			frappe.db.get_value("Jarvis Conversation", self.conv.name, "thinking_override"),
			"low",
		)

	def test_set_thinking_rejects_invalid_level(self):
		from jarvis.chat import api

		out = api.set_conversation_thinking(self.conv.name, "ultra")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "unknown_thinking")

	def test_set_thinking_empty_clears(self):
		from jarvis.chat import api

		api.set_conversation_thinking(self.conv.name, "high")
		out = api.set_conversation_thinking(self.conv.name, "")
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["effective_thinking"], "medium")


class TestChatUiSettings(FrappeTestCase):
	"""get_chat_ui_settings serves the canonical subscription catalogue +
	per-provider defaults so the JS pages don't duplicate them."""

	def test_get_chat_ui_settings_exposes_subscription_catalogue(self):
		# Regression pin for "_SUBSCRIPTION_MODELS duplicated 4-5
		# times" - the catalogue + per-provider defaults are now
		# served from the single Python source so jarvis_onboarding.js
		# and jarvis_account.js can drop their hand-maintained copies.
		from jarvis._subscription_models import (
			DEFAULT_MODEL,
			SUBSCRIPTION_MODELS,
		)
		from jarvis.chat.api import get_chat_ui_settings

		s = get_chat_ui_settings()
		self.assertEqual(s["subscription_models"], SUBSCRIPTION_MODELS)
		self.assertEqual(s["default_models"], DEFAULT_MODEL)
		# Every default must be a member of its provider's allow-list -
		# protects against a future drift in jarvis/_subscription_models.py.
		for provider, default in DEFAULT_MODEL.items():
			self.assertIn(default, SUBSCRIPTION_MODELS[provider])


class TestWarmSessionEndpoint(FrappeTestCase):
	def test_warm_session_enqueues_not_inline(self):
		"""warm_session must enqueue warm_prefix as a background job and
		return immediately - proves FIX D (non-blocking web worker)."""
		from jarvis.chat import api

		with patch("frappe.enqueue") as enqueue, patch("jarvis.chat.prewarm.warm_prefix") as wp:
			out = api.warm_session()

		# Must have enqueued the prewarm job with the right method + queue.
		enqueue.assert_called_once_with(
			"jarvis.chat.prewarm.warm_prefix",
			queue="short",
		)
		# warm_prefix must NOT have been called inline in the web worker.
		wp.assert_not_called()
		self.assertEqual(out, {"ok": True, "enqueued": True})


INTRUDER_USER = "jarvis-test-intruder@example.com"


class TestConversationOwnershipEnforcement(_ChatTestCase):
	"""SEC-002 regression: every conversation-scoped endpoint must reject a
	caller who does not own the conversation with ``frappe.PermissionError``
	— and keep working for the legitimate owner.

	``frappe.get_doc`` performs NO permission check, so the endpoints assert
	ownership explicitly (``api._get_owned_conversation``); these tests pin
	that gate for both the read and the write paths. The intruder fixture
	deliberately holds System Manager (via ``_ensure_test_user``): even an
	admin-role tenant user must not reach another user's chat through these
	endpoints.
	"""

	def setUp(self):
		super().setUp()  # runs as TEST_USER — the legitimate owner
		_ensure_test_user(INTRUDER_USER)
		self.conv = create_conversation()
		# Seed one user turn + one errored assistant turn so the message-id
		# endpoints (get_canvas, retry_message) have targets.
		user_msg = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv,
				"seq": 1,
				"role": "user",
				"content": "what is our payroll?",
			}
		)
		user_msg.insert(ignore_permissions=True)
		asst_msg = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv,
				"seq": 2,
				"role": "assistant",
				"content": "",
				"error": "rate limit",
			}
		)
		asst_msg.insert(ignore_permissions=True)
		frappe.db.commit()
		self.user_msg = user_msg.name
		self.asst_msg = asst_msg.name

	def _as_intruder(self):
		frappe.set_user(INTRUDER_USER)

	# ---- read paths -------------------------------------------------- #

	def test_non_owner_cannot_read_conversation(self):
		self._as_intruder()
		with self.assertRaises(frappe.PermissionError):
			get_conversation(self.conv)

	def test_non_owner_cannot_read_canvas(self):
		self._as_intruder()
		with self.assertRaises(frappe.PermissionError):
			get_canvas(self.asst_msg)

	# ---- write paths ------------------------------------------------- #

	def test_non_owner_cannot_archive(self):
		self._as_intruder()
		with self.assertRaises(frappe.PermissionError):
			archive_conversation(self.conv)
		self.assertEqual(frappe.db.get_value(CONV, self.conv, "status"), "Active")

	def test_non_owner_cannot_rename(self):
		self._as_intruder()
		with self.assertRaises(frappe.PermissionError):
			rename_conversation(self.conv, "pwned")
		self.assertEqual(frappe.db.get_value(CONV, self.conv, "title"), "New chat")

	def test_non_owner_cannot_star(self):
		self._as_intruder()
		with self.assertRaises(frappe.PermissionError):
			set_star(self.conv, 1)
		self.assertFalse(frappe.db.get_value(CONV, self.conv, "starred"))

	def test_non_owner_cannot_send_message(self):
		self._as_intruder()
		with patch("jarvis.chat.api._dispatch_turn") as dispatch:
			with self.assertRaises(frappe.PermissionError):
				send_message(self.conv, "hijack this thread")
		dispatch.assert_not_called()
		# No message row was injected into the victim's conversation.
		self.assertEqual(len(frappe.get_all(MSG, filters={"conversation": self.conv})), 2)

	def test_non_owner_cannot_retry_message(self):
		self._as_intruder()
		with patch("jarvis.chat.api._dispatch_turn") as dispatch:
			with self.assertRaises(frappe.PermissionError):
				retry_message(self.asst_msg)
		dispatch.assert_not_called()

	def test_non_owner_cannot_set_model(self):
		# set_conversation_model mutates the row via db.set_value (bypasses
		# perms), so it must assert ownership explicitly (same IDOR class).
		self._as_intruder()
		with self.assertRaises(frappe.PermissionError):
			set_conversation_model(self.conv, "gpt-5.5")
		self.assertFalse(frappe.db.get_value(CONV, self.conv, "model_override"))

	def test_non_owner_cannot_set_thinking(self):
		self._as_intruder()
		with self.assertRaises(frappe.PermissionError):
			set_conversation_thinking(self.conv, "high")
		self.assertFalse(frappe.db.get_value(CONV, self.conv, "thinking_override"))

	def test_unknown_conversation_still_soft_errors_for_model(self):
		# A missing conversation must keep returning the structured
		# unknown_conversation error (not PermissionError) for the owner.
		out = set_conversation_model("missing-conv-xyz", "gpt-5.5")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "unknown_conversation")

	# ---- owner is not over-blocked ----------------------------------- #

	def test_owner_still_allowed(self):
		"""The ownership gate must not lock out the legitimate owner."""
		out = get_conversation(self.conv)
		self.assertEqual(out["conversation"]["name"], self.conv)
		# get_canvas passes the ownership gate for the owner: a message with
		# no canvas fails LATER with DoesNotExistError, never PermissionError.
		with self.assertRaises(frappe.DoesNotExistError):
			get_canvas(self.asst_msg)
		self.assertTrue(rename_conversation(self.conv, "my chat")["ok"])
		self.assertTrue(set_star(self.conv, 1)["ok"])
		with patch("jarvis.chat.api._dispatch_turn"):
			self.assertTrue(send_message(self.conv, "hello")["ok"])
			self.assertTrue(retry_message(self.asst_msg)["ok"])
		self.assertTrue(archive_conversation(self.conv)["ok"])
