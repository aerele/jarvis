"""The desktop shell tells the SPA whether the viewer reviews skills, so only
reviewers poll the reviewer-guarded pending-review count behind the sidebar
Skills badge."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.www import jarvis as www_desktop


class TestWwwSkillReviewerBoot(FrappeTestCase):
	def _boot(self, reviewer: bool) -> dict:
		ctx = frappe._dict()
		with (
			patch.object(www_desktop, "has_jarvis_access", return_value=True),
			patch.object(www_desktop, "has_jarvis_admin_access", return_value=False),
			patch.object(www_desktop, "is_skill_reviewer", return_value=reviewer),
			patch.object(www_desktop, "support_scope", return_value=None),
			patch.object(www_desktop, "_support_state", return_value=www_desktop.SUPPORT_OFF),
		):
			www_desktop.get_context(ctx)
		return ctx.boot

	def test_reviewer_flag_true(self):
		self.assertIs(self._boot(True)["is_skill_reviewer"], True)

	def test_reviewer_flag_false(self):
		self.assertIs(self._boot(False)["is_skill_reviewer"], False)
