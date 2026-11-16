"""TEMPORARY CI diagnostic (remove with the fix). The name sorts after the last ``test_filebox_*``
file and before ``test_flag_matrix``, so its snapshot is the state those watchdog tests start from."""

from frappe.tests.utils import FrappeTestCase

from jarvis.tests import _draftcard_diag


class TestDraftCardDiagBeforeFlagMatrix(FrappeTestCase):
	def test_print_state(self):
		_draftcard_diag.snapshot("before-flag-matrix")
