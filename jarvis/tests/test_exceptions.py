from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import (
	InvalidArgumentError,
	JarvisError,
	PermissionDeniedError,
	ToolNotFoundError,
)


class TestExceptions(FrappeTestCase):
	def test_all_inherit_from_jarvis_error(self):
		for exc in (ToolNotFoundError, PermissionDeniedError, InvalidArgumentError):
			self.assertTrue(issubclass(exc, JarvisError))
