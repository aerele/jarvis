"""Sharing authorization, lifecycle, projection and concurrency contracts.

All fixtures and handlers use test_jarvis. No owner check or recipient access
decision is mocked. A savepoint isolates each case, including expected refusals.
"""

import json
from datetime import timedelta
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime, now_datetime

from jarvis.tests._share_helpers import (
	ADMIN,
	DISABLED,
	MANAGER,
	MESSAGE,
	NO_ACCESS,
	OUTSIDER,
	OWNER,
	RECIPIENT,
	SHARE,
	WEBSITE,
	add_share,
	as_user,
	make_chat,
	make_message,
	make_people,
	make_user,
	payload,
)

RESPONSE_KEYS = {"conversation", "messages", "paging", "older_pending", "refreshed"}
CONVERSATION_KEYS = {"name", "title", "owner", "owner_name", "last_active_at", "status"}
ROW_KEYS = {
	"name",
	"role",
	"content",
	"creation",
	"modified",
	"seq",
	"streaming",
	"stopped",
	"recovering",
	"error_headline",
	"tool_name",
	"tool_status",
	"tool_args",
	"tool_result",
	"tool_result_compact",
	"action_outcome",
	"canvas",
	"steps",
	"live_steps",
	"live_step_times",
	"run_id",
	"pump_epoch",
	"last_event_seq",
	"turn_state",
	"ref_doctype",
	"ref_name",
	"tool_call_id",
	"tool_call_ids",
}
PENDING_KEYS = {"name", "seq", "role", "creation", "tool_name", "awaiting_owner"}
PAGING_KEYS = {"start", "before", "has_more", "older_counts"}


class SharingCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.previous_user = frappe.session.user
		frappe.set_user("Administrator")
		self.callbacks_before = {
			"after_commit": list(frappe.db.after_commit._functions),
			"after_rollback": list(frappe.db.after_rollback._functions),
		}
		frappe.db.savepoint("sharing_case")
		make_people()
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 1)
		self.conversation, self.message = make_chat()
		self.docshares = frappe.db.count("DocShare")

	def tearDown(self):
		frappe.db.rollback(save_point="sharing_case")
		for name, functions in self.callbacks_before.items():
			manager = getattr(frappe.db, name)
			manager._functions.clear()
			manager._functions.extend(functions)
		frappe.set_user(self.previous_user)
		frappe.flags.jarvis_share_write = None
		frappe.clear_cache()
		super().tearDown()

	def share(self, users=None):
		from jarvis.chat import sharing

		with as_user(OWNER):
			return sharing.set_shares(self.conversation, users if users is not None else [RECIPIENT])

	def read(self, user=RECIPIENT, **kwargs):
		from jarvis.chat import sharing

		with as_user(user):
			return sharing.get_shared_conversation(self.conversation, **kwargs)

	def assert_refused(self, user=RECIPIENT, reason="not_shared"):
		frappe.local.response.pop("http_status_code", None)
		self.assertEqual(self.read(user), {"refused": reason})
		self.assertEqual(frappe.local.response.http_status_code, 403)


class TestShareWrites(SharingCase):
	def test_owner_add_remove_duplicate_and_no_docshare(self):
		from jarvis.chat.sharing import get_shares

		self.share([RECIPIENT, RECIPIENT])
		self.share([RECIPIENT])
		self.assertEqual(frappe.db.count(SHARE, {"conversation": self.conversation}), 1)
		with as_user(OWNER):
			result = get_shares(self.conversation)
		self.assertEqual(set(result), {"enabled", "users"})
		self.assertEqual(
			set(result["users"][0]), {"name", "full_name", "shared_on", "hidden", "hidden_reason"}
		)
		self.assertNotIn("seen_marker", payload(result))
		self.share([])
		self.assertEqual(frappe.db.count(SHARE, {"conversation": self.conversation}), 0)
		self.assertEqual(frappe.db.count("DocShare"), self.docshares)

	def test_nonowners_cannot_manage_shares(self):
		from jarvis.chat.sharing import get_shares, set_shares

		for user in (RECIPIENT, OUTSIDER, MANAGER, ADMIN, "Administrator"):
			with self.subTest(user=user), as_user(user), self.assertRaises(frappe.PermissionError):
				set_shares(self.conversation, [OUTSIDER])
			with as_user(user), self.assertRaises(frappe.PermissionError):
				get_shares(self.conversation)

	def test_invalid_recipients_and_flag_restored(self):
		for user in (OWNER, "Guest", "Administrator", WEBSITE, NO_ACCESS, DISABLED, "not-a-user@example.com"):
			with self.subTest(user=user), self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
				self.share([user])
			self.assertFalse(frappe.flags.get("jarvis_share_write"))
		self.assertEqual(frappe.db.count(SHARE, {"conversation": self.conversation}), 0)

	def test_nonshareable_chats_refused(self):
		from jarvis.chat.sharing import set_shares

		for values in ({"file_box": 1}, {"origin_page": "dashboards"}, {"status": "Archived"}):
			conversation, _ = make_chat(**values)
			with as_user(OWNER), self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
				set_shares(conversation, [RECIPIENT])
		conversation, _ = make_chat("Administrator")
		with as_user("Administrator"), self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
			set_shares(conversation, [RECIPIENT])

	def test_additions_off_refused_but_hidden_removals_work(self):
		self.share([RECIPIENT, OUTSIDER])
		frappe.db.set_value("User", OUTSIDER, "enabled", 0)
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 0)
		self.share([RECIPIENT])
		with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
			self.share([RECIPIENT, ADMIN])
		self.share([])

	def test_visible_cap_keeps_hidden_rows_and_duplicate_is_noop(self):
		users = [make_user(f"share-cap-{n}@example.com") for n in range(25)]
		self.share(users)
		frappe.db.set_value("User", users[0], "enabled", 0)
		self.share(users + [users[1]])
		self.share(users + [RECIPIENT])
		self.assertEqual(frappe.db.count(SHARE, {"conversation": self.conversation}), 26)
		with self.assertRaises(frappe.ValidationError):
			self.share(users + [RECIPIENT, OUTSIDER])
		self.share(users[1:] + [RECIPIENT])
		self.assertEqual(frappe.db.count(SHARE, {"conversation": self.conversation}), 25)

	def test_controller_forgery_refused_despite_custom_docperm(self):
		# Grant the same create/write permissions a System Manager can grant in
		# production. Controller validation must still protect the victim owner.
		frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": SHARE,
				"role": "System Manager",
				"permlevel": 0,
				"create": 1,
				"write": 1,
				"read": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(doctype=SHARE)
		with as_user(MANAGER), self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{"doctype": SHARE, "conversation": self.conversation, "user": RECIPIENT, "shared_by": OWNER}
			).insert()

	def test_controller_identity_guard_is_required_inside_write_context(self):
		from jarvis.chat.sharing import share_write

		for actor, shared_by in ((MANAGER, OWNER), (OWNER, OUTSIDER), (OUTSIDER, OUTSIDER)):
			with (
				self.subTest(actor=actor, shared_by=shared_by),
				as_user(actor),
				share_write(),
				self.assertRaises(frappe.PermissionError),
			):
				frappe.get_doc(
					{
						"doctype": SHARE,
						"conversation": self.conversation,
						"user": RECIPIENT,
						"shared_by": shared_by,
					}
				).insert(ignore_permissions=True)
		self.assertFalse(frappe.db.exists(SHARE, {"conversation": self.conversation}))

	def test_shared_by_lookup_has_a_database_index(self):
		indexes = frappe.db.sql("SHOW INDEX FROM `tabJarvis Conversation Share`", as_dict=True)
		self.assertTrue(any(row.Column_name == "shared_by" and row.Seq_in_index == 1 for row in indexes))

	def test_controller_context_and_immutability(self):
		from jarvis.chat.sharing import share_write

		with as_user(OWNER), self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{"doctype": SHARE, "conversation": self.conversation, "user": RECIPIENT, "shared_by": OWNER}
			).insert(ignore_permissions=True)
		self.share()
		name = frappe.db.get_value(SHARE, {"conversation": self.conversation, "user": RECIPIENT})
		for field, value in (
			("user", OUTSIDER),
			("shared_by", OUTSIDER),
			("conversation", make_chat()[0]),
			("seen_marker", now_datetime()),
		):
			doc = frappe.get_doc(SHARE, name)
			doc.set(field, value)
			with (
				as_user(OWNER),
				share_write(),
				self.assertRaises((frappe.PermissionError, frappe.ValidationError)),
			):
				doc.save(ignore_permissions=True)
		with self.assertRaises(RuntimeError), share_write():
			raise RuntimeError("context cleanup")
		self.assertFalse(frappe.flags.jarvis_share_write)

	def test_get_refused_for_write_endpoints(self):
		from jarvis.chat import sharing

		for name in ("set_shares", "get_shared_conversation", "set_sharing_setting", "revoke_share"):
			fn = getattr(sharing, name)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])

	def test_candidates_are_filtered_server_side(self):
		from jarvis.chat.sharing import share_candidates

		with as_user(OWNER):
			rows = share_candidates("share-")
		self.assertLessEqual(len(rows), 20)
		self.assertTrue({RECIPIENT, OUTSIDER, MANAGER, ADMIN} <= {row["name"] for row in rows})
		self.assertFalse(
			{OWNER, WEBSITE, NO_ACCESS, DISABLED, "Guest", "Administrator"} & {row["name"] for row in rows}
		)
		self.assertTrue(all(set(row) == {"name", "full_name"} for row in rows))


class TestShareReads(SharingCase):
	def test_malformed_canvas_keeps_recipient_transcript_readable(self):
		self.share()
		for canvas in (42, True, "invalid canvas", {"unknown": "shape"}):
			with self.subTest(canvas=canvas):
				frappe.db.set_value(MESSAGE, self.message, "canvas", json.dumps(canvas))
				body = self.read()
				row = next(row for row in body["messages"] if row["name"] == self.message)
				self.assertEqual(row["canvas"], [])
				self.assertEqual(row["content"], "Shared transcript sentinel")

	def test_projection_scrubs_step_times(self):
		from jarvis.chat.share_projection import project_row

		row = project_row({"steps": [{"text": "safe", "at": "Bearer STEP-TIME-SENTINEL"}]}, self.conversation)
		self.assertNotIn("STEP-TIME-SENTINEL", payload(row))

	def test_projection_rewrites_nested_json_without_corrupting_it(self):
		from jarvis.chat.share_projection import safe_value

		original = json.dumps({"content": [{"text": json.dumps({"file_url": "/private/files/report.pdf"})}]})
		outer = json.loads(safe_value(original, self.conversation))
		inner = json.loads(outer["content"][0]["text"])
		self.assertIn("get_shared_file", inner["file_url"])

	def test_projection_rewrites_private_urls_in_dictionary_keys(self):
		from jarvis.chat.share_projection import safe_value

		keyed = safe_value({"/private/files/report.pdf": "report"}, self.conversation)
		self.assertNotIn("/private/files/", payload(keyed))
		self.assertEqual(list(keyed.values()), ["report"])

	def test_real_endpoint_scrubs_all_three_message_lists(self):
		with as_user(OWNER):
			frappe.db.set_value(
				"Jarvis Conversation",
				self.conversation,
				{"session_key": "PRIVATE-SESSION-SENTINEL", "model_override": "PRIVATE-MODEL-SENTINEL"},
			)
			pending = make_message(
				self.conversation,
				role="tool",
				tool_name="submit_doc",
				tool_status="pending",
				tool_call_id="PRIVATE-PENDING-ID",
				pending_card='{"token":"PRIVATE-PENDING-SENTINEL"}',
			)
			tool = make_message(
				self.conversation,
				role="tool",
				tool_name="get_doc",
				tool_status="completed",
				tool_args='{"apiKey":"PRIVATE-ARGS-SENTINEL"}',
				tool_result=json.dumps(
					{"result": {"content": [{"text": '{"access_token":"PRIVATE-MCP-SENTINEL"}'}]}}
				),
				tool_call_id="allowed-report-id",
			)
			make_message(self.conversation, content="New turn")
			assistant = make_message(
				self.conversation,
				role="assistant",
				content="Bearer PRIVATE-TEXT-SENTINEL",
				error="PRIVATE-ERROR-SENTINEL",
				steps='[{"text":"api_key=PRIVATE-STEP-SENTINEL","token":"PRIVATE-STEP-TOKEN"}]',
				canvas='[{"name":"artifact","type":"file","title":"Attachment","file_url":"/private/files/source.pdf","token":"PRIVATE-CANVAS-SENTINEL"}]',
			)
		self.share()
		result = self.read(limit=2, refresh=json.dumps([pending, tool]))
		self.assertEqual(set(result), RESPONSE_KEYS)
		self.assertEqual(set(result["conversation"]), CONVERSATION_KEYS)
		self.assertEqual({row["name"] for row in result["refreshed"]}, {pending, tool})
		self.assertIn(pending, {row["name"] for row in result["older_pending"]})
		self.assertIn(assistant, {row["name"] for row in result["messages"]})
		for key in ("messages", "older_pending", "refreshed"):
			for row in result[key]:
				self.assertTrue(set(row) <= (PENDING_KEYS if row.get("awaiting_owner") else ROW_KEYS))
		for secret in ("SESSION", "MODEL", "PENDING", "ARGS", "MCP", "TEXT", "ERROR", "STEP", "CANVAS"):
			self.assertNotIn(f"PRIVATE-{secret}-SENTINEL", payload(result))
		self.assertNotIn("PRIVATE-PENDING-ID", payload(result))
		self.assertNotIn("PRIVATE-STEP-TOKEN", payload(result))
		self.assertNotIn("/private/files/", payload(result))
		self.assertIn("allowed-report-id", payload(result))

	def test_every_owner_output_key_is_classified(self):
		from jarvis.tests.test_chat_owner_response import owner_fixture, owner_responses

		owner_fixture()
		for body in owner_responses().values():
			self.assertTrue(set(body) <= RESPONSE_KEYS | {"recent_prompts"})
			self.assertTrue(
				set(body["conversation"])
				<= CONVERSATION_KEYS
				| {
					"session_key",
					"model_override",
					"thinking_override",
					"auto_mode",
					"file_box",
					"origin_page",
				}
			)
			self.assertTrue(set(body.get("paging", {})) <= PAGING_KEYS | {"limit"})
			for key in ("messages", "older_pending", "refreshed"):
				for row in body.get(key, []):
					self.assertTrue(
						set(row)
						<= ROW_KEYS
						| {
							"error",
							"pending_card",
							"expires_at",
							"reply_duration_ms",
							"model",
							"provider",
							"created_at",
							"recent",
							"activity_state",
						}
					)

	def test_visibility_and_refusal_reasons(self):
		self.assert_refused()
		self.assert_refused(OWNER, "owner")
		self.share()
		self.assertEqual(set(self.read()), RESPONSE_KEYS)
		self.assert_refused(OUTSIDER)
		self.assert_refused(MANAGER)
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 0)
		self.assert_refused(reason="sharing_off")
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 1)
		self.assertIn("messages", self.read())

	def test_both_identities_must_remain_enabled_and_have_access(self):
		self.share()
		for user in (OWNER, RECIPIENT):
			frappe.db.set_value("User", user, "enabled", 0)
			self.assert_refused()
			frappe.db.set_value("User", user, "enabled", 1)
			self.assertIn("messages", self.read())
			frappe.db.delete("Has Role", {"parent": user, "role": "Jarvis User"})
			frappe.clear_cache(user=user)
			if user == RECIPIENT:
				with self.assertRaises(frappe.PermissionError):
					self.read()
			else:
				self.assert_refused()
			frappe.get_doc("User", user).add_roles("Jarvis User")
			self.assertIn("messages", self.read())
		self.assertEqual(frappe.db.count(SHARE, {"conversation": self.conversation}), 1)

	def test_shareability_rechecked_when_hooks_bypassed(self):
		self.share()
		for field, value, original in (
			("status", "Archived", "Active"),
			("origin_page", "dashboards", ""),
			("file_box", 1, 0),
		):
			frappe.db.set_value("Jarvis Conversation", self.conversation, field, value)
			self.assert_refused()
			frappe.db.set_value("Jarvis Conversation", self.conversation, field, original)
			self.assertIn("messages", self.read())

	def test_paging_limit_clamp_and_older_pending(self):
		with as_user(OWNER):
			pending = make_message(
				self.conversation,
				role="tool",
				tool_status="pending",
				tool_name="submit_doc",
				tool_call_id="PENDING-SENTINEL",
				pending_card='{"token":"PENDING-SECRET"}',
			)
			for n in range(8):
				make_message(self.conversation, content=f"entry {n}")
		self.share()
		newest = self.read(limit=0)
		self.assertEqual(len(newest["messages"]), 1)
		older = self.read(limit=2, before=6)
		self.assertTrue(all(row["seq"] < 6 for row in older["messages"]))
		refreshed = self.read(limit=1, refresh=json.dumps([pending]))
		for key in ("older_pending", "refreshed"):
			row = next(r for r in refreshed[key] if r["name"] == pending)
			self.assertEqual(set(row), PENDING_KEYS)
			self.assertTrue(row["awaiting_owner"])
		self.assertNotIn("PENDING-SECRET", payload(refreshed))

	def test_projection_exact_allowlists_and_nested_secret_scrubbing(self):
		from jarvis.chat.share_projection import project_conversation

		sentinels = [
			"SENTINEL-SESSION",
			"SENTINEL-MODEL",
			"SENTINEL-RAW-ERROR",
			"SENTINEL-CANVAS",
			"SENTINEL-TOKEN",
			"SENTINEL-MCP",
		]
		row = {key: None for key in ROW_KEYS}
		row.update(
			name="m",
			role="tool",
			tool_status="completed",
			content="safe",
			error="SENTINEL-RAW-ERROR",
			pending_card={"token": "SENTINEL-TOKEN"},
			tool_call_id="kept-id",
			tool_call_ids='["kept-id"]',
			tool_args='{"api_key":"SENTINEL-TOKEN","token_number":3}',
			tool_result=json.dumps({"result": {"content": [{"text": '{"access_token":"SENTINEL-MCP"}'}]}}),
			canvas=[
				{
					"name": "a",
					"type": "file",
					"title": "a",
					"file_url": "/private/files/a.pdf",
					"token": "SENTINEL-CANVAS",
				}
			],
			steps=[{"text": "api_key=SENTINEL-TOKEN", "at": "2026-01-01", "token": "SENTINEL-TOKEN"}],
			live_steps=["Bearer abcdefghijk"],
			extra="drop",
		)
		pending = dict(row, tool_status="pending")
		conversation = frappe.get_doc("Jarvis Conversation", self.conversation)
		body = {
			"conversation": {
				"name": self.conversation,
				"session_key": sentinels[0],
				"model_override": sentinels[1],
			},
			"messages": [row, pending],
			"older_pending": [pending],
			"refreshed": [row],
			"paging": {key: 0 for key in PAGING_KEYS} | {"limit": 80, "recent_prompts": ["SECRET"]},
		}
		result = project_conversation(body, conversation)
		self.assertEqual(set(result), RESPONSE_KEYS)
		self.assertEqual(set(result["conversation"]), CONVERSATION_KEYS)
		self.assertEqual(set(result["paging"]), PAGING_KEYS)
		for key in ("messages", "older_pending", "refreshed"):
			for item in result[key]:
				if item.get("awaiting_owner"):
					self.assertEqual(set(item), PENDING_KEYS)
				else:
					self.assertEqual(set(item), ROW_KEYS)
					self.assertEqual(set(item["canvas"][0]), {"name", "type", "title", "file_url"})
					self.assertEqual(set(item["steps"][0]), {"text", "at"})
					self.assertIn("get_shared_file", item["canvas"][0]["file_url"])
					self.assertEqual(item["tool_call_id"], "kept-id")
		for secret in sentinels:
			self.assertNotIn(secret, payload(result))

	def test_tool_detail_is_scrubbed_and_pending_is_minimal(self):
		from jarvis.chat.sharing import get_shared_tool_detail

		with as_user(OWNER):
			tool = make_message(
				self.conversation,
				role="tool",
				tool_status="completed",
				tool_args='{"api_key":"DETAIL-SECRET"}',
				tool_result='{"result":{"content":[{"text":"{\\"access_token\\":\\"MCP-SECRET\\"}"}]}}',
			)
			pending = make_message(
				self.conversation,
				role="tool",
				tool_status="pending",
				tool_args=json.dumps("PENDING-ARGS-SECRET"),
			)
		self.share()
		with as_user(RECIPIENT):
			result = get_shared_tool_detail(tool)
			pending_result = get_shared_tool_detail(pending)
		self.assertTrue(set(result) <= ROW_KEYS)
		self.assertNotIn("DETAIL-SECRET", payload(result))
		self.assertNotIn("MCP-SECRET", payload(result))
		self.assertEqual(set(pending_result), PENDING_KEYS)
		self.assertNotIn("PENDING-ARGS-SECRET", payload(pending_result))

	def test_no_stock_frappe_access(self):
		self.share()
		with as_user(RECIPIENT):
			self.assertFalse(frappe.has_permission("Jarvis Conversation", doc=self.conversation))
			self.assertFalse(frappe.get_list("Jarvis Conversation", filters={"name": self.conversation}))


class TestShareLifecycleAndUnseen(SharingCase):
	def test_invalidation_uses_planned_site_scoped_conversation_cache_before_and_after_commit(self):
		from jarvis.chat import sharing

		cache = frappe.cache()
		key = cache.make_key(f"jarvis:share:rcpt:{self.conversation}")
		unshared_key = cache.make_key("jarvis:share:rcpt:negative-entry")
		try:
			cache.set(key, b"old", ex=60)
			callbacks_before = len(frappe.db.after_commit._functions)
			self.share()
			self.assertIsNone(cache.get(key))
			cache.set(key, b"regenerated-before-commit", ex=60)
			callbacks = list(frappe.db.after_commit._functions)[callbacks_before:]
			with patch("jarvis.chat.events.publish_to_user"):
				for callback in callbacks:
					callback()
			self.assertIsNone(cache.get(key))
			cache.set(unshared_key, b"negative", ex=60)
			sharing.invalidate_all_recipients()
			self.assertIsNone(cache.get(unshared_key))
		finally:
			cache.delete(key, unshared_key)

	def test_shared_index_filters_before_limit_with_bounded_queries(self):
		from jarvis.chat.sharing import list_shared_with_me

		for n in range(53):
			conversation, _ = make_chat()
			add_share(conversation)
			if n < 2:
				# Deliberately bypass hooks: hidden rows must not fill the limit.
				frappe.db.set_value("Jarvis Conversation", conversation, "status", "Archived")
			if n == 2:
				with as_user(RECIPIENT):
					# Warm fixed framework User/DocType metadata independently
					# of list size; role-cache revocation has separate cold tests.
					self.assertEqual(len(list_shared_with_me()), 1)
					with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as small:
						self.assertEqual(len(list_shared_with_me()), 1)
		with as_user(RECIPIENT), patch.object(frappe.db, "sql", wraps=frappe.db.sql) as queries:
			rows = list_shared_with_me()
		self.assertEqual(len(rows), 50)
		self.assertLessEqual(
			queries.call_count, 20, "The shared index must not issue a query per stored share."
		)
		self.assertLessEqual(
			queries.call_count, small.call_count + 2, "List queries must stay bounded as shares grow."
		)

	def test_shared_index_and_direct_read_apply_the_same_visibility_rules(self):
		from jarvis.chat.sharing import list_shared_with_me

		self.share()
		for case in (
			"disabled_owner",
			"owner_without_access",
			"file_box",
			"dashboard",
			"admin_owner",
			"empty",
		):
			frappe.db.savepoint("index_visibility")
			try:
				with self.subTest(case=case):
					if case == "disabled_owner":
						frappe.db.set_value("User", OWNER, "enabled", 0)
					elif case == "owner_without_access":
						frappe.db.delete("Has Role", {"parent": OWNER, "parenttype": "User"})
					elif case == "file_box":
						frappe.db.set_value("Jarvis Conversation", self.conversation, "file_box", 1)
					elif case == "dashboard":
						frappe.db.set_value(
							"Jarvis Conversation", self.conversation, "origin_page", "dashboards"
						)
					elif case == "admin_owner":
						frappe.db.set_value(
							"Jarvis Conversation", self.conversation, "owner", "Administrator"
						)
					else:
						frappe.db.delete(MESSAGE, {"conversation": self.conversation})
					with as_user(RECIPIENT):
						self.assertNotIn(self.conversation, {row["name"] for row in list_shared_with_me()})
					self.assert_refused()
			finally:
				frappe.db.rollback(save_point="index_visibility")

	def test_archive_and_hard_delete_remove_rows(self):
		self.share()
		with as_user(OWNER):
			doc = frappe.get_doc("Jarvis Conversation", self.conversation)
			doc.status = "Archived"
			doc.save(ignore_permissions=True)
		self.assertFalse(frappe.db.exists(SHARE, {"conversation": self.conversation}))
		conversation, _ = make_chat()
		add_share(conversation)
		with as_user("Administrator"):
			frappe.delete_doc("Jarvis Conversation", conversation, ignore_permissions=True, force=True)
		self.assertFalse(frappe.db.exists(SHARE, {"conversation": conversation}))

	def test_deleting_recipient_removes_shares(self):
		self.share()
		with as_user("Administrator"):
			frappe.delete_doc("User", RECIPIENT, ignore_permissions=True, force=True)
		self.assertFalse(frappe.db.exists(SHARE, {"user": RECIPIENT}))

	def test_unseen_tracks_all_visible_rows_and_marker_is_monotone(self):
		from jarvis.chat.sharing import list_shared_with_me

		with as_user(OWNER):
			for n in range(85):
				make_message(self.conversation, role="user", content=f"turn {n}")
		self.share()
		with as_user(RECIPIENT):
			self.assertTrue(list_shared_with_me()[0]["unseen"])
		page = self.read(limit=80)
		self.assertNotIn(self.message, {row["name"] for row in page["messages"]})
		name = frappe.db.get_value(SHARE, {"conversation": self.conversation, "user": RECIPIENT})
		old = frappe.db.get_value(SHARE, name, "seen_marker")
		with patch.object(frappe.db, "set_value", wraps=frappe.db.set_value) as writes:
			self.read()
			seen_writes = [c for c in writes.call_args_list if c.args and c.args[0] == SHARE]
			self.assertEqual(len(seen_writes), 0)
		frappe.db.set_value(
			MESSAGE, self.message, "modified", get_datetime(old) + timedelta(seconds=2), update_modified=False
		)
		with as_user(RECIPIENT):
			self.assertTrue(list_shared_with_me()[0]["unseen"])
		self.read()
		with as_user(RECIPIENT):
			self.assertFalse(list_shared_with_me()[0]["unseen"])
		new = frappe.db.get_value(SHARE, name, "seen_marker")
		self.assertGreater(new, old)
		from jarvis.chat.sharing import _mark_seen

		_mark_seen(name, old)
		self.assertEqual(frappe.db.get_value(SHARE, name, "seen_marker"), new)

	def test_admin_people_search_revoke_and_metadata_only(self):
		from jarvis.chat.sharing import list_all_shares, revoke_share

		frappe.db.set_value("Jarvis Conversation", self.conversation, "title", "UNIQUE-TITLE-SEARCH")
		self.share()
		for user in (OWNER, RECIPIENT):
			with as_user(user), self.assertRaises(frappe.PermissionError):
				list_all_shares()
		for user in (MANAGER, ADMIN):
			with as_user(user):
				self.assertEqual(list_all_shares(search="UNIQUE-TITLE-SEARCH")["rows"], [])
				rows = list_all_shares(search="share-owner")["rows"]
			self.assertEqual(
				set(rows[0]),
				{"name", "conversation_title", "owner_name", "recipient_name", "shared_on", "hidden"},
			)
			self.assertNotIn("Shared transcript sentinel", payload(rows))
		with as_user(ADMIN):
			revoke_share(rows[0]["name"])
		self.assert_refused()

	def test_sidebar_additive_fields_count_hidden_shares(self):
		from jarvis.chat.api import list_conversations

		self.share([RECIPIENT, OUTSIDER])
		frappe.db.set_value("User", OUTSIDER, "enabled", 0)
		with as_user(OWNER):
			row = next(r for r in list_conversations(refresh=True) if r.name == self.conversation)
		self.assertEqual(row["shared_count"], 2)
		self.assertEqual(row["file_box"], 0)

	def test_setting_endpoints_admin_only_and_transition_audited_once(self):
		from jarvis.chat import sharing

		self.share()
		for user in (OWNER, RECIPIENT):
			with as_user(user), self.assertRaises(frappe.PermissionError):
				sharing.get_sharing_setting()
			with as_user(user), self.assertRaises(frappe.PermissionError):
				sharing.set_sharing_setting(False)
			self.assertTrue(sharing.sharing_enabled())
		with (
			patch.object(sharing, "invalidate_all_recipients") as invalidate,
			patch.object(sharing, "_audit") as audit,
		):
			with as_user(ADMIN):
				sharing.set_sharing_setting(False)
			self.assertEqual(audit.call_count, 1)
			self.assertEqual(invalidate.call_count, 1)
			callbacks = list(frappe.db.after_commit._functions)
			self.assertIn(sharing.invalidate_all_recipients, callbacks)
			with as_user(RECIPIENT):
				self.assertEqual(sharing.list_shared_with_me(), [])
		self.assertTrue(frappe.db.exists(SHARE, {"conversation": self.conversation}))
		# A full Desk save exercises the controller, including its early return.
		with (
			as_user("Administrator"),
			patch.object(sharing, "invalidate_all_recipients") as invalidate,
			patch.object(sharing, "_audit") as audit,
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.JarvisSettings._on_update_single_model_legacy"
			),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.JarvisSettings._on_update_unified_llm"
			),
		):
			doc = frappe.get_single("Jarvis Settings")
			doc.chat_sharing_enabled = 1
			doc.save(ignore_permissions=True)
			self.assertEqual(audit.call_count, 1)
			self.assertEqual(invalidate.call_count, 1)
		self.assertIn("messages", self.read())

	def test_default_seed_handles_missing_null_but_preserves_explicit_zero(self):
		from jarvis.chat.sharing import sharing_enabled
		from jarvis.learning.roles import _seed_personalise_settings_defaults

		for value in (None, "missing", "0"):
			frappe.db.delete("Singles", {"doctype": "Jarvis Settings", "field": "chat_sharing_enabled"})
			if value != "missing":
				frappe.db.sql(
					"INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s,%s,%s)",
					("Jarvis Settings", "chat_sharing_enabled", value),
				)
			_seed_personalise_settings_defaults()
			self.assertEqual(sharing_enabled(), value != "0")
		with (
			as_user("Administrator"),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.JarvisSettings._on_update_single_model_legacy"
			),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.JarvisSettings._on_update_unified_llm"
			),
		):
			frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 1)
			frappe.get_single("Jarvis Settings").save(ignore_permissions=True)
		self.assertTrue(sharing_enabled())

	def test_install_and_migrate_hooks_seed_sharing_on(self):
		from jarvis.chat.sharing import sharing_enabled
		from jarvis.install import after_install
		from jarvis.learning.roles import after_migrate

		for hook in (after_install, after_migrate):
			frappe.db.delete("Singles", {"doctype": "Jarvis Settings", "field": "chat_sharing_enabled"})
			with patch.object(frappe.db, "commit"), patch("jarvis.catalog_store.enqueue_refresh_all"):
				hook()
			self.assertTrue(sharing_enabled())
