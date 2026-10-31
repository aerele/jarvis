import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools import _file_source
from jarvis.tools.query_file import query_file


class TestFileSourcePermissions(unittest.TestCase):
	def test_url_lookup_uses_permission_aware_listing_and_one_denial(self):
		url = "/private/files/source.csv"
		for matches in ([], ["A", "B"], ["A"]):
			with self.subTest(matches=matches), (
				patch.object(frappe, "get_list", return_value=matches) as listing,
				patch.object(frappe, "get_all") as unscoped,
				patch.object(frappe, "get_cached_doc", return_value=SimpleNamespace(
					name="A", file_name="source.csv", file_url=url, attached_to_doctype=None,
				)),
				patch.object(frappe, "has_permission", return_value=False),
			):
				with self.assertRaisesRegex(PermissionDeniedError, "^no matching file found$"):
					query_file(file_url=url)
				listing.assert_called_once_with("File", filters={"file_url": url}, pluck="name", limit_page_length=2)
				unscoped.assert_not_called()

	def test_list_denial_does_not_reveal_file_existence(self):
		with patch.object(frappe, "get_list", side_effect=frappe.PermissionError), self.assertRaisesRegex(
			PermissionDeniedError, "^no matching file found$"
		):
			_file_source.resolve_file(file_url="/private/files/source.csv")
