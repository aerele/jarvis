"""Inline report snapshots: create, owner-bound reads, gates and Redis rules.

Run on a dedicated test site. Redis keys under `jarvis:report_snapshot*` are cleared
around every test. Fixtures are deterministic.
"""

import ast
import gzip
import hashlib
import json
import logging
import re
import secrets
import time
import uuid
import zlib
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
import redis
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError, RetryLaterError
from jarvis.tools import _report_snapshot_digest as digest
from jarvis.tools import _report_snapshot_payload as payload
from jarvis.tools import _report_snapshot_reload as reload_module
from jarvis.tools import _report_snapshot_source as source
from jarvis.tools import _report_snapshot_store as store
from jarvis.tools import _report_snapshots as snap
from jarvis.tools._report_query_data import decode_report, encode

NOT_AVAILABLE = "This report snapshot expired or is not available. Run run_report again."
CHANGED = "This report snapshot changed while it was read. Send the same call again."
UNAVAILABLE = "Report snapshots are temporarily unavailable. Send the same call again."

COLUMNS = [
	{"fieldname": "name", "label": "Name", "fieldtype": "Data"},
	{"fieldname": "amount", "label": "Amount", "fieldtype": "Currency"},
	{"fieldname": "posting_date", "label": "Date", "fieldtype": "Date"},
]


def _rows(count):
	return [
		{
			"name": f"ROW-{i:03}",
			"amount": Decimal(i) / Decimal(4),
			"posting_date": date(2026, 1, 1) + timedelta(days=i),
		}
		for i in range(1, count + 1)
	]


def _data(count=12, **extra):
	return {"columns": COLUMNS, "result": _rows(count), "skip_total_row": 0, **extra}


def _clear_keys():
	cache = frappe.cache()
	keys = cache.keys(cache.make_key("jarvis:report_snapshot*"))
	if keys:
		cache.delete(*keys)
	digest.forget_perm_digest()


def _snapshot_keys():
	cache = frappe.cache()
	return sorted(k.decode() for k in cache.keys(cache.make_key("jarvis:report_snapshot*")))


def _has(key):
	"""1 when a fully built key exists. Not `cache.exists`: the wrapper prefixes the key again."""
	return int(frappe.cache().ttl(key) != -2)


def _meta_key(snapshot_id):
	return frappe.cache().make_key(store.META_PREFIX + snapshot_id)


def _content_key(snapshot_id):
	return frappe.cache().make_key(store.CONTENT_PREFIX + snapshot_id)


def _meta(snapshot_id):
	return json.loads(frappe.cache().get(_meta_key(snapshot_id)))


def _put_meta(snapshot_id, **changes):
	meta = _meta(snapshot_id)
	meta.update(changes)
	frappe.cache().set(_meta_key(snapshot_id), json.dumps(meta), xx=True, keepttl=True)


def _json_bytes(data):
	payload = {"columns": data["columns"], "result": data["result"], "skip_total_row": data["skip_total_row"]}
	from frappe.utils.response import json_handler

	return json.dumps(payload, default=json_handler, sort_keys=True).encode()


@contextmanager
def _connection_fault(method):
	"""Fail every call of one redis-py connection method; assert in the test that it fired."""
	import redis.connection as connection

	fired = []

	def faulty(self, *args, **kwargs):
		fired.append(method)
		raise redis.exceptions.ConnectionError("injected " + method)

	with patch.object(connection.AbstractConnection, method, faulty):
		yield fired


class SnapshotCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.tag = uuid.uuid4().hex[:8]
		cls.report = frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": "Jarvis Snapshot Fixture " + cls.tag,
				"ref_doctype": "ToDo",
				"report_type": "Query Report",
				"is_standard": "No",
				"query": "select name from `tabToDo` limit 1",
				"disable_prepared_report_automation": 1,
				"roles": [{"role": "System Manager"}],
			}
		).insert(ignore_permissions=True)
		cls.foreign = "snap-foreign-" + cls.tag + "@example.test"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": cls.foreign,
				"first_name": "Snapshot Foreign",
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": "System Manager"}],
			}
		).insert(ignore_permissions=True)
		cls.company = frappe.db.get_value("Company", {}, "name")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.delete_doc("User", cls.foreign, force=True, ignore_permissions=True)
		frappe.delete_doc("Report", cls.report.name, force=True, ignore_permissions=True)
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_clear_keys()

	def tearDown(self):
		frappe.set_user("Administrator")
		_clear_keys()

	def make(self, data=None, *, report_name=None, filters=None, **kwargs):
		result = snap.create(
			data or _data(),
			report_name=report_name or self.report.name,
			filters={} if filters is None else filters,
			run_ms=kwargs.pop("run_ms", 1200),
			**kwargs,
		)
		self.assertIsNotNone(result[0], result)
		return result[0]

	def query(self, snapshot_id, **kwargs):
		from jarvis.tools.query_report import query_report

		return query_report(snapshot_id, **kwargs)


class TestCreate(SnapshotCase):
	def test_round_trip_keeps_decimal_and_date_and_hashes_the_json(self):
		data = _data()
		snapshot_id, reason = snap.create(data, report_name=self.report.name, filters={"a": 1}, run_ms=5)
		self.assertEqual(reason, "ok")
		self.assertRegex(snapshot_id, r"^rs_[A-Za-z0-9_-]{32}$")
		stored = frappe.cache().get(_content_key(snapshot_id))
		self.assertEqual(hashlib.sha256(gzip.decompress(stored)).hexdigest(), _meta(snapshot_id)["sha256"])
		self.assertEqual(gzip.decompress(stored), _json_bytes(data))
		schema, rows, total_index = decode_report(stored)
		self.assertEqual((len(schema["columns"]), len(rows), total_index), (3, 12, None))
		self.assertEqual(str(rows[2]["amount"]), "0.75")
		self.assertEqual(rows[0]["posting_date"], str(date(2026, 1, 2)))
		meta = _meta(snapshot_id)
		self.assertEqual(meta["owner"], "Administrator")
		self.assertEqual(meta["report_name"], self.report.name)
		self.assertEqual(meta["filters"], {"a": 1})
		self.assertEqual((meta["generation"], meta["run_ms"]), (0, 5))
		self.assertEqual(meta["perm_digest"], snap.perm_digest("Administrator", self.report.name))
		self.assertTrue(meta["created_at"])

	def test_a_digest_taken_before_the_run_is_the_one_stored(self):
		"""A role removed during a long run must not stamp the old-access rows with the new
		digest: `create` stores the digest it is given, not one taken after the run."""
		before = snap.perm_digest("Administrator", self.report.name, fresh=True)
		with patch.object(frappe, "get_roles", return_value=["System Manager", "Another Role"]):
			digest.forget_perm_digest()  # a new request computes the post-run value
			snapshot_id = self.make(perm_digest=before)
			self.assertNotEqual(snap.perm_digest("Administrator", self.report.name), before)
			self.assertEqual(_meta(snapshot_id)["perm_digest"], before)
			with self.assertRaises(RetryLaterError) as caught:  # the next read sees the change
				snap.SnapshotSource(snapshot_id).read()
		self.assertEqual(str(caught.exception), CHANGED)
		self.assertEqual(_has(_content_key(snapshot_id)), 0)

	def test_keys_are_site_prefixed_and_expire(self):
		snapshot_id = self.make()
		cache = frappe.cache()
		self.assertTrue(0 < cache.ttl(_content_key(snapshot_id)) <= 3600)
		self.assertTrue(3600 < cache.ttl(_meta_key(snapshot_id)) <= 86400)
		prefix = cache.make_key("jarvis:report_snapshot").decode()
		self.assertTrue(all(k.startswith(prefix) for k in _snapshot_keys()))
		self.assertTrue(all(k.startswith(frappe.conf.get("db_name") + "|") for k in _snapshot_keys()))

	def test_two_creates_of_identical_data_share_a_sha(self):
		first, second = self.make(), self.make()
		self.assertNotEqual(first, second)
		self.assertEqual(_meta(first)["sha256"], _meta(second)["sha256"])

	def test_an_empty_list_is_stored(self):
		snapshot_id, reason = snap.create(
			{"columns": COLUMNS, "result": [], "skip_total_row": 0},
			report_name=self.report.name,
			filters={},
			run_ms=1,
		)
		self.assertIsNotNone(snapshot_id, reason)

	def test_every_skip_reason_stores_nothing(self):
		cases = {
			"prepared": _data(prepared_report=True),
			"no_rows": {"columns": COLUMNS, "result": None, "skip_total_row": 0},
			"tree": {
				"columns": COLUMNS,
				"result": [{**_rows(1)[0], "indent": 0}],
				"skip_total_row": 0,
			},
			"undecodable": {"columns": COLUMNS, "result": [["a", "1", "2"]], "skip_total_row": 0},
		}
		for reason, data in cases.items():
			with self.subTest(reason):
				self.assertEqual(
					snap.create(data, report_name=self.report.name, filters={}, run_ms=1), (None, reason)
				)
				self.assertEqual(_snapshot_keys(), [])
		with self.subTest("no_rows string"):
			bad = {"columns": COLUMNS, "result": "x", "skip_total_row": 0}
			self.assertEqual(
				snap.create(bad, report_name=self.report.name, filters={}, run_ms=1), (None, "no_rows")
			)
		with (
			self.subTest("private"),
			patch.object(payload._private_output, "metadata_redacted", return_value=True),
		):
			self.assertEqual(
				snap.create(_data(), report_name=self.report.name, filters={}, run_ms=1), (None, "private")
			)
		with self.subTest("disabled"), patch.dict(frappe.conf, {"jarvis_report_snapshots": 0}):
			self.assertEqual(
				snap.create(_data(), report_name=self.report.name, filters={}, run_ms=1), (None, "disabled")
			)
		self.assertEqual(_snapshot_keys(), [])

	def test_caps_stop_a_snapshot(self):
		for name, value in (("MAX_ROWS", 3), ("MAX_COLUMNS", 2), ("MAX_EXPANDED", 100), ("MAX_GZIP", 50)):
			with self.subTest(name), patch.object(payload, name, value):
				self.assertEqual(
					snap.create(_data(5), report_name=self.report.name, filters={}, run_ms=1),
					(None, "caps"),
				)
		with self.subTest("filters"):
			big = {"note": "x" * 5000}
			self.assertEqual(
				snap.create(_data(), report_name=self.report.name, filters=big, run_ms=1), (None, "caps")
			)
		self.assertEqual(_snapshot_keys(), [])

	def test_a_failed_multi_leaves_no_key(self):
		with patch("redis.client.Pipeline.execute", side_effect=redis.exceptions.ConnectionError("down")):
			self.assertEqual(
				snap.create(_data(), report_name=self.report.name, filters={}, run_ms=1), (None, "error")
			)
		self.assertEqual(_snapshot_keys(), [])

	def test_a_falsy_multi_reply_deletes_both_keys(self):
		real = redis.client.Pipeline.execute

		def run_then_fail(pipe, *args, **kwargs):
			real(pipe, *args, **kwargs)
			return [True, False]

		with patch("redis.client.Pipeline.execute", run_then_fail):
			self.assertEqual(
				snap.create(_data(), report_name=self.report.name, filters={}, run_ms=1), (None, "error")
			)
		self.assertEqual(_snapshot_keys(), [])

	def test_create_never_raises_and_logs_no_values(self):
		secret = "SECRET-FILTER-VALUE"
		with (
			patch("redis.client.Pipeline.execute", side_effect=RuntimeError(secret)),
			self.assertLogs(
				frappe.logger("jarvis.report_snapshot", max_size=1_000_000, file_count=30), level="WARNING"
			) as logs,
		):
			result = snap.create(
				_data(), report_name=self.report.name, filters={"customer": secret}, run_ms=1
			)
		self.assertEqual(result, (None, "error"))
		joined = "\n".join(logs.output)
		self.assertIn("RuntimeError", joined)
		self.assertNotIn(secret, joined)
		line = json.loads(logs.records[-1].getMessage())
		self.assertEqual(
			(line["kind"], line["event"], line["outcome"], line["error"]),
			("report_snapshot", "failure", "create_error", "RuntimeError"),
		)

	def test_sixth_snapshot_deletes_the_oldest_with_both_keys(self):
		ids = [self.make() for _ in range(6)]
		self.assertEqual(_has(_meta_key(ids[0])), 0)
		self.assertEqual(_has(_content_key(ids[0])), 0)
		for kept in ids[1:]:
			self.assertEqual(_has(_meta_key(kept)), 1)
			self.assertEqual(_has(_content_key(kept)), 1)
		index = store.INDEX_PREFIX + "Administrator"
		listed = [i.decode() for i in frappe.cache().lrange(index, 0, -1)]
		self.assertEqual(listed, list(reversed(ids[1:])))

	def test_index_key_is_site_prefixed_with_a_ttl(self):
		self.make()
		key = frappe.cache().make_key(store.INDEX_PREFIX + "Administrator")
		self.assertEqual(_has(key), 1)
		self.assertTrue(0 < frappe.cache().ttl(key) <= 86400)
		unprefixed = (store.INDEX_PREFIX + "Administrator").encode()
		self.assertEqual(_has(unprefixed), 0)

	def test_another_owner_has_an_own_index(self):
		self.make()
		frappe.set_user(self.foreign)
		self.make()
		for owner in ("Administrator", self.foreign):
			self.assertEqual(frappe.cache().llen(store.INDEX_PREFIX + owner), 1)

	# --- A3: caps before serialisation, early gzip abort, no round trip -----------------

	def test_the_cell_cap_stops_a_snapshot_before_anything_is_serialised(self):
		def serialised(*args, **kwargs):
			raise AssertionError("serialised")

		with (
			patch.object(payload, "MAX_CELLS", 10),
			patch.object(payload, "json", SimpleNamespace(dumps=serialised)),
		):
			result = snap.create(_data(12), report_name=self.report.name, filters={}, run_ms=1)
		self.assertEqual(result, (None, "caps"))
		self.assertEqual(_snapshot_keys(), [])

	def test_the_size_api_measured_stops_a_snapshot_before_anything_is_serialised(self):
		def serialised(*args, **kwargs):
			raise AssertionError("serialised")

		with patch.object(payload, "json", SimpleNamespace(dumps=serialised)):
			result = snap.create(
				_data(),
				report_name=self.report.name,
				filters={},
				run_ms=1,
				measured=payload.MAX_EXPANDED + 1,
			)
		self.assertEqual(result, (None, "caps"))
		fits = snap.create(
			_data(), report_name=self.report.name, filters={}, run_ms=1, measured=payload.MAX_EXPANDED
		)
		self.assertEqual(fits[1], "ok")

	def test_the_gzip_stops_as_soon_as_its_output_passes_the_cap(self):
		noise = [
			{"name": secrets.token_urlsafe(96), "amount": Decimal(i), "posting_date": date(2026, 1, 1)}
			for i in range(3000)
		]
		data = {"columns": COLUMNS, "result": noise, "skip_total_row": 0}
		fed = []
		real = zlib.compressobj

		def spy(*args, **kwargs):
			packer = real(*args, **kwargs)

			class Counting:
				def compress(self, chunk):
					fed.append(len(chunk))
					return packer.compress(chunk)

				def flush(self):
					return packer.flush()

			return Counting()

		with (
			patch.object(payload, "MAX_GZIP", 20_000),
			patch.object(payload, "GZIP_CHUNK", 8192),
			patch.object(payload.zlib, "compressobj", spy),
		):
			result = snap.create(data, report_name=self.report.name, filters={}, run_ms=1)
		self.assertEqual(result, (None, "caps"))
		total_chunks = -(-len(_json_bytes(data)) // 8192)
		self.assertGreater(total_chunks, 10)
		self.assertTrue(fed)
		self.assertLess(len(fed), total_chunks)

	def test_the_web_request_makes_no_gzip_and_json_round_trip(self):
		with patch("jarvis.tools._report_query_data.decode_report", side_effect=AssertionError("decoded")):
			snapshot_id, reason = snap.create(_data(), report_name=self.report.name, filters={}, run_ms=1)
		self.assertEqual(reason, "ok")
		decode_report(frappe.cache().get(_content_key(snapshot_id)))  # what read() will do

	def test_the_structural_checks_of_the_read_still_run_before_storing(self):
		long_name = "x" * 300
		cases = {
			"oversized column metadata": {
				"columns": [{"fieldname": long_name, "label": "L", "fieldtype": "Data"}],
				"result": [{long_name: 1}],
				"skip_total_row": 0,
			},
			"ambiguous dynamic link": {
				"columns": [
					{"fieldname": "ref", "label": "Ref", "fieldtype": "Dynamic Link", "options": "kind"},
					{"fieldname": "kind", "label": "Kind", "fieldtype": "Data"},
					{"fieldname": "kind", "label": "Kind 2", "fieldtype": "Data"},
				],
				"result": [{"ref": "A", "kind": "ToDo"}],
				"skip_total_row": 0,
			},
			"rows without columns": {"columns": [], "result": [{"a": 1}], "skip_total_row": 0},
		}
		for label, data in cases.items():
			with self.subTest(label):
				self.assertEqual(
					snap.create(data, report_name=self.report.name, filters={}, run_ms=1),
					(None, "undecodable"),
				)
		self.assertEqual(_snapshot_keys(), [])

	def test_validate_report_and_decode_report_agree_on_a_stored_result(self):
		from jarvis.tools._report_query_data import validate_report

		data = _data(7)
		stored = decode_report(gzip.compress(_json_bytes(data), mtime=0))
		schema, rows, total_index = validate_report(data["columns"], data["result"], skip=0, has_skip=True)
		self.assertEqual((schema, len(rows), total_index), (stored[0], len(stored[1]), stored[2]))

	# --- A7: one atomic index step ------------------------------------------------------

	def index_ids(self, owner="Administrator"):
		index = store.INDEX_PREFIX + owner
		return [i.decode() for i in frappe.cache().lrange(index, 0, -1)]

	def live_ids(self):
		keys = _snapshot_keys()
		prefix = frappe.cache().make_key(store.META_PREFIX).decode()
		return sorted(k[len(prefix) :] for k in keys if k.startswith(prefix))

	def test_a_fault_in_the_index_step_leaves_the_list_and_the_keys_as_they_were(self):
		ids = [self.make() for _ in range(5)]
		before = (self.index_ids(), _snapshot_keys())
		real = redis.client.Pipeline.execute

		def fault(pipe, *args, **kwargs):
			if any(command[0][0] == "RPUSH" for command in pipe.command_stack):
				raise redis.exceptions.ConnectionError("down inside the index step")
			return real(pipe, *args, **kwargs)

		with patch("redis.client.Pipeline.execute", fault):
			result = snap.create(_data(), report_name=self.report.name, filters={}, run_ms=1)
		self.assertEqual(result, (None, "error"))
		self.assertEqual((self.index_ids(), _snapshot_keys()), before)
		self.assertEqual(self.index_ids(), list(reversed(ids)))

	def test_two_creates_that_overlap_never_leave_more_than_five_live_snapshots(self):
		first = [self.make() for _ in range(4)]
		nested, started = [], []

		def interleave():
			# a second create lands between this one's read of the list and its EXEC
			if not started:
				started.append(True)
				nested.append(self.make())

		with patch.object(store, "_before_index_exec", interleave):
			outer = self.make()
		self.assertEqual(len(nested), 1)
		expected = [outer, nested[0], *reversed(first)][:5]
		self.assertEqual(sorted(self.index_ids()), sorted(expected))
		self.assertEqual(self.live_ids(), sorted(expected))
		self.assertEqual(len(self.live_ids()), 5)
		dropped = (set(first) | {outer, nested[0]}) - set(expected)
		self.assertEqual(len(dropped), 1)
		for gone in dropped:
			self.assertEqual((_has(_meta_key(gone)), _has(_content_key(gone))), (0, 0))

	def test_a_lost_watch_that_never_clears_raises_and_leaves_no_phantom_id(self):
		def always_busy():
			# the wrapper's list methods take the UNPREFIXED key
			frappe.cache().rpush(store.INDEX_PREFIX + "Administrator", "rs_" + "Z" * 32)

		with patch.object(store, "_before_index_exec", always_busy):
			result = snap.create(_data(), report_name=self.report.name, filters={}, run_ms=1)
		self.assertEqual(result[0], None)
		self.assertEqual(result[1], "error")
		self.assertEqual(self.live_ids(), [])

	def test_a_reload_write_moves_the_id_in_one_step_and_a_fault_keeps_its_place(self):
		first, second = self.make(), self.make()
		real = redis.client.Pipeline.execute

		def fault(pipe, *args, **kwargs):
			if any(command[0][0] == "RPUSH" for command in pipe.command_stack):
				raise redis.exceptions.ConnectionError("down inside the index step")
			return real(pipe, *args, **kwargs)

		data = _data(3)
		with patch("redis.client.Pipeline.execute", fault):
			written = snap.create(
				data,
				report_name=self.report.name,
				filters={},
				run_ms=1,
				snapshot_id=first,
				generation=1,
				created=(_meta(first)["created_at"], _meta(first)["created_ts"]),
			)
		self.assertEqual(written, (first, "ok"))  # the rows are written; the index fault is logged
		self.assertEqual(self.index_ids(), [second, first])
		self.assertEqual(_meta(first)["generation"], 1)
		snap.create(
			data,
			report_name=self.report.name,
			filters={},
			run_ms=1,
			snapshot_id=first,
			generation=2,
			created=(_meta(first)["created_at"], _meta(first)["created_ts"]),
		)
		self.assertEqual(self.index_ids(), [first, second])


class TestPermDigest(SnapshotCase):
	def digest(self, user="Administrator"):
		digest.forget_perm_digest()
		return snap.perm_digest(user, self.report.name)

	def test_deterministic_across_two_computations(self):
		self.assertEqual(self.digest(), self.digest())
		self.assertEqual(len(self.digest()), 64)

	def test_memoised_for_one_request(self):
		first = snap.perm_digest("Administrator", self.report.name)
		with patch.object(frappe, "get_roles", side_effect=AssertionError("memo not used")):
			self.assertEqual(snap.perm_digest("Administrator", self.report.name), first)

	def test_the_party_doctype_is_found_by_the_canonical_report_name(self):
		# The database matches a report name case-insensitively; the AR/AP lookup must too.
		if not frappe.db.exists("Report", "Accounts Receivable"):
			self.skipTest("no Accounts Receivable report on the test site")
		exact = snap.perm_digest("Administrator", "Accounts Receivable")
		digest.forget_perm_digest()
		self.assertEqual(snap.perm_digest("Administrator", "accounts receivable"), exact)

	def test_each_input_changes_the_digest(self):
		base = self.digest()
		with patch.object(frappe, "get_roles", return_value=["System Manager", "Extra Role"]):
			self.assertNotEqual(self.digest(), base)
		with patch.object(digest, "get_user_permissions", return_value={"Company": [{"doc": "X"}]}):
			self.assertNotEqual(self.digest(), base)
		with patch.object(digest, "get_role_permissions", return_value={"read": 1, "if_owner": {"read": 1}}):
			self.assertNotEqual(self.digest(), base)

	def test_a_report_save_that_only_moves_modified_does_not_change_the_digest(self):
		# Not an access input: gate_report re-checks the report on every read. Frappe's
		# prepared-report automation saves the Report in the middle of a slow run.
		base = self.digest()
		before = frappe.db.get_value("Report", self.report.name, "modified")
		self.addCleanup(frappe.db.set_value, "Report", self.report.name, "query", self.report.query)
		frappe.db.set_value("Report", self.report.name, "query", "select name from `tabToDo` limit 2")
		self.assertNotEqual(frappe.db.get_value("Report", self.report.name, "modified"), before)
		self.assertEqual(self.digest(), base)

	def test_a_read_share_changes_the_digest(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "digest share " + self.tag}).insert(
			ignore_permissions=True
		)
		base = self.digest(self.foreign)
		frappe.share.add_docshare(
			"ToDo", todo.name, self.foreign, read=1, flags={"ignore_share_permission": True}
		)
		self.assertNotEqual(self.digest(self.foreign), base)

	def seed_shares(self, count, reverse=False):
		stamp = "2026-01-01 00:00:00.000000"
		fields = [
			"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx",
			"user", "share_doctype", "share_name", "read", "write", "share", "submit", "everyone",
		]  # fmt: skip
		numbers = range(count, 0, -1) if reverse else range(1, count + 1)
		values = [
			(f"snapcost-{self.tag}-{n}", "Administrator", stamp, stamp, "Administrator", 0, 0)
			+ (self.foreign, "ToDo", f"snapcost-{self.tag}-{n}", 1, 0, 0, 0, 0)
			for n in numbers
		]
		frappe.db.bulk_insert("DocShare", fields, values, ignore_duplicates=False)

	def drop_shares(self):
		frappe.db.delete("DocShare", {"share_name": ("like", f"snapcost-{self.tag}-%")})

	def new_request(self):
		frappe.local.role_permissions = {}
		digest.forget_perm_digest()

	def counted_digest(self):
		self.new_request()
		real = frappe.db.sql
		with patch.object(frappe.db, "sql", wraps=real) as spy:
			digest = snap.perm_digest(self.foreign, self.report.name)
		return digest, spy.call_count

	def warmed_count(self):
		"""The query count of one digest after a discarded run, so the cold meta and
		permission cache loads are not counted."""
		self.counted_digest()
		return self.counted_digest()[1]

	def test_digest_query_count_does_not_grow_with_the_share_rows(self):
		self.addCleanup(self.drop_shares)
		empty = self.warmed_count()
		self.assertGreaterEqual(empty, 1)
		self.seed_shares(5000)
		self.assertEqual(
			frappe.db.count("DocShare", {"share_name": ("like", f"snapcost-{self.tag}-%")}), 5000
		)
		full = self.warmed_count()
		self.assertEqual(full, empty)

	def test_digest_is_equal_across_two_requests_with_shares_inserted_in_reverse(self):
		self.addCleanup(self.drop_shares)
		self.seed_shares(300)
		first, _ = self.counted_digest()
		self.drop_shares()
		self.seed_shares(300, reverse=True)
		second, _ = self.counted_digest()
		self.assertEqual(first, second)
		self.drop_shares()
		third, _ = self.counted_digest()
		self.assertNotEqual(first, third)

	def test_never_uses_masked_fields_of(self):
		with patch("jarvis.compat.masked_fields_of", side_effect=AssertionError("banned")):
			self.digest()


def _banned_calls(source):
	"""Calls that break the Redis rules of B.9.0 in `source`: `expire_key` and
	`masked_fields_of` on any receiver; `exists`, `get_value`, `set_value`, `delete_value` on
	a receiver that is `frappe.cache()`, a name bound to it (assignment or `with`), a call
	of a helper that returns it, or a parameter that a call passes it to. `frappe.db.get_value`
	is fine."""
	tree = ast.parse(source)
	functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
	names, cache_helpers = set(), set()

	def is_cache_call(node):
		if not isinstance(node, ast.Call):
			return False
		func = node.func
		if isinstance(func, ast.Name) and func.id in cache_helpers:
			return True
		return (
			isinstance(func, ast.Attribute)
			and func.attr == "cache"
			and isinstance(func.value, ast.Name)
			and func.value.id == "frappe"
		)

	def from_cache(node):
		while not is_cache_call(node) and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
			node = node.func.value
		return is_cache_call(node) or (isinstance(node, ast.Name) and node.id in names)

	for _ in range(4):
		for node in ast.walk(tree):
			if isinstance(node, ast.Assign) and from_cache(node.value):
				names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
			elif isinstance(node, ast.withitem) and from_cache(node.context_expr):
				if isinstance(node.optional_vars, ast.Name):
					names.add(node.optional_vars.id)
			elif isinstance(node, ast.Return) and node.value is not None and from_cache(node.value):
				cache_helpers |= {f.name for f in functions.values() if node in ast.walk(f)}
			elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
				params = [a.arg for a in functions[node.func.id].args.args]
				for param, arg in zip(params, node.args, strict=False):
					if from_cache(arg):
						names.add(param)
	found = []
	for node in ast.walk(tree):
		if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
			attr = node.func.attr
			if attr in {"expire_key", "masked_fields_of"}:
				found.append(attr)
			elif attr in {"exists", "get_value", "set_value", "delete_value"} and from_cache(node.func.value):
				found.append(attr)
	return found


SNAPSHOT_MODULES = (
	"_report_snapshots",
	"_report_snapshot_events",
	"_report_snapshot_digest",
	"_report_snapshot_payload",
	"_report_snapshot_store",
	"_report_snapshot_source",
	"_report_snapshot_reload",
)


def snapshot_module_paths():
	import importlib

	return [Path(importlib.import_module("jarvis.tools." + name).__file__) for name in SNAPSHOT_MODULES]


class TestStaticRules(FrappeTestCase):
	def test_every_snapshot_module_avoids_the_wrapper_methods_that_swallow_errors(self):
		for path in snapshot_module_paths():
			self.assertEqual(_banned_calls(path.read_text()), [], path.name)

	def test_the_detector_is_receiver_aware(self):
		bad = [
			"import frappe\nfrappe.cache().get_value('k')",
			"import frappe\nc = frappe.cache()\nc.exists('k')",
			"import frappe\nwith frappe.cache().pipeline() as p:\n\tp.set_value('k', 1)",
			"x.expire_key('k', 1)",
			"compat.masked_fields_of(m)",
			"import frappe\ncache = frappe.cache()\ncache.delete_value('k')",
			"import frappe\ndef helper():\n\treturn frappe.cache()\nhelper().get_value('k')",
			"import frappe\ndef helper():\n\tc = frappe.cache()\n\treturn c\nx = helper()\nx.exists('k')",
			"import frappe\ndef use(c):\n\tc.set_value('k', 1)\nuse(frappe.cache())",
			"import frappe\ndef use(c, d):\n\td.exists('k')\nuse(1, frappe.cache())",
		]
		for code in bad:
			with self.subTest(code):
				self.assertTrue(_banned_calls(code))
		good = [
			"import frappe\nfrappe.db.get_value('Report', 'x', 'y')",
			"import frappe\nfrappe.db.exists('Report', 'x')",
			"import frappe\nc = frappe.cache()\nc.get('k')",
			"import frappe\ndef helper():\n\treturn {}\nhelper().get_value('k')",
			"import frappe\ndef use(c):\n\tc.exists('k')\nuse({})",
		]
		for code in good:
			with self.subTest(code):
				self.assertEqual(_banned_calls(code), [])


class TestSnapshotRead(SnapshotCase):
	def test_inspect_then_exact_sum_equals_python(self):
		snapshot_id = self.make(_data(12))
		first = self.query(snapshot_id)
		self.assertFalse(first["sql_executed"])
		self.assertEqual(first["prepared_report"], snapshot_id)
		self.assertEqual(first["report_name"], self.report.name)
		answer = self.query(
			snapshot_id,
			version=first["version"],
			sql="SELECT decimal_sum(c2), COUNT(*) FROM report_rows",
		)
		self.assertTrue(answer["sql_executed"])
		total, count = answer["rows"][0]
		self.assertEqual((Decimal(total), count), (sum(r["amount"] for r in _rows(12)), 12))

	def sql(self, snapshot_id, text):
		version = self.query(snapshot_id)["version"]
		return self.query(snapshot_id, version=version, sql=text)

	def test_b_appended_total_row_is_in_report_totals_not_in_the_sum(self):
		detail = _rows(12)
		total = ["Total", sum(r["amount"] for r in detail), None]
		data = {"columns": COLUMNS, "result": [*detail, total], "skip_total_row": 0}
		snapshot_id = self.make(data)
		answer = self.sql(snapshot_id, "SELECT decimal_sum(c2), COUNT(*) FROM report_rows")
		self.assertEqual(
			(Decimal(answer["rows"][0][0]), answer["rows"][0][1]), (sum(r["amount"] for r in detail), 12)
		)
		totals = self.sql(snapshot_id, "SELECT c1, c2 FROM report_totals")
		self.assertEqual(len(totals["rows"]), 1)
		self.assertEqual(totals["rows"][0][0], "Total")
		self.assertEqual(Decimal(totals["rows"][0][1]), sum(r["amount"] for r in detail))

	def test_c_own_dict_total_row_with_skip_total_row_counts_as_a_detail_row(self):
		detail = _rows(12)
		own = {"name": "Total", "amount": sum(r["amount"] for r in detail), "posting_date": None}
		data = {"columns": COLUMNS, "result": [*detail, own], "skip_total_row": 1}
		snapshot_id = self.make(data)
		answer = self.sql(snapshot_id, "SELECT decimal_sum(c2), COUNT(*) FROM report_rows")
		expected = sum(r["amount"] for r in detail)
		# Documented: with skip_total_row the report's own label row is a detail row, so a plain
		# sum counts it twice. This is the envelope note's "check before summing" case.
		self.assertEqual((Decimal(answer["rows"][0][0]), answer["rows"][0][1]), (expected * 2, 13))
		self.assertEqual(self.sql(snapshot_id, "SELECT COUNT(*) FROM report_totals")["rows"], [[0]])
		self.assertEqual(
			self.sql(snapshot_id, "SELECT c2 FROM report_rows WHERE c1 = 'Total'")["rows"], [["19.5"]]
		)

	def test_unchanged_version_is_stable_between_calls(self):
		snapshot_id = self.make()
		self.assertEqual(self.query(snapshot_id)["version"], self.query(snapshot_id)["version"])

	def test_receipt_fields_and_size_with_reloaded_block(self):
		filters = {f"filter_{i}": "v" * 60 for i in range(30)}
		self.assertLess(len(encode(filters)), payload.MAX_FILTER_BYTES)
		snapshot_id = self.make(filters=filters)
		result = self.query(snapshot_id)
		keys = (
			"prepared_report",
			"report_name",
			"as_of",
			"created_at",
			"generation",
			"filters",
			"scope_digest",
			"version",
			"sql_executed",
		)
		receipt = {k: result[k] for k in keys}
		self.assertEqual(receipt["as_of"], _meta(snapshot_id)["created_at"])
		self.assertEqual(receipt["generation"], 0)
		reloaded = {"generation": 1, "reloaded_at": "2026-11-18 10:00:00.123456", "data_changed": False}
		self.assertLessEqual(len(encode({**receipt, "reloaded": {**reloaded, "perm_changed": False}})), 4000)

	def test_other_user_gets_the_same_text_as_a_missing_id(self):
		snapshot_id = self.make()
		missing = "rs_" + "A" * 32
		frappe.set_user(self.foreign)
		errors = []
		for target in (snapshot_id, missing):
			with self.assertRaises(InvalidArgumentError) as caught:
				self.query(target)
			errors.append(str(caught.exception))
		self.assertEqual(errors, [NOT_AVAILABLE, NOT_AVAILABLE])
		self.assertEqual(_has(_content_key(snapshot_id)), 1)

	def test_read_itself_refuses_a_foreign_owner_with_the_missing_text(self):
		snapshot_id = self.make()
		frappe.set_user(self.foreign)
		with self.assertRaises(InvalidArgumentError) as caught:
			snap.SnapshotSource(snapshot_id).read()
		self.assertEqual(str(caught.exception), NOT_AVAILABLE)

	def test_bad_id_formats_are_refused_before_any_redis_read(self):
		for bad in ("rs_short", "rs_" + "!" * 32, "rs_" + "a" * 33, "rs_" + "a" * 31, "rs_"):
			with self.subTest(bad):
				with (
					patch.object(type(frappe.cache()), "get", side_effect=AssertionError("redis read")),
					patch.object(type(frappe.cache()), "pipeline", side_effect=AssertionError("redis read")),
					self.assertRaises(InvalidArgumentError) as caught,
				):
					self.query(bad)
				self.assertEqual(str(caught.exception), NOT_AVAILABLE)

	def test_tampered_sha_is_refused_and_content_deleted(self):
		snapshot_id = self.make()
		_put_meta(snapshot_id, sha256="0" * 64)
		with self.assertRaises(RetryLaterError) as caught:
			self.query(snapshot_id)
		self.assertEqual(str(caught.exception), CHANGED)
		self.assertEqual(_has(_content_key(snapshot_id)), 0)

	def test_tampered_content_is_refused_and_deleted(self):
		snapshot_id = self.make()
		frappe.cache().set(_content_key(snapshot_id), gzip.compress(_json_bytes(_data(2))), keepttl=True)
		with self.assertRaises(RetryLaterError):
			self.query(snapshot_id)
		self.assertEqual(_has(_content_key(snapshot_id)), 0)

	def test_a_corrupt_gzip_is_refused_and_deleted(self):
		snapshot_id = self.make()
		frappe.cache().set(_content_key(snapshot_id), b"not gzip", keepttl=True)
		with self.assertRaises(RetryLaterError):
			self.query(snapshot_id)
		self.assertEqual(_has(_content_key(snapshot_id)), 0)

	def test_pinned_version_mismatch_is_refused(self):
		snapshot_id = self.make()
		version = self.query(snapshot_id)["version"]
		other = _data(5)
		frappe.cache().set(
			_content_key(snapshot_id), gzip.compress(_json_bytes(other), mtime=0), keepttl=True
		)
		_put_meta(snapshot_id, sha256=hashlib.sha256(_json_bytes(other)).hexdigest())
		with self.assertRaises(InvalidArgumentError) as caught:
			self.query(snapshot_id, version=version, sql="SELECT COUNT(*) FROM report_rows")
		self.assertNotIn("reloaded at", str(caught.exception))
		_put_meta(snapshot_id, generation=1, reloaded_at="2026-11-18 10:00:00.000000")
		with self.assertRaises(InvalidArgumentError) as caught:
			self.query(snapshot_id, version=version, sql="SELECT COUNT(*) FROM report_rows")
		self.assertIn("reloaded at 2026-11-18 10:00:00", str(caught.exception))
		self.assertIn("inspect it again", str(caught.exception))

	def test_a_changed_digest_in_read_is_retryable_and_deletes_content(self):
		snapshot_id = self.make()
		self.assertIsNone(snap.ensure_live(snapshot_id))
		digest.forget_perm_digest()
		with (
			patch.object(frappe, "get_roles", return_value=["System Manager", "Another Role"]),
			self.assertRaises(RetryLaterError) as caught,
		):
			snap.SnapshotSource(snapshot_id).read()
		self.assertEqual(str(caught.exception), CHANGED)
		self.assertEqual(_has(_content_key(snapshot_id)), 0)
		self.assertEqual(_has(_meta_key(snapshot_id)), 1)
		self.assertTrue(_meta(snapshot_id).get("content_deleted_at"))

	def test_read_never_reloads_when_content_is_gone(self):
		snapshot_id = self.make()
		self.assertIsNone(snap.ensure_live(snapshot_id))
		frappe.cache().delete(_content_key(snapshot_id))
		with (
			patch("jarvis.tools.run_report.run_report", side_effect=AssertionError("reload")),
			patch("frappe.desk.query_report.run", side_effect=AssertionError("reload")),
			self.assertRaises(RetryLaterError) as caught,
		):
			snap.SnapshotSource(snapshot_id).read()
		self.assertEqual(str(caught.exception), CHANGED)
		self.assertEqual(_has(_content_key(snapshot_id)), 0)
		self.assertEqual(_meta(snapshot_id)["generation"], 0)

	def test_confirm_refuses_a_changed_record(self):
		snapshot_id = self.make()
		source = snap.SnapshotSource(snapshot_id)
		source.read()
		source.confirm()
		for change in (
			{"generation": 3},
			{"sha256": "1" * 64},
			{"perm_digest": "2" * 64},
			{"owner": "x@y.z"},
		):
			with self.subTest(change):
				original = _meta(snapshot_id)
				_put_meta(snapshot_id, **change)
				with self.assertRaises(RetryLaterError) as caught:
					source.confirm()
				self.assertEqual(str(caught.exception), CHANGED)
				_put_meta(snapshot_id, **{k: original[k] for k in change})
		frappe.cache().delete(_meta_key(snapshot_id))
		with self.assertRaises(RetryLaterError):
			source.confirm()

	def test_confirm_refuses_when_the_permission_inputs_changed_since_the_read(self):
		snapshot_id = self.make()
		source = snap.SnapshotSource(snapshot_id)
		source.read()
		digest.forget_perm_digest()
		with (
			patch.object(frappe, "get_roles", return_value=["System Manager", "Another Role"]),
			self.assertRaises(RetryLaterError) as caught,
		):
			source.confirm()
		self.assertEqual(str(caught.exception), CHANGED)

	def test_confirm_recomputes_the_digest_instead_of_reusing_the_memo_of_the_call(self):
		snapshot_id = self.make()
		source = snap.SnapshotSource(snapshot_id)
		source.read()  # the digest of the call is now memoised
		with (
			patch.object(frappe, "get_roles", return_value=["System Manager", "Another Role"]),
			self.assertRaises(RetryLaterError) as caught,
		):
			source.confirm()  # no forget_perm_digest(): confirm must look again
		self.assertEqual(str(caught.exception), CHANGED)

	def test_a_delegate_snapshot_config_run_cannot_read_a_snapshot_either(self):
		from jarvis.exceptions import DelegateReportRefused

		snapshot_id = self.make()
		with (
			patch(
				"jarvis.tools._delegate_capability.snapshot_config_report_refusal",
				return_value="delegate refusal text",
			),
			patch("jarvis.telemetry.record_snapshot_event") as event,
			self.assertRaises(DelegateReportRefused) as caught,
		):
			self.query(snapshot_id)
		self.assertEqual(str(caught.exception), "delegate refusal text")
		self.assertIsInstance(caught.exception, PermissionDeniedError)
		reads = [c for c in event.call_args_list if c.args[0] == "read"]
		self.assertEqual([(c.args[1], c.kwargs.get("reason")) for c in reads], [("refused", "delegate")])
		self.assertEqual(self.query(snapshot_id)["prepared_report"], snapshot_id)  # not delegated: reads

	def test_the_receipt_filters_follow_the_one_shared_policy(self):
		from jarvis.tools._report_query_source import RECEIPT_SCOPE_KEYS, receipt_filters, scope_digest

		filters = {"company": "A", "report_date": "2026-01-01", "customer": "C1", "secret": "x"}
		self.assertEqual(receipt_filters(filters, {"masked_columns": [], "denied_columns": []}), filters)
		for context in ({"masked_columns": ["c1"]}, {"denied_columns": ["c2"]}):
			restricted = receipt_filters(filters, context)
			self.assertEqual(set(restricted), {"company", "report_date"})
			self.assertTrue(set(restricted) <= RECEIPT_SCOPE_KEYS)
		self.assertEqual(scope_digest(filters), scope_digest(dict(reversed(filters.items()))))
		self.assertNotEqual(scope_digest(filters), scope_digest({}))

	def test_kill_switch_blocks_an_existing_id_before_any_redis_access(self):
		snapshot_id = self.make()
		with (
			patch.dict(frappe.conf, {"jarvis_report_snapshots": 0}),
			patch.object(type(frappe.cache()), "get", side_effect=AssertionError("redis read")),
			patch.object(type(frappe.cache()), "pipeline", side_effect=AssertionError("redis read")),
		):
			for call in (lambda: self.query(snapshot_id), lambda: snap.ensure_live(snapshot_id)):
				with self.assertRaises(InvalidArgumentError) as caught:
					call()
				self.assertEqual(str(caught.exception), NOT_AVAILABLE)
			with self.assertRaises(InvalidArgumentError):
				snap.SnapshotSource(snapshot_id)

	def test_ensure_live_runs_before_the_deadline_starts(self):
		order = []
		snapshot_id = self.make()
		from jarvis.tools import query_report as module
		from jarvis.tools._report_query_process import Deadline

		def deadline(*args, **kwargs):
			order.append("Deadline")
			return Deadline(*args, **kwargs)

		real = snap.ensure_live

		def live(snapshot):
			order.append("ensure_live")
			return real(snapshot)

		with (
			patch.object(module, "Deadline", deadline),
			patch.object(snap, "ensure_live", live),
		):
			self.query(snapshot_id)
		self.assertEqual(order, ["ensure_live", "Deadline"])

	def test_the_prepared_report_branch_does_not_touch_snapshots(self):
		with (
			patch.object(snap, "ensure_live", side_effect=AssertionError("snapshot path")),
			patch.object(snap, "SnapshotSource", side_effect=AssertionError("snapshot path")),
			self.assertRaises(PermissionDeniedError),
		):
			self.query("PREP-" + uuid.uuid4().hex)

	def test_column_policy_is_applied_to_a_snapshot(self):
		snapshot_id = self.make()
		policy = ({"c1"}, {"c2"})
		with patch.object(source, "_column_policy", return_value=policy) as stub:
			result = self.query(snapshot_id)
		stub.assert_called_once()
		columns = {c["fieldname"]: c for c in result["source_columns"]}
		self.assertNotIn("amount", columns)
		self.assertTrue(columns["name"]["masked"])
		self.assertEqual(result["coverage"]["complete"], True)

	def test_a_new_policy_changes_the_version(self):
		snapshot_id = self.make()
		plain = self.query(snapshot_id)["version"]
		with patch.object(source, "_column_policy", return_value=({"c1"}, set())):
			masked = self.query(snapshot_id)["version"]
		with patch.object(source, "_column_policy", return_value=(set(), {"c1"})):
			denied = self.query(snapshot_id)["version"]
		self.assertEqual(len({plain, masked, denied}), 3)

	def test_a_masked_customer_name_masks_party_name_on_receivable(self):
		columns = [
			{"fieldname": "party_name", "label": "Party Name", "fieldtype": "Data"},
			{"fieldname": "outstanding", "label": "Outstanding", "fieldtype": "Currency"},
		]
		data = {
			"columns": columns,
			"result": [{"party_name": "Acme", "outstanding": Decimal("5.50")}],
			"skip_total_row": 0,
		}
		filters = {"company": self.company, "report_date": "2026-10-29"}
		snapshot_id = self.make(data, report_name="Accounts Receivable", filters=filters)
		from jarvis.tools import _report_query_source as source_module

		def masked(meta):
			return [frappe._dict(fieldname="customer_name")] if meta.name == "Customer" else []

		plain = {c["fieldname"]: c for c in self.query(snapshot_id)["source_columns"]}
		self.assertFalse(plain["party_name"]["masked"])
		with patch.object(source_module, "_masked_fields", side_effect=masked):
			result = self.query(snapshot_id)
		self.assertTrue({c["fieldname"]: c for c in result["source_columns"]}["party_name"]["masked"])

	def test_the_gate_refuses_receivable_without_stored_company_and_date(self):
		snapshot_id = self.make(report_name="Accounts Receivable", filters={})
		with self.assertRaises(InvalidArgumentError) as caught:
			self.query(snapshot_id)
		self.assertNotIn("Regenerate", str(caught.exception))
		self.assertNotIn("saved scope", str(caught.exception))
		self.assertIn("snapshot", str(caught.exception))

	def test_a_missing_report_gives_a_snapshot_text(self):
		snapshot_id = self.make(report_name="No Such Report " + self.tag)
		with self.assertRaises(InvalidArgumentError) as caught:
			self.query(snapshot_id)
		self.assertEqual(str(caught.exception), NOT_AVAILABLE)

	def test_a_permission_failure_in_the_gate_gives_a_snapshot_text(self):
		snapshot_id = self.make()
		from jarvis.tools import _report_query_source as source_module

		with (
			patch.object(source_module, "validate_filters_permissions", side_effect=frappe.PermissionError),
			self.assertRaises(PermissionDeniedError) as caught,
		):
			self.query(snapshot_id)
		self.assertNotIn("saved scope", str(caught.exception))
		self.assertIn("snapshot", str(caught.exception))
		self.assertEqual(_has(_content_key(snapshot_id)), 1)

	def test_mute_messages_is_restored_after_a_refused_gate(self):
		snapshot_id = self.make()
		from jarvis.tools import _report_query_source as source_module

		frappe.flags.mute_messages = False
		with (
			patch.object(source_module, "validate_filters_permissions", side_effect=frappe.PermissionError),
			self.assertRaises(PermissionDeniedError),
		):
			self.query(snapshot_id)
		self.assertFalse(frappe.flags.mute_messages)


class TestEnsureLive(SnapshotCase):
	def test_live_snapshot_returns_none_and_changes_nothing(self):
		snapshot_id = self.make()
		before = (frappe.cache().get(_meta_key(snapshot_id)), frappe.cache().get(_content_key(snapshot_id)))
		self.assertIsNone(snap.ensure_live(snapshot_id))
		after = (frappe.cache().get(_meta_key(snapshot_id)), frappe.cache().get(_content_key(snapshot_id)))
		self.assertEqual(before, after)

	def test_foreign_and_missing_have_the_same_text(self):
		snapshot_id = self.make()
		frappe.set_user(self.foreign)
		texts = []
		for target in (snapshot_id, "rs_" + "B" * 32):
			with (
				patch("jarvis.telemetry.record_snapshot_event") as event,
				self.assertRaises(InvalidArgumentError) as caught,
			):
				snap.ensure_live(target)
			texts.append((type(caught.exception), str(caught.exception)))
			self.assertEqual(event.call_args.args[:2], ("read", "not_available"))
		self.assertEqual(texts[0], texts[1])
		self.assertEqual(texts[0][1], NOT_AVAILABLE)
		self.assertEqual(_has(_content_key(snapshot_id)), 1)
		self.assertIsNone(_meta(snapshot_id).get("content_deleted_at"))

	def test_digest_change_deletes_the_content_then_is_not_available_for_now(self):
		snapshot_id = self.make()
		digest.forget_perm_digest()
		with (
			patch.object(frappe, "get_roles", return_value=["System Manager", "Other"]),
			self.assertRaises(InvalidArgumentError) as caught,
		):
			snap.ensure_live(snapshot_id)
		self.assertEqual(str(caught.exception), NOT_AVAILABLE)
		self.assertEqual(_has(_content_key(snapshot_id)), 0)
		self.assertTrue(_meta(snapshot_id).get("content_deleted_at"))

	def test_missing_content_is_not_available_for_now(self):
		snapshot_id = self.make()
		frappe.cache().delete(_content_key(snapshot_id))
		with self.assertRaises(InvalidArgumentError) as caught:
			snap.ensure_live(snapshot_id)
		self.assertEqual(str(caught.exception), NOT_AVAILABLE)

	def test_it_reads_through_one_plain_pipeline_without_the_wrapper_methods(self):
		snapshot_id = self.make()
		cache = frappe.cache()
		with (
			patch.object(type(cache), "exists", side_effect=AssertionError("banned")),
			patch.object(type(cache), "get_value", side_effect=AssertionError("banned")),
		):
			self.assertIsNone(snap.ensure_live(snapshot_id))

	def test_a_connection_error_on_the_meta_read_is_retryable_not_a_miss(self):
		snapshot_id = self.make()
		for method in ("send_packed_command", "read_response"):
			with self.subTest(method):
				with (
					_connection_fault(method) as fired,
					patch("jarvis.telemetry.record_snapshot_event") as event,
					self.assertRaises(RetryLaterError) as caught,
				):
					snap.ensure_live(snapshot_id)
				self.assertTrue(fired)
				self.assertEqual(str(caught.exception), UNAVAILABLE)
				self.assertEqual(event.call_args.args[:2], ("read", "redis_error"))
				self.assertEqual(event.call_args.kwargs["reason"], "ConnectionError")
		self.assertEqual(_has(_content_key(snapshot_id)), 1)
		self.assertEqual(_has(_meta_key(snapshot_id)), 1)

	def test_a_connection_error_during_the_read_is_retryable(self):
		snapshot_id = self.make()
		with (
			_connection_fault("read_response") as fired,
			patch("jarvis.tools.run_report.run_report", side_effect=AssertionError("reload")),
			self.assertRaises(RetryLaterError) as caught,
		):
			snap.SnapshotSource(snapshot_id).read()
		self.assertTrue(fired)
		self.assertEqual(str(caught.exception), UNAVAILABLE)


class TestReadTelemetry(SnapshotCase):
	def events(self):
		return patch("jarvis.telemetry.record_snapshot_event")

	def last(self, event):
		return event.call_args.args[:2], event.call_args.kwargs

	def test_live_read_reports_sql_executed(self):
		snapshot_id = self.make()
		with self.events() as event:
			first = self.query(snapshot_id)
		outcome, fields = self.last(event)
		self.assertEqual(outcome, ("read", "live"))
		self.assertEqual((fields["sql_executed"], fields["generation"]), (False, 0))
		self.assertEqual(fields["snapshot_id"], snapshot_id)
		with self.events() as event:
			self.query(snapshot_id, version=first["version"], sql="SELECT COUNT(*) FROM report_rows")
		self.assertTrue(self.last(event)[1]["sql_executed"])

	def test_a_gate_permission_refusal_is_refused_permission(self):
		snapshot_id = self.make()
		from jarvis.tools import _report_query_source as source_module

		with (
			self.events() as event,
			patch.object(source_module, "validate_filters_permissions", side_effect=frappe.PermissionError),
			self.assertRaises(PermissionDeniedError),
		):
			self.query(snapshot_id)
		self.assertEqual(event.call_count, 1)
		self.assertEqual(self.last(event)[0], ("read", "refused"))
		self.assertEqual(self.last(event)[1]["reason"], "permission")

	def test_a_pinned_version_mismatch_is_refused_version(self):
		snapshot_id = self.make()
		with self.events() as event, self.assertRaises(InvalidArgumentError):
			self.query(snapshot_id, version="0" * 64, sql="SELECT COUNT(*) FROM report_rows")
		self.assertEqual(event.call_count, 1)
		self.assertEqual(self.last(event)[0], ("read", "refused"))
		self.assertEqual(self.last(event)[1]["reason"], "version")

	def test_other_failures_at_the_boundary_are_error_with_the_class(self):
		snapshot_id = self.make()
		with (
			self.events() as event,
			patch("jarvis.tools.query_report.analyze_report", side_effect=InvalidArgumentError("deadline")),
			self.assertRaises(InvalidArgumentError),
		):
			self.query(snapshot_id)
		self.assertEqual(event.call_count, 1)
		self.assertEqual(self.last(event)[0], ("read", "error"))
		self.assertEqual(self.last(event)[1]["reason"], "InvalidArgumentError")

	def test_one_read_event_per_call_on_every_path(self):
		snapshot_id = self.make()
		with self.events() as event:
			self.query(snapshot_id)
		self.assertEqual(event.call_count, 1)
		with self.events() as event, self.assertRaises(InvalidArgumentError):
			self.query("rs_" + "D" * 32)
		self.assertEqual(event.call_count, 1)

	def test_missing_id_is_not_available(self):
		with self.events() as event, self.assertRaises(InvalidArgumentError):
			self.query("rs_" + "C" * 32)
		self.assertEqual(self.last(event)[0], ("read", "not_available"))

	def test_changed_digest_in_read_is_changed(self):
		snapshot_id = self.make()
		digest.forget_perm_digest()
		with (
			self.events() as event,
			patch.object(frappe, "get_roles", return_value=["System Manager", "Other"]),
			self.assertRaises(RetryLaterError),
		):
			snap.SnapshotSource(snapshot_id).read()
		self.assertEqual(self.last(event)[0], ("read", "changed"))

	def evicted_flag(self, **meta):
		"""The `evicted` field of the one `read` event of a call whose rows are gone. The
		outcome stays `not_available`: eviction is a field, not an outcome."""
		snapshot_id = self.make()
		frappe.cache().delete(_content_key(snapshot_id))
		if meta:
			_put_meta(snapshot_id, **meta)
		with self.events() as event, self.assertRaises(InvalidArgumentError):
			snap.ensure_live(snapshot_id)
		reads = [c for c in event.call_args_list if c.args[0] == "read"]
		self.assertEqual([c.args[1] for c in reads], ["not_available"])
		return reads[0].kwargs.get("evicted", False)

	def test_content_gone_inside_the_hour_is_evicted(self):
		self.assertIs(self.evicted_flag(), True)

	def test_the_age_clock_is_reloaded_ts_when_set(self):
		old = time.time() - 5 * 3600
		self.assertIs(self.evicted_flag(created_ts=old), False)
		self.assertIs(self.evicted_flag(created_ts=old, reloaded_ts=time.time(), generation=1), True)

	def test_content_gone_after_the_hour_is_not_evicted(self):
		self.assertIs(self.evicted_flag(created_ts=time.time() - 61 * 60), False)

	def test_the_age_is_epoch_math_not_site_clock_text(self):
		# A DST change moves the site-time text by an hour; the epoch float does not move.
		skewed = str(frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2))
		self.assertIs(self.evicted_flag(created_at=skewed), True)
		recent = str(frappe.utils.now_datetime())
		self.assertIs(self.evicted_flag(created_at=recent, created_ts=time.time() - 2 * 3600), False)

	def test_a_record_without_an_epoch_is_never_evicted(self):
		self.assertIs(self.evicted_flag(created_ts=None), False)

	def test_a_delete_by_jarvis_is_not_evicted(self):
		self.assertIs(self.evicted_flag(content_deleted_at=str(frappe.utils.now())), False)


class TestFailureLog(SnapshotCase):
	"""A snapshot fault is a line in the INFO-pinned snapshot log: reason code and exception
	class only, and it survives the production default log level (ERROR)."""

	def capture(self):
		name = "jarvis.report_snapshot-" + str(frappe.local.site)
		frappe.loggers.pop(name, None)
		logging.getLogger(name).setLevel(logging.NOTSET)
		logger = frappe.logger("jarvis.report_snapshot", max_size=1_000_000, file_count=30)
		raw = []

		class Capture(logging.Handler):
			def emit(handler, record):
				raw.append((record.levelno, record.getMessage()))

		handler = Capture()
		logger.addHandler(handler)
		self.addCleanup(logger.removeHandler, handler)
		return raw

	def test_a_failed_delete_is_written_at_the_production_log_level_with_the_class_only(self):
		raw = self.capture()
		secret = "SECRET-FILTER-VALUE"
		with (
			patch.object(frappe, "log_level", None),
			patch.object(
				type(frappe.cache()), "delete", side_effect=redis.exceptions.ConnectionError(secret)
			),
		):
			store.delete_keys("rs_" + "Q" * 32)
		self.assertEqual(len(raw), 1)
		level, text = raw[0]
		self.assertGreaterEqual(level, logging.WARNING)
		line = json.loads(text)
		self.assertEqual(
			(line["kind"], line["event"], line["outcome"], line["error"]),
			("report_snapshot", "failure", "delete_error", "ConnectionError"),
		)
		self.assertNotIn(secret, text)

	def test_a_redis_fault_on_read_is_a_log_line_and_an_event_with_the_class(self):
		snapshot_id = self.make()
		raw = self.capture()
		with (
			patch.object(frappe, "log_level", None),
			_connection_fault("send_packed_command") as fired,
			self.assertRaises(RetryLaterError),
		):
			snap.ensure_live(snapshot_id)
		self.assertTrue(fired)
		lines = [json.loads(text) for _, text in raw]
		failures = [x for x in lines if x["event"] == "failure"]
		self.assertEqual(
			[(x["outcome"], x["error"]) for x in failures], [("read_redis_error", "ConnectionError")]
		)


class TestPersistentKeys(SnapshotCase):
	"""The 5-minute watchdog clears the whole site cache; only `persistent_cache_keys` survive it."""

	def families(self, snapshot_id):
		owner = "Administrator"
		return {
			"content": store.CONTENT_PREFIX + snapshot_id,
			"meta": store.META_PREFIX + snapshot_id,
			"index": store.INDEX_PREFIX + owner,
			"reload lock": reload_module.LOCK_PREFIX + owner,
			"reload rate": reload_module.RATE_PREFIX + owner,
		}

	def test_every_key_family_is_covered_by_a_persistent_prefix(self):
		persistent = frappe.get_hooks("persistent_cache_keys")
		for family, name in self.families("rs_" + "P" * 32).items():
			self.assertTrue(any(name.startswith(prefix) for prefix in persistent), family)

	def test_a_full_cache_clear_leaves_every_family_and_the_snapshot_readable(self):
		snapshot_id = self.make()
		cache = frappe.cache()
		families = self.families(snapshot_id)
		for name in (families["reload lock"], families["reload rate"]):
			cache.set(cache.make_key(name), "1", ex=300)
		frappe.clear_cache()
		for family, name in families.items():
			self.assertTrue(_has(cache.make_key(name)), family)
		self.assertEqual(self.query(snapshot_id)["prepared_report"], snapshot_id)


class TestSnapshotEventLine(FrappeTestCase):
	def setUp(self):
		from jarvis import telemetry

		self.telemetry = telemetry
		self.logger = frappe.logger("jarvis.report_snapshot", max_size=1_000_000, file_count=30)
		self.lines = []

		class Capture(logging.Handler):
			def emit(handler, record):
				self.lines.append(record.getMessage())

		self.handler = Capture()
		self.logger.addHandler(self.handler)

	def tearDown(self):
		self.logger.removeHandler(self.handler)

	def test_the_line_has_hashes_and_never_the_raw_id(self):
		snapshot_id = "rs_" + "Z" * 32
		with patch.object(self.telemetry, "_snapshot_conversation", return_value="CONV-1"):
			self.telemetry.record_snapshot_event(
				"read", "live", snapshot_id=snapshot_id, generation=2, sql_executed=True
			)
		line = json.loads(self.lines[-1])
		self.assertEqual(line["kind"], "report_snapshot")
		self.assertEqual((line["event"], line["outcome"]), ("read", "live"))
		self.assertEqual(line["snapshot_hash"], hashlib.sha256(snapshot_id.encode()).hexdigest()[:12])
		self.assertEqual(line["user_hash"], self.telemetry._user_hash(frappe.session.user))
		self.assertEqual(
			(line["generation"], line["sql_executed"], line["conversation"]), (2, True, "CONV-1")
		)
		self.assertNotIn(snapshot_id, self.lines[-1])

	def test_it_never_raises(self):
		with patch("jarvis.telemetry.frappe.logger", side_effect=RuntimeError("boom")):
			self.telemetry.record_snapshot_event("read", "live")

	def test_the_logger_is_pinned_to_info_without_a_log_level(self):
		name = "jarvis.report_snapshot-" + str(frappe.local.site)
		frappe.loggers.pop(name, None)
		logging.getLogger(name).setLevel(logging.NOTSET)
		with patch.object(frappe, "log_level", None):
			self.telemetry.record_snapshot_event("read", "live")
			self.assertEqual(frappe.logger("jarvis.report_snapshot").level, logging.INFO)
