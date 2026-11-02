"""The audit classifier's archive lookups are equality seeks on
(deleted_doctype, deleted_name): one composite index on the core Deleted Document
table, created by one idempotent helper from a post-model-sync patch (upgraded
sites) and after_install (fresh sites, whose patches are only marked complete).

No fixture transaction: adding an index is DDL with an implicit commit. The tests
never drop or recreate it; a second call must be a no-op.
"""

from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import approval_reviewers

INDEX = "jarvis_deleted_doctype_name"


def index_columns():
	rows = frappe.db.sql("SHOW INDEX FROM `tabDeleted Document` WHERE Key_name = %s", INDEX, as_dict=True)
	return [(r.Seq_in_index, r.Column_name, r.Non_unique) for r in sorted(rows, key=lambda r: r.Seq_in_index)]


EXPECTED = [(1, "deleted_doctype", 1), (2, "deleted_name", 1)]


class TestAuditLookupIndex(FrappeTestCase):
	def test_helper_creates_the_index_once_and_is_idempotent(self):
		"""I1."""
		approval_reviewers.ensure_audit_lookup_index()
		self.assertEqual(index_columns(), EXPECTED)
		approval_reviewers.ensure_audit_lookup_index()
		self.assertEqual(index_columns(), EXPECTED)

	def test_upgrade_patch_runs_after_the_reviewer_patch_and_uses_the_helper(self):
		"""I2."""
		from jarvis.patches import v2_29_deleted_document_audit_index as patch_module

		lines = [
			line.strip()
			for line in (Path(frappe.get_app_path("jarvis")) / "patches.txt").read_text().splitlines()
			if line.strip()
		]
		post = lines[lines.index("[post_model_sync]") :]
		reviewer = post.index("jarvis.patches.v2_29_chat_approval_reviewers")
		self.assertEqual(post[reviewer + 1], "jarvis.patches.v2_29_deleted_document_audit_index")
		with patch.object(approval_reviewers, "ensure_audit_lookup_index") as ensure:
			patch_module.execute()
		ensure.assert_called_once_with()

	def test_a_fresh_install_creates_it(self):
		"""install_app marks every patch complete without running it."""
		from jarvis import install

		with (
			patch.object(install, "seed_roles_and_settings"),
			patch("jarvis.chat.agent_catalog.sync_agent_listings"),
			patch("jarvis.catalog_store.enqueue_refresh_all"),
			patch.object(approval_reviewers, "ensure_audit_lookup_index") as ensure,
		):
			install.after_install()
		ensure.assert_called_once_with()

	def test_the_index_survives_a_core_deleted_document_schema_sync(self):
		"""I3: schema sync drops only single-column indexes it does not declare."""
		approval_reviewers.ensure_audit_lookup_index()
		frappe.reload_doc("core", "doctype", "deleted_document", force=True)
		frappe.db.commit()
		self.assertEqual(index_columns(), EXPECTED)
