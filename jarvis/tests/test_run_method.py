"""Tests for jarvis.tools.run_method - classify-and-dispatch between a
whitelisted Python method (dotted path) and a Server Script API method
(bare api_method name), plus the Jarvis Settings blocklist and input /
permission errors. Runs against real Frappe (the whitelist gate and the
server-script map are the real security boundary; exercising them for real
is more meaningful than mocking them)."""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.run_method import run_method

# An API-type Server Script created once for the whole class. Its api_method
# is a bare name (no dots), exactly what run_method must recognise as a
# server script rather than a Python dotted path.
_API_METHOD = "jarvis_test_say_my_name"
_SCRIPT = "frappe.response['message'] = 'name is ' + (frappe.form_dict.get('who') or 'nobody')"

# A second API script that returns via frappe.flags instead of frappe.response,
# to cover run_method's flags-return branch.
_FLAGS_METHOD = "jarvis_test_flags_out"
_FLAGS_SCRIPT = "frappe.flags['result'] = 'via flags: ' + (frappe.form_dict.get('who') or 'nobody')"


def _set_blocklist(value: str) -> None:
	settings = frappe.get_single("Jarvis Settings")
	settings.run_method_blocklist = value
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_document_cache("Jarvis Settings")


class TestRunMethod(FrappeTestCase):
	@staticmethod
	def _ensure_api_script(name: str, script: str) -> None:
		if not frappe.db.exists("Server Script", name):
			frappe.get_doc(
				{
					"doctype": "Server Script",
					"name": name,
					"script_type": "API",
					"api_method": name,
					"allow_guest": 0,
					"script": script,
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._ensure_api_script(_API_METHOD, _SCRIPT)
		cls._ensure_api_script(_FLAGS_METHOD, _FLAGS_SCRIPT)

	@classmethod
	def tearDownClass(cls):
		for name in (_API_METHOD, _FLAGS_METHOD):
			if frappe.db.exists("Server Script", name):
				frappe.delete_doc("Server Script", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		_set_blocklist("")

	def tearDown(self):
		_set_blocklist("")

	# --- whitelisted Python method branch (unchanged behaviour) ---

	def test_calls_whitelisted_method(self):
		self.assertEqual(run_method("frappe.ping"), "pong")

	def test_blocks_resolvable_but_non_whitelisted_method(self):
		# frappe.get_all / frappe.delete_doc resolve but are NOT @whitelist'd.
		with self.assertRaises(PermissionDeniedError):
			run_method("frappe.get_all", {"doctype": "ToDo"})
		with self.assertRaises(PermissionDeniedError):
			run_method("frappe.delete_doc", {"doctype": "ToDo", "name": "x"})

	def test_unknown_dotted_method_raises(self):
		with self.assertRaises(InvalidArgumentError):
			run_method("nope.not.a.real.method")

	def test_unknown_bare_name_raises(self):
		# A bare name that is neither a server-script api_method nor an
		# importable attribute is still an unknown method.
		with self.assertRaises(InvalidArgumentError):
			run_method("definitely_not_a_server_script_or_method")

	def test_blank_method_raises(self):
		with self.assertRaises(InvalidArgumentError):
			run_method("")

	def test_non_dict_args_raises(self):
		with self.assertRaises(InvalidArgumentError):
			run_method("frappe.ping", args="not-a-dict")

	def test_unknown_arg_is_rejected(self):
		# A mistyped/extra arg must error, not be silently dropped by frappe.call.
		with self.assertRaises(InvalidArgumentError):
			run_method("frappe.ping", {"bogus_arg": 1})

	# --- Server Script API branch (classified via the server-script map) ---

	def test_calls_server_script_api(self):
		self.assertEqual(run_method(_API_METHOD), {"message": "name is nobody"})

	def test_server_script_receives_args(self):
		# args are injected into frappe.form_dict, which the script reads.
		self.assertEqual(run_method(_API_METHOD, {"who": "navin"}), {"message": "name is navin"})

	def test_server_script_returns_flags(self):
		# Covers the flags-return branch: a script that sets frappe.flags rather
		# than frappe.response['message'].
		self.assertEqual(run_method(_FLAGS_METHOD, {"who": "navin"}), {"result": "via flags: navin"})

	def test_form_dict_restored_after_server_script(self):
		frappe.local.form_dict = frappe._dict({"sentinel": "keep"})
		try:
			run_method(_API_METHOD, {"who": "x"})
			self.assertEqual(frappe.local.form_dict.get("sentinel"), "keep")
			self.assertNotIn("who", frappe.local.form_dict)
		finally:
			frappe.local.form_dict = frappe._dict({})

	# --- blocklist (Jarvis Settings.run_method_blocklist), fnmatch, fail-open ---

	def test_blocklist_blocks_python_method(self):
		_set_blocklist("frappe.*")
		with self.assertRaises(PermissionDeniedError):
			run_method("frappe.ping")

	def test_blocklist_blocks_server_script(self):
		_set_blocklist(_API_METHOD)
		with self.assertRaises(PermissionDeniedError):
			run_method(_API_METHOD)

	def test_blocklist_fnmatch_wildcard_matches(self):
		_set_blocklist("*ping")
		with self.assertRaises(PermissionDeniedError):
			run_method("frappe.ping")

	def test_blocklist_unmatched_pattern_allows(self):
		_set_blocklist("erpnext.*\n*.delete_doc")
		self.assertEqual(run_method("frappe.ping"), "pong")

	def test_empty_blocklist_blocks_nothing(self):
		# Fail-open: an empty blocklist blocks nothing.
		_set_blocklist("")
		self.assertEqual(run_method("frappe.ping"), "pong")
