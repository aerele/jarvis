"""The not-onboarded nudge must wait for the SITE to be set up.

Jarvis operates the customer's ERP. On a site with no Company there is nothing
for it to operate, so the desk nudge would land on top of ERPNext's own setup
wizard - competing for the same person's attention and pointing at a Jarvis
wizard whose premise (a company to run) does not exist yet.

``set_jarvis_boot`` publishes ``jarvis_site_setup_complete`` and
jarvis_onboarding_banner.bundle.js gates on it. These cover the boot half; the
banner reads it with a strict ``=== false`` so an older payload without the key
behaves exactly as before.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.boot import set_jarvis_boot


class TestBootSiteSetupFlag(FrappeTestCase):
	def _boot(self):
		b = frappe._dict()
		set_jarvis_boot(b)
		return b

	def test_a_site_with_a_company_is_set_up(self):
		with patch("frappe.db.count", return_value=3):
			self.assertTrue(self._boot().jarvis_site_setup_complete)

	def test_a_site_with_no_company_is_not(self):
		"""The case this exists for: a fresh install mid-ERPNext-setup."""
		with patch("frappe.db.count", return_value=0):
			self.assertFalse(self._boot().jarvis_site_setup_complete)

	def test_no_company_doctype_is_treated_as_not_set_up(self):
		"""erpnext is not a hard dependency of jarvis, so the doctype may be
		absent entirely. Counting it would raise; the guard must short-circuit."""
		with patch("frappe.db.exists", return_value=False):
			self.assertFalse(self._boot().jarvis_site_setup_complete)

	def test_a_boot_error_stays_quiet(self):
		"""Matches the jarvis_onboarded fail-safe directly above it: when in
		doubt, do not nag. A nudge that finishing setup cannot dismiss is worse
		than a missing one."""
		with patch("frappe.db.exists", side_effect=Exception("boom")):
			self.assertFalse(self._boot().jarvis_site_setup_complete)

	def test_the_flag_is_independent_of_onboarding_state(self):
		"""Two separate questions: is the SITE ready, and has JARVIS been set up.
		The banner needs both, so neither may stand in for the other."""
		with (
			patch("frappe.db.count", return_value=1),
			patch("jarvis.account.is_ready_for_chat", return_value={"ready": False}),
		):
			b = self._boot()
		self.assertTrue(b.jarvis_site_setup_complete)
		self.assertFalse(b.jarvis_onboarded)
