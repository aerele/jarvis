"""Owner-only transcript export: content, secret boundaries and Redis admission."""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis._redis_lock import LOCK_PREFIX, redis_lock
from jarvis.chat import export as chat_export
from jarvis.chat.scrub import scrub_for_share
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._export.document import furniture

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "chart_blocks.json").read_text())
OWNER = "jarvis-share-owner@example.com"
RECIPIENT = "jarvis-share-recipient@example.com"
OUTSIDER = "jarvis-share-outsider@example.com"


def _table_rows(markdown: str) -> list[list[str]]:
	return [
		[
			html.unescape(re.sub(r"\\([\\`*_{}\[\]()#+.!|>~\-])", r"\1", cell.strip()))
			for cell in line.strip().strip("|").split("|")
		]
		for line in markdown.splitlines()
		if line.strip().startswith("|") and not re.fullmatch(r"[\s|:\-]+", line)
	]


def _readable(markdown: str) -> str:
	return html.unescape(re.sub(r"\\([\\`*_{}\[\]()#+.!|>~\-])", r"\1", markdown))


def _site_lock_name(name: str) -> str:
	return frappe.cache().make_key(name).decode()


def _lock_key(name: str) -> str:
	return LOCK_PREFIX + _site_lock_name(name)


class TestChatExportScrub(unittest.TestCase):
	def test_safe_keys_survive_and_sensitive_keys_are_redacted(self):
		safe = {
			"author": "Navin",
			"keyword": "balance",
			"is_private": True,
			"token_number": "ABC-12",
			"token_count": 42,
			"key_field": "customer",
			"session_date": "2026-10-16",
			"subsidiary": "Company A",
		}
		secrets = {
			key: f"secret-{index}"
			for index, key in enumerate(
				(
					"access_token",
					"refresh_token",
					"api_key",
					"apiKey",
					"session_key",
					"x-auth-token",
					"Authorization",
					"client_secret",
					"db_password",
					"password",
				)
			)
		}
		original = {**safe, **secrets}
		actual = scrub_for_share(original)
		for key, value in safe.items():
			with self.subTest(key=key):
				self.assertEqual(actual[key], value)
		for value in secrets.values():
			self.assertNotIn(value, json.dumps(actual))
		self.assertEqual(original, {**safe, **secrets}, "scrubbing must not mutate the stored value")

	def test_value_rule_also_scrubs_under_kept_keys(self):
		actual = scrub_for_share({"token_number": "Bearer KEPT-KEY-SECRET"})
		self.assertNotIn("KEPT-KEY-SECRET", json.dumps(actual))

	def test_nested_json_mcp_text_is_scrubbed(self):
		actual = scrub_for_share(
			json.dumps(
				{"result": {"content": [{"text": '{"access_token":"MCP-NESTED-SECRET","author":"Kept"}'}]}}
			)
		)
		self.assertNotIn("MCP-NESTED-SECRET", json.dumps(actual))
		self.assertIn("Kept", json.dumps(actual))

	def test_safe_chart_and_card_json_is_preserved_exactly(self):
		for tag in ("jarvis-chart", "jarvis-cards"):
			text = f'Before\n```{tag}\n{{ "title" : "author", "token_number" : 2 }}\n```\nAfter'
			with self.subTest(tag=tag):
				self.assertEqual(scrub_for_share(text), text)

	def test_plain_message_secrets_are_scrubbed(self):
		actual = scrub_for_share(
			'Authorization: Bearer BEARER-SENTINEL and "access_token": "INLINE-SENTINEL"'
		)
		self.assertNotIn("BEARER-SENTINEL", actual)
		self.assertNotIn("INLINE-SENTINEL", actual)

	def test_large_plain_message_scrubs_in_bounded_time(self):
		text = "a" * 200_000
		started = time.monotonic()
		self.assertEqual(scrub_for_share(text), text)
		self.assertLess(time.monotonic() - started, 2)

	def test_every_value_prefix_and_jwt_is_scrubbed(self):
		secrets = (
			"sk-APIKEYSENTINEL1234567890",
			"xoxb-SLACKSENTINEL12345",
			"ghp_GITHUBSENTINEL1234567890",
			"AKIAABCDEFGHIJKLMNOP",
			"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJTRUNSRVQifQ.c2lnbmF0dXJl",
		)
		for secret in secrets:
			with self.subTest(secret=secret):
				self.assertNotIn(secret, scrub_for_share(f"Before {secret} after"))

	def test_white_label_redaction_applies_after_secret_scrubbing(self):
		with patch(
			"jarvis.chat.egress_rules.redact",
			side_effect=lambda text: text.replace("InternalBrand", "Assistant"),
		):
			self.assertEqual(scrub_for_share({"author": "InternalBrand"}), {"author": "Assistant"})


class TestChatExportBlocks(unittest.TestCase):
	def test_every_chart_fixture_becomes_the_expected_table(self):
		for fixture in FIXTURES:
			with self.subTest(name=fixture["name"]):
				out = chat_export._render_chart(fixture["body"], mermaid=fixture["tag"] == "mermaid")
				rows = _table_rows(out)
				self.assertTrue(rows, out)
				for header in fixture["expected_headers"]:
					self.assertIn(header, rows[0])
				for expected in fixture["expected_rows"]:
					self.assertIn(expected, rows, out)

	def test_python_block_handlers_cover_frontend_tags(self):
		text = (ROOT / "frontend/src/lib/chatBlocks.js").read_text()
		match = re.search(r"export const BLOCK_TAGS\s*=\s*\[([\s\S]*?)\]", text)
		self.assertIsNotNone(match)
		frontend_tags = set(re.findall(r'"([^"]+)"', match.group(1)))
		self.assertEqual(set(chat_export.BLOCK_TAGS), frontend_tags)

	def test_python_charts_cover_frontend_types_and_fixture(self):
		text = (ROOT / "frontend/src/views/ChatView.vue").read_text()
		match = re.search(r"const _CHART_TYPES\s*=\s*new Set\(\[([\s\S]*?)\]\)", text)
		self.assertIsNotNone(match)
		frontend_types = set(re.findall(r'"([^"]+)"', match.group(1)))
		self.assertEqual(set(chat_export.CHART_TYPES), frontend_types)
		self.assertEqual({f["name"] for f in FIXTURES if f["tag"] == "jarvis-chart"}, frontend_types)

	def test_inert_blocks_are_summarized_and_actions_are_dropped(self):
		text = "\n".join(
			(
				"Before",
				'```jarvis-ask\n[{"q":"Which company?","options":["A","B"]}]\n```',
				'```jarvis-cards\n[{"title":"Invoice","doctype":"Sales Invoice","name":"SINV-001","fields":[{"label":"Amount","value":"100"}]}]\n```',
				"```jarvis-skill\ncustom-accounts, stock\n```",
				'```jarvis-macro\n{"name":"Daily routine","steps":[{"prompt":"PRIVATE MACRO PROMPT"}]}\n```',
				'```jarvis-goto\n{"route":"PRIVATE-GOTO"}\n```',
				'```jarvis-action\n{"token":"PRIVATE-ACTION"}\n```',
				'```confirm\n{"token":"PRIVATE-CONFIRM"}\n```',
				"```mermaid\nflowchart LR\n A-->B\n```",
				"After",
			)
		)
		out = chat_export._render_blocks(text)
		for kept in (
			"Before",
			"After",
			"Which company?",
			"Invoice",
			"SINV-001",
			"Amount",
			"100",
			"Skills used:",
			"accounts",
			"stock",
			"Saved routine: Daily routine",
			"```mermaid",
			"A-->B",
		):
			self.assertIn(kept, _readable(out))
		for dropped in (
			"PRIVATE-GOTO",
			"PRIVATE-ACTION",
			"PRIVATE-CONFIRM",
			"PRIVATE MACRO PROMPT",
			"jarvis-action",
			"jarvis-goto",
			"```confirm",
		):
			self.assertNotIn(dropped, out)

	def test_bad_chart_has_readable_fallback(self):
		out = chat_export._render_chart('{"type":"unknown","title":"Chart title"}')
		self.assertIn("[Chart: Chart title]", out)
		self.assertNotIn('"type"', out)

	def test_table_cells_cannot_break_rows(self):
		out = chat_export._render_chart(
			json.dumps({"type": "bar", "x": ["A|B\nC"], "series": [{"name": "Amount", "data": [7]}]})
		)
		self.assertIn("A\\|B", out)
		self.assertNotIn("A|B\nC", out)

	def test_inline_and_indented_action_fences_are_dropped(self):
		for prefix in ("Text ", "    "):
			with self.subTest(prefix=prefix):
				out = chat_export._render_blocks(
					prefix + '```jarvis-action\n{"token":"INLINE-ACTION-SENTINEL"}\n``` tail'
				)
				self.assertNotIn("INLINE-ACTION-SENTINEL", out)
				self.assertIn("tail", out)

	def test_repeated_urls_render_in_bounded_time(self):
		text = "https://" * 25_000
		conv = frappe._dict(title="Performance", owner=OWNER)
		row = frappe._dict(role="assistant", content=text, creation="2026-10-01 09:00:00")
		started = time.monotonic()
		with (
			patch("frappe.db.get_value", return_value="Owner"),
			patch("frappe.db.get_single_value", return_value="Jarvis"),
			patch("jarvis.chat.egress_rules.get_rules", return_value=[]),
			patch(
				"jarvis.chat.export.now_datetime",
				return_value=frappe.utils.get_datetime("2026-10-16 12:00:00"),
			),
		):
			out = chat_export._markdown(conv, [row])
		self.assertIn("https://", out)
		self.assertLess(time.monotonic() - started, 2)


class TestChatExportEndpoint(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.assertNotIn(
			frappe.local.site,
			{"jarvis.localhost", "jarvis_admin.localhost", "jarvis_helpdesk.localhost"},
			"mutating export tests require a dedicated test site",
		)
		self.original_user = frappe.session.user
		frappe.set_user("Administrator")
		from jarvis.permissions import ensure_jarvis_user_role

		ensure_jarvis_user_role()
		for email, first_name, roles in (
			(OWNER, "Export Owner", ["Jarvis User"]),
			(RECIPIENT, "Export Recipient", ["Jarvis User"]),
			(OUTSIDER, "Export Outsider", ["Jarvis User", "System Manager"]),
		):
			if not frappe.db.exists("User", email):
				doc = frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": first_name,
						"enabled": 1,
						"send_welcome_email": 0,
						"user_type": "System User",
					}
				).insert(ignore_permissions=True)
				doc.add_roles(*roles)
		self.cache = frappe.cache()
		self._clear_export_keys()
		frappe.set_user(OWNER)
		self.conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "Export fixture"}).insert(
			ignore_permissions=True
		)
		self.seq = 0
		self.agent_name = frappe.db.get_single_value("Jarvis Settings", "agent_name") or "Jarvis"
		self.owner_name = frappe.db.get_value("User", OWNER, "full_name")

	def tearDown(self):
		self._clear_export_keys()
		frappe.set_user(self.original_user)
		super().tearDown()

	def _clear_export_keys(self):
		keys = [self.cache.make_key(f"jarvis:export:rl:{user}") for user in (OWNER, RECIPIENT, OUTSIDER)]
		keys += [_lock_key(f"export:{user}") for user in (OWNER, RECIPIENT, OUTSIDER)]
		keys += [_lock_key(f"export:pdf:slot:{slot}") for slot in (1, 2)]
		self.cache.delete(*keys)

	def _message(self, role="assistant", **values):
		self.seq += 1
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conv.name,
				"seq": self.seq,
				"role": role,
				**values,
			}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		return doc

	def _download(self, format="md"):
		frappe.response.clear()
		chat_export.download_conversation(self.conv.name, format)
		return dict(frappe.response)

	def _markdown(self):
		body = self._download()["filecontent"]
		return body.decode() if isinstance(body, bytes) else body

	def _security_fixture(self):
		frappe.db.set_value(
			"Jarvis Conversation", self.conv.name, "session_key", f"S1-SESSION-SECRET-{self.conv.name}"
		)
		self._message(
			content='Visible answer. Authorization: Bearer S2 and "access_token": "S5"',
			model="S4",
			provider="PROVIDER-SENTINEL",
			error="S3 upstream",
		)
		self._message(
			"tool",
			tool_name="jarvis__create_doc",
			tool_status="pending",
			tool_args='{"password":"ARGS-SENTINEL"}',
			tool_result='{"secret":"RESULT-SENTINEL"}',
			pending_card='{"token":"PENDING-TOKEN-SENTINEL"}',
		)

	def _assert_no_secrets(self, text):
		text = _readable(text)
		for secret in (
			"S1-SESSION-SECRET",
			"S2",
			"S3 upstream",
			"S4",
			"S5",
			"PENDING-TOKEN-SENTINEL",
			"ARGS-SENTINEL",
			"RESULT-SENTINEL",
			"PROVIDER-SENTINEL",
		):
			self.assertNotIn(secret, text)

	def test_order_header_hidden_and_automated_origin(self):
		first = self._message("user", content="FIRST *unsafe* [label](url)", origin="human")
		self._message(content="LAST visible answer")
		self._message(content="HIDDEN-SENTINEL", hidden=1)
		self._message("user", content="AUTOMATED-PRIVATE-PROMPT", origin="macro")
		frappe.db.set_value(
			"Jarvis Chat Message", first.name, "creation", "2026-10-01 09:00:00", update_modified=False
		)
		out = self._markdown()
		for expected in (
			"Export fixture",
			"Exported on",
			"2026-10-01",
			self.owner_name,
			self.agent_name,
			"Automated step",
			"> FIRST \\*unsafe\\*",
		):
			self.assertIn(expected, out if expected.startswith("> FIRST") else _readable(out))
		self.assertLess(out.index("FIRST"), out.index("LAST"))
		self.assertNotIn("HIDDEN-SENTINEL", out)
		self.assertNotIn("AUTOMATED-PRIVATE-PROMPT", out)

	def test_empty_chat_download_is_a_markdown_attachment(self):
		response = self._download()
		self.assertEqual(response["type"], "download")
		self.assertEqual(response["content_type"], "text/markdown; charset=utf-8")
		self.assertIn("No messages yet", response["filecontent"].decode())

	def test_non_owner_user_and_system_manager_are_refused(self):
		for user in (RECIPIENT, OUTSIDER):
			with self.subTest(user=user):
				frappe.set_user(user)
				with self.assertRaises(frappe.PermissionError):
					self._download()

	def test_guest_is_refused_and_endpoint_is_get_only(self):
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[chat_export.download_conversation], ["GET"]
		)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			self._download()

	def test_human_legacy_delegated_and_board_answer_origins_keep_user_text(self):
		for origin in ("", "human", "delegated", "board_answer"):
			self._message("user", content=f"Readable {origin or 'legacy'} content", origin=origin)
		out = self._markdown()
		for origin in ("legacy", "human", "delegated", "board_answer"):
			self.assertIn(f"Readable {origin} content", _readable(out))

	def test_success_and_failure_audit_contains_only_admission_metadata(self):
		self._message(content="AUDIT-CONTENT-SENTINEL")
		with patch("frappe.logger") as logger:
			self._download()
			with self.assertRaises(frappe.ValidationError):
				self._download("UNTRUSTED-INVALID-FORMAT")
		logs = [json.loads(call.args[0]) for call in logger.return_value.info.call_args_list]
		self.assertEqual(len(logs), 2)
		self.assertEqual([entry["ok"] for entry in logs], [True, False])
		for entry in logs:
			self.assertEqual(set(entry), {"actor", "action", "conversation", "format", "ok"})
			self.assertEqual(entry["actor"], OWNER)
			self.assertEqual(entry["action"], "export")
			self.assertEqual(entry["conversation"], self.conv.name)
			self.assertNotIn("AUDIT-CONTENT-SENTINEL", json.dumps(entry))
			self.assertNotIn("UNTRUSTED-INVALID-FORMAT", json.dumps(entry))

	def test_sentinels_absent_from_markdown(self):
		self._security_fixture()
		self._assert_no_secrets(self._markdown())

	def test_sentinels_absent_from_pdf_html(self):
		self._security_fixture()
		with patch.object(furniture, "render_pdf", return_value=b"%PDF-1.4 fixture") as render:
			response = self._download("pdf")
		self.assertEqual(response["filecontent"], b"%PDF-1.4 fixture")
		self.assertEqual(response["content_type"], "application/pdf")
		self._assert_no_secrets(render.call_args.args[0])
		self.assertIn("Visible answer", render.call_args.args[0])

	def test_export_never_fetches_tool_args_results_or_provider_data(self):
		self._security_fixture()
		real_get_all = frappe.get_all
		with patch("frappe.get_all", wraps=real_get_all) as queries:
			self._download()
		message_calls = [
			call for call in queries.call_args_list if call.args and call.args[0] == "Jarvis Chat Message"
		]
		self.assertTrue(message_calls)
		for call in message_calls:
			for field in ("tool_args", "tool_result", "pending_card", "model", "provider"):
				self.assertNotIn(field, call.kwargs["fields"])

	def test_review_secrets_and_safe_prose_through_both_export_formats(self):
		self._message(
			"user",
			content="Before ASIAIOSFODNN7EXAMPLE after\n"
			"-----BEGIN PRIVATE KEY-----\nUEVNLVNFTlRJTkVM\n-----END PRIVATE KEY-----\n"
			"Authorization: bearer S2",
		)
		self._message(
			content="Company Secretary: Mr. Rao\nStock item SK-1001\n"
			"The bearer cheque was deposited.\n\n**Key:** Keep the invoice reference."
		)
		for format in ("md", "pdf"):
			with (
				self.subTest(format=format),
				patch.object(furniture, "render_pdf", return_value=b"%PDF-fixture") as renderer,
			):
				response = self._download(format)
				text = response["filecontent"].decode() if format == "md" else renderer.call_args.args[0]
				for secret in ("ASIAIOSFODNN7EXAMPLE", "UEVNLVNFTlRJTkVM", "S2"):
					self.assertNotIn(secret, text)
				for ordinary in ("Company Secretary: Mr. Rao", "SK-1001", "bearer cheque"):
					self.assertIn(ordinary, _readable(text))
				self.assertIn("**Key:**" if format == "md" else "<strong>Key:</strong>", text)

	def test_malformed_chart_and_deep_json_do_not_block_either_format(self):
		malformed = {
			"type": "bar",
			"title": "Malformed records",
			"x": "label",
			"data": [{"label": "A", "amount": 10}],
			"series": [{"name": "Amount", "field": ["amount"]}],
		}
		self._message(content="```jarvis-chart\n" + json.dumps(malformed) + "\n```")
		self._message("user", content="[" * 600 + '{"access_token":"DEEP-EXPORT-SENTINEL"}' + "]" * 600)
		self._message(
			content='```jarvis-chart\n{"type":"bar","x":["A"],"series":[{"name":"Sales","data":[203,307]}]}\n```'
		)
		for format in ("md", "pdf"):
			with (
				self.subTest(format=format),
				patch.object(furniture, "render_pdf", return_value=b"%PDF-fixture") as renderer,
			):
				response = self._download(format)
				text = response["filecontent"].decode() if format == "md" else renderer.call_args.args[0]
				self.assertIn("[Chart: Malformed records]", _readable(text))
				self.assertNotIn("DEEP-EXPORT-SENTINEL", text)
				self.assertIn("203", text)
				self.assertIn("307", text)

	def test_attachments_links_receipts_errors_and_stopped(self):
		self._message(
			content="[Invoice](/app/sales-invoice/SINV-001)",
			canvas=json.dumps(
				[{"title": "Report", "type": "pdf", "file_url": "/private/files/PRIVATE-URL-SENTINEL.pdf"}]
			),
			stopped=1,
			error="unrecognized RAW-ERROR-SENTINEL",
		)
		self._message(
			"tool",
			tool_name="jarvis__create_doc",
			action_outcome="confirmed",
			ref_doctype="Sales Invoice",
			ref_name="SINV-001",
		)
		self._message(
			"tool",
			tool_name="jarvis__update_doc",
			action_outcome="auto_applied",
			ref_doctype="Customer",
			ref_name="CUST-001",
		)
		self._message("tool", tool_name="jarvis__delete_doc", action_outcome="discarded")
		self._message("tool", tool_name="jarvis__set_value", action_outcome="failed")
		self._message("tool", tool_name="jarvis__get_list", tool_status="pending")
		out = _readable(self._markdown())
		for expected in (
			"[Attachment: Report (pdf)]",
			frappe.utils.get_url("/app/sales-invoice/SINV-001"),
			"SINV-001",
			"Customer CUST-001",
			"Discarded",
			"Failed",
			"Awaiting confirmation",
			"(stopped)",
		):
			self.assertIn(expected, out)
		for secret in ("PRIVATE-URL-SENTINEL", "RAW-ERROR-SENTINEL", "jarvis__"):
			self.assertNotIn(secret, out)

	def test_private_file_links_in_plain_text_are_removed(self):
		self._message(
			content="See [private](/private/files/PLAIN-PRIVATE-SENTINEL.pdf) and /private/files/BARE-PRIVATE-SENTINEL.csv"
		)
		out = self._markdown()
		self.assertNotIn("PLAIN-PRIVATE-SENTINEL", out)
		self.assertNotIn("BARE-PRIVATE-SENTINEL", out)

	def test_encoded_private_file_urls_cannot_reach_pdf_html(self):
		self._message(content='<a href="&#47;private&#47;files&#47;ENCODED-SENTINEL.pdf">Link</a>')
		with patch.object(furniture, "render_pdf", return_value=b"%PDF-1.4 fixture") as render:
			self._download("pdf")
		self.assertNotIn("ENCODED-SENTINEL", render.call_args.args[0])

	def test_user_multiline_credentials_scrub_before_blockquote_escaping(self):
		self._message(
			"user",
			content='Authorization: Bearer\nMULTILINE-BEARER-SENTINEL\n"access_token":\n"MULTILINE-KEY-SENTINEL"',
			origin="human",
		)
		out = self._markdown()
		with patch.object(furniture, "render_pdf", return_value=b"%PDF-1.4 fixture") as render:
			self._download("pdf")
		for body in (out, render.call_args.args[0]):
			self.assertNotIn("MULTILINE-BEARER-SENTINEL", body)
			self.assertNotIn("MULTILINE-KEY-SENTINEL", body)

	def test_html_app_links_are_made_absolute_in_pdf(self):
		self._message(content='<a href="/app/customer/CUST-001">Customer</a>')
		with patch.object(furniture, "render_pdf", return_value=b"%PDF-1.4 fixture") as render:
			self._download("pdf")
		self.assertIn(frappe.utils.get_url("/app/customer/CUST-001"), render.call_args.args[0])

	def test_streaming_step_cache_is_removed_and_progress_is_marked(self):
		seed = self._message("user", content="Count invoices")
		assistant = self._message(content="Checking overdue invoices.\n\nThe answer is three.", streaming=1)
		turn_name = "export-test-" + frappe.generate_hash(length=8)
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Turn",
				"run_id": turn_name,
				"conversation": self.conv.name,
				"relay_target_id": "export-test-target",
				"turn_class": "interactive",
				"state": "streaming",
				"seed_message": seed.name,
				"assistant_message": assistant.name,
				"enqueued_at": frappe.utils.now(),
			}
		).insert(ignore_permissions=True)
		with patch("jarvis.chat.pump._read_run_steps", return_value=["Checking overdue invoices."]):
			out = self._markdown()
		self.assertIn("The answer is three.", out)
		self.assertIn("(in progress at export time)", out)
		self.assertNotIn("Checking overdue invoices.", out)

	def test_used_tool_summary_is_per_turn(self):
		self._message("user", content="First question")
		self._message("tool", tool_name="jarvis__get_list", tool_status="completed")
		self._message("tool", tool_name="jarvis__get_doc", tool_status="completed")
		self._message(content="First answer")
		self._message("user", content="Second question")
		self._message("tool", tool_name="jarvis__count", tool_status="completed")
		self._message(content="Second answer")
		out = _readable(self._markdown())
		self.assertIn("Used 2 tools:", out)
		self.assertIn("get_list", out)
		self.assertIn("get_doc", out)
		self.assertIn("Used 1 tool", out)
		self.assertIn("count", out)

	def test_sixth_export_in_window_is_refused_before_rendering(self):
		for _ in range(5):
			self._download()
		with patch.object(furniture, "render_pdf") as render:
			with self.assertRaises(frappe.ValidationError):
				self._download("pdf")
		render.assert_not_called()
		rate_key = self.cache.make_key(f"jarvis:export:rl:{OWNER}")
		self.assertLessEqual(self.cache.ttl(rate_key), 60)
		self.assertGreater(self.cache.ttl(rate_key), 0)

	def test_concurrent_same_user_export_is_refused(self):
		with redis_lock(_site_lock_name(f"export:{OWNER}"), timeout_s=90) as acquired:
			self.assertTrue(acquired)
			with self.assertRaises(frappe.ValidationError):
				self._download()

	def test_sequential_exports_release_the_per_user_lock(self):
		self.assertIn("No messages yet", self._markdown())
		self.assertIn("No messages yet", self._markdown())
		self.assertIsNone(self.cache.get(_lock_key(f"export:{OWNER}")))

	def test_third_simultaneous_pdf_is_refused(self):
		with redis_lock(_site_lock_name("export:pdf:slot:1"), timeout_s=90) as slot1:
			with redis_lock(_site_lock_name("export:pdf:slot:2"), timeout_s=90) as slot2:
				self.assertTrue(slot1 and slot2)
				with patch.object(furniture, "render_pdf") as render:
					with self.assertRaisesRegex(frappe.ValidationError, "Too many exports are running"):
						self._download("pdf")
					render.assert_not_called()

	def test_second_slot_is_used_when_first_is_busy_and_all_keys_are_site_prefixed(self):
		prefix = self.cache.make_key("").decode()

		def while_rendering(*args, **kwargs):
			for raw in (f"export:{OWNER}", "export:pdf:slot:1", "export:pdf:slot:2"):
				key = _lock_key(raw)
				self.assertIn(prefix, key)
				self.assertIsNotNone(self.cache.get(key))
				self.assertLessEqual(self.cache.ttl(key), 90)
			return b"%PDF-1.4 fixture"

		with redis_lock(_site_lock_name("export:pdf:slot:1"), timeout_s=90):
			with patch.object(furniture, "render_pdf", side_effect=while_rendering):
				self._download("pdf")
		self.assertIsNone(self.cache.get(_lock_key("export:pdf:slot:2")))

	def test_expired_request_release_preserves_new_holder_token(self):
		name = _site_lock_name(f"export:{OWNER}")
		key = LOCK_PREFIX + name
		with redis_lock(name, timeout_s=90) as acquired:
			self.assertTrue(acquired)
			self.cache.delete(key)  # old holder expires while the request is rendering
			replacement = self.cache.lock(key, timeout=90)
			self.assertTrue(replacement.acquire(blocking=False))
			new_token = self.cache.get(key)
		self.assertEqual(self.cache.get(key), new_token)
		replacement.release()

	def test_killed_pdf_slot_expires_and_next_export_reuses_it(self):
		key = _lock_key("export:pdf:slot:1")
		killed = self.cache.lock(key, timeout=0.1)
		self.assertTrue(killed.acquire(blocking=False))
		with redis_lock(_site_lock_name("export:pdf:slot:2"), timeout_s=90):
			with self.assertRaisesRegex(frappe.ValidationError, "Too many exports are running"):
				self._download("pdf")
			time.sleep(0.15)
			self.assertIsNone(self.cache.get(key))
			with patch.object(furniture, "render_pdf", return_value=b"%PDF-1.4 fixture"):
				self._download("pdf")

	def test_pdf_character_cap_and_markdown_fallback(self):
		self._message(content="x" * (chat_export.MAX_PDF_CHARS + 1))
		with patch.object(furniture, "render_pdf") as render:
			with self.assertRaisesRegex(frappe.ValidationError, "too long for a PDF"):
				self._download("pdf")
		render.assert_not_called()
		self.assertIn("x" * 1000, self._markdown())

	def test_invalid_format_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Unknown format"):
			self._download("html")

	def test_busy_owner_refusals_are_429_and_do_not_use_quota(self):
		with redis_lock(_site_lock_name(f"export:{OWNER}"), timeout_s=90):
			for _ in range(6):
				with self.assertRaises(frappe.RateLimitExceededError) as caught:
					self._download()
				self.assertEqual(caught.exception.http_status_code, 429)
		self.assertIsNone(self.cache.get(self.cache.make_key(f"jarvis:export:rl:{OWNER}")))
		self.assertIn("No messages yet", self._markdown())

	def test_busy_pdf_slots_do_not_use_quota(self):
		with (
			redis_lock(_site_lock_name("export:pdf:slot:1"), timeout_s=90),
			redis_lock(_site_lock_name("export:pdf:slot:2"), timeout_s=90),
		):
			with self.assertRaises(frappe.RateLimitExceededError) as caught:
				self._download("pdf")
			self.assertEqual(caught.exception.http_status_code, 429)
		self.assertIsNone(self.cache.get(self.cache.make_key(f"jarvis:export:rl:{OWNER}")))

	def test_pdf_length_refusal_does_not_use_quota_or_snapshot_content(self):
		self._message(content="x" * (chat_export.MAX_PDF_CHARS + 1))
		with self.assertRaises(frappe.ValidationError) as caught:
			self._download("pdf")
		self.assertIsInstance(caught.exception, frappe.SecurityException)
		self.assertEqual(type(caught.exception).__name__, "_ExportRefusedError")
		self.assertIsNone(self.cache.get(self.cache.make_key(f"jarvis:export:rl:{OWNER}")))

	def test_quota_refusal_is_429_and_excludes_transcript_snapshots(self):
		for _ in range(5):
			self._download()
		with self.assertRaises(frappe.RateLimitExceededError) as caught:
			self._download()
		self.assertIsInstance(caught.exception, frappe.SecurityException)
		self.assertEqual(caught.exception.http_status_code, 429)

	def test_non_ascii_title_yields_safe_ascii_filename(self):
		frappe.db.set_value("Jarvis Conversation", self.conv.name, "title", "日本語 😀")
		name = self._download()["filename"]
		self.assertRegex(name, r"^jarvis-chat-\d{4}-\d{2}-\d{2}\.md$")

	def test_ascii_slug_is_bounded(self):
		frappe.db.set_value("Jarvis Conversation", self.conv.name, "title", "A " * 65)
		name = self._download()["filename"]
		slug = re.sub(r"-\d{4}-\d{2}-\d{2}\.md$", "", name)
		self.assertLessEqual(len(slug), 60)
		self.assertRegex(slug, r"^[a-z0-9-]+$")

	def test_export_creates_no_file_row(self):
		before = frappe.db.count("File")
		self._download()
		with patch.object(furniture, "render_pdf", return_value=b"%PDF-1.4 fixture"):
			self._download("pdf")
		self.assertEqual(frappe.db.count("File"), before)

	def test_pdf_failure_is_safe_deferred_and_releases_locks(self):
		self._message(content="TRANSCRIPT-LOG-SENTINEL")
		active_exceptions = []

		def safe_log(*args, **kwargs):
			active_exceptions.append(sys.exc_info())

		with patch.object(furniture, "render_pdf", side_effect=RuntimeError("RAW-RENDERER-SECRET")):
			with patch("frappe.log_error", side_effect=safe_log) as log:
				with self.assertRaisesRegex(
					frappe.ValidationError, "We couldn't create the PDF. Try Markdown instead"
				) as caught:
					self._download("pdf")
				from frappe.utils.error import log_error_snapshot

				log_error_snapshot(caught.exception)
				log.assert_called_once()
				self.assertEqual(
					active_exceptions, [(None, None, None)], "Sentry must not see the renderer exception"
				)
				self.assertTrue(log.call_args.kwargs["defer_insert"])
				logged = str(log.call_args)
				self.assertIn(self.conv.name, logged)
				self.assertIn("pdf", logged)
				self.assertNotIn("RAW-RENDERER-SECRET", logged)
				self.assertNotIn("TRANSCRIPT-LOG-SENTINEL", logged)
		for name in (f"export:{OWNER}", "export:pdf:slot:1", "export:pdf:slot:2"):
			self.assertIsNone(self.cache.get(_lock_key(name)))

	def test_actual_renderer_failure_produces_one_content_free_deferred_log(self):
		failure = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"SUBPROCESS-SECRET-SENTINEL")
		self._message(content="PDF-CONTENT-SENTINEL")
		with patch.object(furniture, "_resolve_binary", return_value="/fake/wkhtmltopdf"):
			with patch.object(furniture.subprocess, "run", return_value=failure):
				with patch("frappe.log_error") as log:
					with self.assertRaisesRegex(frappe.ValidationError, "Try Markdown instead"):
						self._download("pdf")
					log.assert_called_once()
					self.assertTrue(log.call_args.kwargs["defer_insert"])
					self.assertNotIn("SUBPROCESS-SECRET-SENTINEL", str(log.call_args))
					self.assertNotIn("PDF-CONTENT-SENTINEL", str(log.call_args))

	@unittest.skipUnless(shutil.which("wkhtmltopdf"), "wkhtmltopdf is not installed in this CI image")
	def test_real_pdf_has_pdf_header_and_safe_extractable_text(self):
		try:
			import pypdfium2
		except ImportError:
			self.skipTest("pypdfium2 is unavailable")
		self._security_fixture()
		self._message(content="REAL PDF SAFE CONTENT")
		response = self._download("pdf")
		self.assertTrue(response["filecontent"].startswith(b"%PDF-"))
		pdf = pypdfium2.PdfDocument(response["filecontent"])
		try:
			text = "\n".join(page.get_textpage().get_text_range() for page in pdf)
		finally:
			pdf.close()
		self.assertIn("REAL PDF SAFE CONTENT", text)
		self._assert_no_secrets(text)


class TestChatExportRateRecovery(unittest.TestCase):
	def test_lost_counter_acknowledgement_cannot_leave_a_permanent_quota(self):
		cache = frappe.cache()
		user = "export-lost-ack-" + frappe.generate_hash(length=12)
		key = cache.make_key("jarvis:export:rl:" + user)
		execute = cache.execute_command

		def lost_acknowledgement(command, *args, **kwargs):
			result = execute(command, *args, **kwargs)
			if command in {"INCR", "INCRBY", "EVAL"}:
				raise ConnectionError("Lost acknowledgement after the Redis mutation")
			return result

		try:
			with patch.object(cache, "execute_command", side_effect=lost_acknowledgement):
				with self.assertRaises(ConnectionError):
					chat_export._rate_limit(user)
			self.assertGreater(cache.ttl(key), 0)
			self.assertLessEqual(cache.ttl(key), 60)
		finally:
			cache.delete(key)

	def test_an_orphaned_counter_recovers_a_bounded_expiry(self):
		cache = frappe.cache()
		user = "export-orphan-" + frappe.generate_hash(length=12)
		key = cache.make_key("jarvis:export:rl:" + user)
		try:
			cache.set(key, 3)
			self.assertEqual(cache.ttl(key), -1)
			chat_export._rate_limit(user)
			self.assertEqual(int(cache.get(key)), 4)
			self.assertGreater(cache.ttl(key), 0)
			self.assertLessEqual(cache.ttl(key), 60)
		finally:
			cache.delete(key)


class TestChatExportRendererLogging(unittest.TestCase):
	def test_scoped_degradations_remain_visible_without_raw_brand_details(self):
		with (
			patch.object(furniture, "resolve_letterhead", return_value=("", "", None)),
			patch("frappe.db.exists", side_effect=RuntimeError("PRIVATE-BRAND-DETAIL")),
			patch.object(furniture, "render_pdf", return_value=b"%PDF-success"),
			patch("frappe.log_error") as log,
		):
			self.assertEqual(chat_export._pdf(frappe._dict(name="degraded"), "Visible"), b"%PDF-success")
		log.assert_called_once()
		metadata = json.loads(log.call_args.kwargs["message"])
		self.assertEqual(metadata["causes"], ["company_lookup_failed"])
		self.assertEqual(set(metadata), {"conversation", "format", "duration", "causes"})
		self.assertTrue(log.call_args.kwargs["defer_insert"])
		self.assertNotIn("PRIVATE-BRAND-DETAIL", str(log.call_args))

	def test_renderer_failures_have_distinct_fixed_cause_codes(self):
		cases = (
			("missing", None, None, "binary_missing"),
			("timeout", subprocess.TimeoutExpired("renderer", 60), None, "timeout"),
			(
				"exit",
				None,
				subprocess.CompletedProcess([], 5, stdout=b"", stderr=b"PRIVATE-STDERR"),
				"render_nonzero_exit",
			),
		)
		for kind, error, result, expected in cases:
			with self.subTest(kind=kind):
				with (
					patch.object(furniture, "resolve_brand", return_value={}),
					patch.object(furniture.pdfkit, "configuration", side_effect=OSError("PRIVATE-PATH"))
					if kind == "missing"
					else patch.object(furniture, "_resolve_binary", return_value="/fake/renderer"),
					patch.object(furniture.shutil, "which", return_value=None),
					patch.object(furniture.subprocess, "run", side_effect=error, return_value=result),
					patch("frappe.log_error") as log,
				):
					with self.assertRaises(chat_export._ExportRenderError):
						chat_export._pdf(frappe._dict(name="fixed-causes"), "Visible transcript")
				log.assert_called_once()
				self.assertEqual(json.loads(log.call_args.kwargs["message"])["causes"], [expected])
				self.assertNotIn("PRIVATE-", str(log.call_args))

	def test_failed_error_log_cannot_replace_safe_pdf_failure(self):
		with (
			patch.object(furniture, "resolve_brand", return_value={}),
			patch.object(furniture, "render_pdf", side_effect=RuntimeError("RAW-RENDERER-SECRET")),
			patch("frappe.log_error", side_effect=RuntimeError("RAW-LOGGER-SECRET")) as log,
			patch("frappe.throw", side_effect=chat_export._ExportRenderError("Safe export failure")) as fail,
		):
			with self.assertRaises(chat_export._ExportRenderError) as caught:
				chat_export._pdf(frappe._dict(name="log-fixture"), "# Transcript")
			self.assertIsNone(caught.exception.__context__)
			log.assert_called_once()
			self.assertEqual(
				fail.call_args.args,
				("We couldn't create the PDF. Try Markdown instead.", chat_export._ExportRenderError),
			)

	def test_pdf_has_one_transcript_title(self):
		with (
			patch.object(furniture, "resolve_brand", return_value={}),
			patch.object(furniture, "render_pdf", return_value=b"%PDF-fixture") as render,
		):
			chat_export._pdf(
				frappe._dict(name="title-fixture", title="Unique transcript title"),
				"# Unique transcript title\n\nBody",
			)
		self.assertEqual(render.call_args.args[0].count("Unique transcript title"), 1)

	def test_default_renderer_logging_is_unchanged(self):
		failure = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"RENDERER-DETAIL")
		with patch.object(furniture, "_resolve_binary", return_value="/fake/wkhtmltopdf"):
			with patch.object(furniture.subprocess, "run", return_value=failure):
				with patch("frappe.log_error") as log:
					with self.assertRaises(InvalidArgumentError):
						furniture.render_pdf("<p>Safe</p>")
					log.assert_called_once()

	def test_export_context_suppresses_raw_renderer_log_and_resets_on_exception(self):
		failure = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"RENDERER-DETAIL")
		with patch.object(furniture, "_resolve_binary", return_value="/fake/wkhtmltopdf"):
			with patch.object(furniture.subprocess, "run", return_value=failure):
				with patch("frappe.log_error") as log:
					with self.assertRaises(InvalidArgumentError):
						with furniture.defer_failure_logging():
							furniture.render_pdf("<p>Safe</p>")
					log.assert_not_called()
					with self.assertRaises(InvalidArgumentError):
						furniture.render_pdf("<p>Safe</p>")
					log.assert_called_once()
