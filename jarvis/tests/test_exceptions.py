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

	def test_carry_message(self):
		e = ToolNotFoundError("no such tool: foo")
		self.assertEqual(str(e), "no such tool: foo")
