"""A replaced storage reader must not poison later raw-byte capability checks."""

import unittest
from unittest.mock import patch

from jarvis import compat


class TestFileReaderReplacement(unittest.TestCase):
	def test_binary_stays_exact_after_a_reader_without_encoding_options(self):
		payload = b"\x1f\x8b\x08\x00"

		class Reader:
			def get_content(self, encodings=None):
				return payload if encodings == [] else payload.decode("windows-1252")

		with patch.object(Reader, "get_content", lambda self: payload):
			self.assertEqual(compat.file_bytes(Reader()), payload)
		# Storage hooks and test wrappers can restore a different method on the
		# same class. A cached False would now choose locale text decoding.
		self.assertEqual(compat.file_bytes(Reader()), payload)
