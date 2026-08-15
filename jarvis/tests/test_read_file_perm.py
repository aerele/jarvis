"""Regression tests for F12 (read_file / get_file_pages info-disclosure) -
see .superpowers/sdd/audit-findings.md.

``_resolve_file``'s filename-based lookup used ``frappe.get_all`` (a
permission bypass) with a substring LIKE fallback ordered by
``creation desc``, so a low-privilege caller searching by a generic
filename could have the globally most-recent name-matching File selected
for them even when it is a private file they have no access to - and the
subsequent permission-denied message then echoed that file's real
``file_name`` back to the caller, confirming the existence of a private
document they had no other way to see.

Needs DB (File doctype); FrappeTestCase rolls back per-test inserts, so
the File fixtures created inside each test method need no manual cleanup
(mirrors the pattern in test_untrusted_fence.py / test_vision_attachments.py).
"""

from __future__ import annotations

import contextlib

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.get_file_pages import get_file_pages
from jarvis.tools.read_file import read_file

USER_CALLER = "jpl-readfile-caller@example.com"
USER_OTHER = "jpl-readfile-other@example.com"


def _ensure_user(email: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _make_file(name: str, content: bytes, owner: str):
	"""Insert a private, unattached File owned by ``owner`` - File's own
	permission model (frappe.core.doctype.file.file.has_permission) grants
	read on an unattached private file only to its owner (or
	Administrator), so this is the simplest fixture that reproduces a
	"private file the caller cannot read" without needing a whole
	permission-scoped target doctype."""
	from frappe.utils.file_manager import save_file

	with _as(owner):
		return save_file(name, content, None, None, is_private=1)


class ReadFilePermTestCase(FrappeTestCase):
	"""Shared fixture for the F12 permission-path tests."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_user(USER_CALLER)
		_ensure_user(USER_OTHER)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()


class TestResolveFileByFilenamePermissionAware(ReadFilePermTestCase):
	"""F12: filename-based fuzzy resolution must not pick a file the
	caller can't read, and must not leak that file's name on denial.

	Note: FrappeTestCase (IntegrationTestCase) only rolls back at CLASS
	teardown, not between individual test methods - so each test method
	below uses its own unique substring token to avoid one test's fixture
	files being visible to (and satisfying) another test's fuzzy search.
	"""

	def test_fuzzy_match_skips_unreadable_newer_file_and_finds_own_older_one(self):
		# caller's own (older) file, matches the "invoicealpha" substring
		mine = _make_file("my-invoicealpha-draft.txt", b"my own draft", USER_CALLER)
		# someone else's newer, unrelated, PRIVATE file that also matches -
		# under the old bug ("most recent" wins, zero permission filter)
		# this one would have been picked instead.
		_make_file("confidential-invoicealpha-CEO-bonus.txt", b"secret bonus data", USER_OTHER)

		with _as(USER_CALLER):
			out = read_file(filename="invoicealpha")
		self.assertEqual(out["filename"], mine.file_name)

	def test_fuzzy_match_with_no_readable_candidate_is_generic_and_does_not_leak_name(self):
		_make_file("confidential-invoicebeta-CEO-bonus.txt", b"secret bonus data", USER_OTHER)

		with _as(USER_CALLER), self.assertRaises(InvalidArgumentError) as ctx:
			read_file(filename="invoicebeta")
		message = str(ctx.exception)
		self.assertNotIn("confidential-invoicebeta-CEO-bonus", message)
		self.assertIn("no matching file found", message)

	def test_exact_file_url_denial_is_generic_and_does_not_leak_name(self):
		theirs = _make_file("their-private-report.txt", b"secret", USER_OTHER)

		with _as(USER_CALLER), self.assertRaises(PermissionDeniedError) as ctx:
			read_file(file_url=theirs.file_url)
		message = str(ctx.exception)
		self.assertNotIn("their-private-report", message)
		self.assertIn("no matching file found", message)

	def test_owner_still_reads_own_file_by_filename(self):
		# Frappe hash-suffixes file_name on collision (a File named
		# "owner-report.txt" may already exist earlier in the full suite),
		# so search by the ACTUAL stored name, not the requested one.
		f = _make_file("owner-report.txt", b"hello owner", USER_CALLER)
		with _as(USER_CALLER):
			out = read_file(filename=f.file_name)
		self.assertEqual(out["text"], "hello owner")

	def test_owner_still_reads_own_file_by_url(self):
		mine = _make_file("owner-report-2.txt", b"hello again", USER_CALLER)
		with _as(USER_CALLER):
			out = read_file(file_url=mine.file_url)
		self.assertEqual(out["text"], "hello again")


class TestGetFilePagesPermissionErrorGeneric(ReadFilePermTestCase):
	"""Same permission-denial generic-message fix, applied to
	get_file_pages (shares read_file._resolve_file). The permission check
	runs before any PDF/image parsing, so a plain .txt fixture is enough -
	no need for real PDF bytes to exercise the denial path."""

	def test_exact_file_url_denial_is_generic_and_does_not_leak_name(self):
		theirs = _make_file("their-private-scan.txt", b"not a real scan", USER_OTHER)

		with _as(USER_CALLER), self.assertRaises(PermissionDeniedError) as ctx:
			get_file_pages(file_url=theirs.file_url)
		message = str(ctx.exception)
		self.assertNotIn("their-private-scan", message)
		self.assertIn("no matching file found", message)

	def test_fuzzy_match_with_no_readable_candidate_is_generic_and_does_not_leak_name(self):
		_make_file("confidential-scangamma-CEO.txt", b"not a real scan", USER_OTHER)

		with _as(USER_CALLER), self.assertRaises(InvalidArgumentError) as ctx:
			get_file_pages(filename="scangamma")
		message = str(ctx.exception)
		self.assertNotIn("confidential-scangamma-CEO", message)
		self.assertIn("no matching file found", message)
