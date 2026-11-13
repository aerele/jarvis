"""The backend receipt parser must honour the shared source contract (fleet-agent PR-0 twin)."""

import hashlib
import json
import unittest
from pathlib import Path

from jarvis.chat.source_receipts import REFERENCE_FIELDS, SOURCE_TOOLS, source_receipt

FIXTURE = Path(__file__).parent / "fixtures" / "source-contract-v1.json"
BYTE_SHA256 = "bd12b22aaa684e8fc65e64543498f1d080989841e996d80ce7924cb8f8e30de8"
SEMANTIC_DIGEST = "0e76718f09b9e549712ac7c0b814e73bb0631d7816c3d24be5e378b67fe3268c"
SEMANTIC_KEYS = (
	"contract",
	"version",
	"marker",
	"marker_placement",
	"retention",
	"requery_required",
	"source_tools",
	"bridged_prefix",
	"reference_keys",
	"reference_list_keys",
	"allowed_non_reference_keys",
)
LIST_KEYS = ("members", "selected_members", "sheets", "worksheet_scope")


def _semantic_digest(contract):
	subset = {key: contract[key] for key in SEMANTIC_KEYS}
	text = json.dumps(subset, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
	return hashlib.sha256(text.encode("ascii")).hexdigest()


def _references(obj, contract):
	"""Reference keys of one object; an empty list equals an absent key (comparison rule)."""
	keys = set(contract["reference_keys"]) | set(contract["reference_list_keys"])
	return {k: v for k, v in obj.items() if k in keys and v != []}


class TestSourceReceiptContract(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.raw = FIXTURE.read_bytes()
		cls.contract = json.loads(cls.raw)

	def test_vendored_fixture_is_pinned(self):
		self.assertEqual(hashlib.sha256(self.raw).hexdigest(), BYTE_SHA256)
		self.assertEqual(_semantic_digest(self.contract), SEMANTIC_DIGEST)

	def test_backend_vocabulary_equals_the_contract(self):
		contract = self.contract
		self.assertEqual(set(contract["source_tools"]), set(SOURCE_TOOLS))
		keys = set(contract["reference_keys"])
		self.assertEqual(keys, set(REFERENCE_FIELDS))
		self.assertEqual(contract["retention"], "source-reference-v1")
		self.assertIs(contract["requery_required"], True)

	def test_backend_parses_every_receipt_shape_in_the_fixture(self):
		contract = self.contract
		allowed = (
			set(contract["reference_keys"])
			| set(contract["reference_list_keys"])
			| set(contract["allowed_non_reference_keys"])
		)
		self.assertTrue(contract["cases"])
		for case in contract["cases"]:
			with self.subTest(case=case["name"]):
				args, result = source_receipt(case["tool"], case["args"], case["backend_result"])
				expect = case["expect"]
				data = result["data"]
				for obj in (result, data):
					self.assertLessEqual(set(obj), allowed)
				self.assertEqual(data["retention"], contract["retention"])
				self.assertIs(data["requery_required"], True)
				self.assertEqual(_references(args, contract), expect["input_references"])
				self.assertEqual(_references(data, contract), expect["result_references"])
				self.assertLessEqual(set(args), allowed)
				serialised = json.dumps([args, result])
				for value in expect["forbidden"]:
					self.assertNotIn(value, serialised)
