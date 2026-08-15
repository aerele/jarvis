"""Tests for the api._run_tool cross-cutting additions: preview (a write
executed in a rolled-back savepoint, nothing committed) and write auditing
(every mutating tool is audited; reads are not)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api, audit


# These exercise the model-facing preview BRANCH in _run_tool. Every real
# _PREVIEWABLE tool is also in _GATED_WRITES, and gated tools now bypass the
# preview branch entirely (they park for confirmation - see Fix 1 in
# test_confirm_gate). So to keep testing the preview-branch mechanics we patch
# _GATED_WRITES to empty, which stands in for a hypothetical non-gated
# previewable tool - the only case that still reaches this branch.
class TestPreview(FrappeTestCase):
	def test_preview_create_does_not_persist(self):
		desc = "jarvis-test-preview-no-persist-001"
		with patch.object(api, "_GATED_WRITES", frozenset()):
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "ToDo",
					"values": {"description": desc},
					"preview": True,
				},
			)
		self.assertTrue(r["ok"])
		self.assertTrue(r["data"]["preview"])
		self.assertIn("would", r["data"])
		# savepoint was rolled back -> nothing committed
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))

	def test_preview_rejected_for_non_previewable_tool(self):
		r = api._run_tool("get_list", {"doctype": "ToDo", "preview": True})
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "InvalidArgumentError")


class TestWriteAudit(FrappeTestCase):
	# NOTE: these exercise the inline write-audit seam with a NON-gated write
	# (add_comment). The gated writes in _GATED_WRITES (create_doc etc.) no
	# longer execute inline - they park for confirmation and are audited only
	# when confirm_tool runs them; that path is covered in test_confirm_gate.
	def test_successful_write_is_audited(self):
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "jarvis-test-audit-target",
			}
		).insert(ignore_permissions=True)
		with patch("jarvis.api.audit.record") as rec:
			r = api._run_tool(
				"add_comment",
				{
					"doctype": "ToDo",
					"name": todo.name,
					"content": "audited note",
				},
			)
		self.assertTrue(r["ok"])
		self.assertTrue(rec.called)
		self.assertEqual(rec.call_args.kwargs["tool"], "add_comment")
		self.assertTrue(rec.call_args.kwargs["ok"])

	def test_failed_write_is_audited_as_error(self):
		with patch("jarvis.api.audit.record") as rec:
			r = api._run_tool(
				"add_comment",
				{
					"doctype": "ToDo",
					"name": "no-such-todo-zzz",
					"content": "x",
				},
			)
		self.assertFalse(r["ok"])
		self.assertTrue(rec.called)
		self.assertFalse(rec.call_args.kwargs["ok"])

	def test_read_is_not_audited(self):
		with patch("jarvis.api.audit.record") as rec:
			api._run_tool("get_list", {"doctype": "ToDo", "limit": 1})
		self.assertFalse(rec.called)

	def test_preview_error_is_not_audited(self):
		# A failed dry-run must not pollute the audit log with a phantom write.
		with patch("jarvis.api.audit.record") as rec, patch.object(api, "_GATED_WRITES", frozenset()):
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "No Such DocType ZZZ",
					"values": {},
					"preview": True,
				},
			)
		self.assertFalse(r["ok"])
		self.assertFalse(rec.called)

	def test_download_pdf_write_is_audited(self):
		# F25: download_pdf inserts a new File doc (and attaches it to the
		# source record) but was absent from _WRITE_TOOLS.
		with (
			patch("jarvis.api.dispatch", return_value={"file_url": "/private/files/x.pdf"}),
			patch("jarvis.api.audit.record") as rec,
		):
			r = api._run_tool("download_pdf", {"doctype": "ToDo", "name": "x"})
		self.assertTrue(r["ok"])
		self.assertTrue(rec.called)
		self.assertEqual(rec.call_args.kwargs["tool"], "download_pdf")
		self.assertTrue(rec.call_args.kwargs["ok"])

	def test_export_excel_write_is_audited(self):
		# F25: export_excel inserts a new File doc but was absent from
		# _WRITE_TOOLS.
		with (
			patch("jarvis.api.dispatch", return_value={"file_url": "/private/files/x.xlsx"}),
			patch("jarvis.api.audit.record") as rec,
		):
			r = api._run_tool("export_excel", {"doctype": "ToDo"})
		self.assertTrue(r["ok"])
		self.assertTrue(rec.called)
		self.assertEqual(rec.call_args.kwargs["tool"], "export_excel")
		self.assertTrue(rec.call_args.kwargs["ok"])

	def test_download_pdf_export_excel_are_write_but_not_gated(self):
		# These need auditing (real DB writes), not a confirmation card - not
		# user-facing mutations the model should have to get a human click
		# for. NOT in _GATED_WRITES/_AUTO_APPLYABLE.
		for tool in ("download_pdf", "export_excel"):
			self.assertIn(tool, api._WRITE_TOOLS)
			self.assertNotIn(tool, api._GATED_WRITES)
			self.assertNotIn(tool, api._AUTO_APPLYABLE)

	def test_preview_sandboxes_a_tool_that_commits(self):
		# The core fix: even when the dispatched tool calls frappe.db.commit()
		# internally, preview must not persist anything.
		sentinel = "jarvis-test-preview-commit-sentinel"

		def fake_dispatch(tool, args):
			frappe.get_doc({"doctype": "ToDo", "description": sentinel}).insert(ignore_permissions=True)
			frappe.db.commit()  # would persist + destroy a naive savepoint
			return {"ok": True}

		with (
			patch("jarvis.api.dispatch", side_effect=fake_dispatch),
			patch.object(api, "_GATED_WRITES", frozenset()),
		):
			r = api._run_tool("run_method", {"method": "x", "preview": True})
		self.assertTrue(r["ok"])
		self.assertTrue(r["data"]["preview"])
		self.assertFalse(frappe.db.exists("ToDo", {"description": sentinel}))


class TestAuditRedaction(FrappeTestCase):
	"""F6: _scrub only redacted keys EXACTLY equal to a fixed literal set, so
	compound secret field names (llm_api_key, agent_token, smtp_password...)
	were logged in plaintext. Fixed via substring match + per-doctype
	Password-fieldtype lookup (frappe.get_meta(doctype).get_password_fields())."""

	def test_compound_secret_keys_are_redacted(self):
		# Substring match: none of these equal a literal in the old fixed set,
		# but each contains "key"/"token"/"password".
		scrubbed = audit._scrub(
			{
				"llm_api_key": "sk-super-secret",
				"agent_token": "tok-abc123",
				"smtp_password": "hunter2",
				"ordinary_field": "keep me",
				"description": "not a secret",
			}
		)
		self.assertEqual(scrubbed["llm_api_key"], "[REDACTED]")
		self.assertEqual(scrubbed["agent_token"], "[REDACTED]")
		self.assertEqual(scrubbed["smtp_password"], "[REDACTED]")
		self.assertEqual(scrubbed["ordinary_field"], "keep me")
		self.assertEqual(scrubbed["description"], "not a secret")

	def test_nested_and_list_secrets_are_redacted(self):
		scrubbed = audit._scrub(
			{
				"changes": {"jarvis_admin_api_secret": "x", "title": "y"},
				"users": [{"webhook_secret": "z", "name": "ok"}],
			}
		)
		self.assertEqual(scrubbed["changes"]["jarvis_admin_api_secret"], "[REDACTED]")
		self.assertEqual(scrubbed["changes"]["title"], "y")
		self.assertEqual(scrubbed["users"][0]["webhook_secret"], "[REDACTED]")
		self.assertEqual(scrubbed["users"][0]["name"], "ok")

	def test_record_redacts_update_doc_changes_for_real_doctype(self):
		# End-to-end through record(): args shaped like a real update_doc call
		# against Jarvis Settings, whose Password-fieldtype fields include
		# llm_api_key/agent_token/smtp_password-style compound names.
		captured = {}

		class _FakeLogger:
			def info(self, msg):
				captured["msg"] = msg

			def error(self, msg):
				captured["err"] = msg

		with patch("frappe.logger", return_value=_FakeLogger()):
			audit.record(
				tool="update_doc",
				args={
					"doctype": "Jarvis Settings",
					"name": "Jarvis Settings",
					"changes": {
						"llm_api_key": "sk-live-should-not-leak",
						"agent_token": "tok-should-not-leak",
						"default_model": "gpt-5",
					},
				},
				ok=True,
			)
		self.assertIn("msg", captured)
		logged = captured["msg"]
		self.assertNotIn("sk-live-should-not-leak", logged)
		self.assertNotIn("tok-should-not-leak", logged)
		self.assertIn("[REDACTED]", logged)
		self.assertIn("gpt-5", logged)  # ordinary field passes through

	def test_password_fields_includes_child_table_password_fields(self):
		# F6 completion: _password_fields previously called the nonexistent
		# frappe.get_meta(doctype).get_password_fields(), which raised
		# AttributeError, was swallowed by a bare except, and returned an
		# empty set on every call - the whole meta-lookup redaction layer was
		# dead. Real Password fields whose names don't contain a
		# _SECRET_KEYS substring (subscription_accounts on the child doctype
		# Jarvis LLM Pool Model, wired via the models Table field on Jarvis
		# Settings) must be discovered.
		fields = audit._password_fields("Jarvis Settings")
		self.assertTrue(fields)  # proves the meta layer isn't dead
		self.assertIn("subscription_accounts", fields)

	def test_record_redacts_child_table_password_field_not_caught_by_substring(self):
		# End-to-end through record(): subscription_accounts is a Password
		# field on the Jarvis LLM Pool Model child doctype (reached via the
		# models Table field on Jarvis Settings) and contains none of
		# password/pwd/secret/token/key, so this only passes if the meta
		# layer (not the substring layer) is doing real work.
		captured = {}

		class _FakeLogger:
			def info(self, msg):
				captured["msg"] = msg

			def error(self, msg):
				captured["err"] = msg

		with patch("frappe.logger", return_value=_FakeLogger()):
			audit.record(
				tool="update_doc",
				args={
					"doctype": "Jarvis Settings",
					"name": "Jarvis Settings",
					"changes": {
						"models": [
							{
								"provider": "openai",
								"subscription_accounts": "SECRET-OAUTH-BLOB",
							}
						],
					},
				},
				ok=True,
			)
		self.assertIn("msg", captured)
		logged = captured["msg"]
		self.assertNotIn("SECRET-OAUTH-BLOB", logged)
		self.assertIn("[REDACTED]", logged)
		self.assertIn("openai", logged)  # ordinary field passes through
