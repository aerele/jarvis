"""after_install must seed the Agents Marketplace catalog.

Regression for the fresh-onboard gap: ``Jarvis Agent Listing`` was seeded ONLY
by the ``after_migrate`` hook, which ``install_app`` never runs, so a newly
onboarded site showed an empty Agents section until its first later migrate.
``jarvis.install.after_install`` now seeds the catalog too (and fails loudly if
the bundled registry is missing).

``after_install`` commits, so these tests cannot rely on the FrappeTestCase
transaction rollback for the catalog table -- they restore it explicitly in
tearDown by re-running the real sync.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import install
from jarvis.chat import agent_catalog

LISTING = "Jarvis Agent Listing"
ALLOWED_ROLE = "Jarvis Agent Allowed Role"


class TestInstallSeedsCatalog(FrappeTestCase):
	def tearDown(self):
		# after_install commits, so restore the catalog to the bundled registry
		# regardless of what each test left behind.
		agent_catalog.sync_agent_listings()
		frappe.db.commit()

	def _clear_catalog(self):
		frappe.db.delete(ALLOWED_ROLE, {"parenttype": LISTING})
		frappe.db.delete(LISTING)
		frappe.db.commit()

	def test_after_install_seeds_the_catalog(self):
		"""On an empty catalog (a fresh install), after_install populates one
		listing per bundled registry agent."""
		self._clear_catalog()
		self.assertEqual(frappe.db.count(LISTING), 0)

		install.after_install()

		expected = len(agent_catalog._load_registry().get("agents") or [])
		self.assertGreater(expected, 0, "the bundled registry must ship at least one agent")
		self.assertEqual(frappe.db.count(LISTING), expected)

	def test_after_install_fails_loudly_on_empty_registry(self):
		"""A missing/empty bundle leaves the catalog empty; after_install must
		throw rather than let the site land with a permanently empty catalog."""
		self._clear_catalog()

		with patch.object(agent_catalog, "_load_registry", return_value={"agents": []}):
			with self.assertRaises(frappe.ValidationError):
				install.after_install()
