"""Storage levers of 821-A (plan 3.11, tests 19-22): proofs hash, day/bucket index, default-order rows."""

import json
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from jarvis.payment_calendar import access, api, detail, store
from jarvis.payment_calendar.errors import ScopeChanged, SnapshotExpired
from jarvis.tests import payment_calendar_golden as golden
from jarvis.tests import test_payment_calendar_read_path as read_path
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site
from jarvis.tests.payment_calendar_legacy import legacy_page
from jarvis.tests.payment_calendar_results import SCOPE, obligation, schema2_result

SIDES = ("Payable", "Receivable")
SORTS = [(sort, order) for sort in ("remaining", "due_date", "party") for order in ("asc", "desc")]
MONTHS = ("2026-10", "2026-11", "2026-12")


def side_buckets(side):
	return [b for b in detail.BUCKETS if side == "Payable" or b not in detail.PAYABLE_ONLY_BUCKETS]


class TestProofsStayOutOfTheParts(TestCase):
	"""Test 19, store level: one proof hash per side, field = row id."""

	def setUp(self):
		require_calendar_test_site()
		self.actor = f"levers-{uuid4().hex}@example.com"
		self.scope = {"actor": self.actor, "company": "Levers", "accounts": [], **SCOPE}

	def tearDown(self):
		store.purge_actor(self.actor)

	def publish(self, result):
		sid, _ = store.admit(self.actor, uuid4().hex * 2, self.scope, "fingerprint")
		self.assertTrue(store.mark_running(self.actor, sid))
		self.assertTrue(store.publish_snapshot(self.actor, sid, store.split_result(result, SCOPE)))
		return sid

	def hash_key(self, sid, kind, side):
		return store._key(self.actor, sid, f"{kind}-{side.lower()}")

	def test_no_stored_side_row_has_a_proof_and_the_hash_holds_one_per_row(self):
		sid = self.publish(schema2_result())
		for side in SIDES:
			with self.subTest(side=side):
				part = store.load_part(self.actor, sid, side.lower())
				self.assertTrue(part["rows"])
				self.assertFalse([row for row in part["rows"] if "_permission" in row])
				self.assertNotIn("_permission", json.dumps(part))
				key = self.hash_key(sid, "proofs", side)
				self.assertEqual(frappe.cache.execute_command("HLEN", key), len(part["rows"]))
				stored = json.loads(frappe.cache.execute_command("HGET", key, part["rows"][0]["id"]))
				self.assertEqual(stored["voucher_no"], part["rows"][0]["voucher_no"])

	def test_a_ten_thousand_row_side_stores_one_field_per_row_with_the_same_expiry(self):
		rows = [
			obligation("Payable", f"PINV-{n}", f"Supplier {n % 50}", due="2026-11-12") for n in range(10000)
		]
		sid = self.publish(schema2_result(payable=rows, days={}))
		key = self.hash_key(sid, "proofs", "Payable")
		self.assertEqual(frappe.cache.execute_command("HLEN", key), 10000)
		self.assertEqual(frappe.cache.execute_command("HLEN", self.hash_key(sid, "proofs", "Receivable")), 1)
		ttl = frappe.cache.execute_command("TTL", key)
		part_ttl = frappe.cache.execute_command("TTL", store._part_key(self.actor, sid, "payable"))
		self.assertLessEqual(abs(ttl - part_ttl), 1)

	def test_a_side_without_rows_has_no_proof_hash_but_an_index(self):
		sid = self.publish(schema2_result(payable=[], days={}))
		self.assertFalse(frappe.cache.execute_command("EXISTS", self.hash_key(sid, "proofs", "Payable")))
		self.assertTrue(frappe.cache.execute_command("EXISTS", self.hash_key(sid, "index", "Payable")))

	def test_a_republish_replaces_the_old_hash(self):
		result = schema2_result()
		sid = self.publish(result)
		key = self.hash_key(sid, "proofs", "Payable")
		frappe.cache.execute_command("HSET", key, "stale-field", "{}")
		self.assertEqual(frappe.cache.execute_command("HLEN", key), 2)
		parts = store.split_result(result, SCOPE)
		self.assertFalse(store.publish_snapshot(self.actor, sid, parts))  # not running any more
		self.assertEqual(frappe.cache.execute_command("HLEN", key), 2)

	def test_load_proofs_answers_what_was_asked_and_never_fills_a_missing_field(self):
		rows = [obligation("Payable", "PINV-1", "Supplier A"), obligation("Payable", "PINV-2", "Supplier B")]
		sid = self.publish(schema2_result(payable=rows))
		ids = [rows[0]["id"]]
		self.assertEqual(list(store.load_proofs(self.actor, sid, "Payable", ids)), ids)
		frappe.cache.execute_command("HDEL", self.hash_key(sid, "proofs", "Payable"), ids[0])
		with self.assertRaises(RuntimeError):
			store.load_proofs(self.actor, sid, "Payable", ids)
		store.discard(self.actor, sid)
		with self.assertRaises(ScopeChanged):  # the tombstone
			store.load_proofs(self.actor, sid, "Receivable", ids)
		with self.assertRaises(SnapshotExpired):  # another actor has no tombstone
			store.load_proofs("other@example.com", sid, "Receivable", ids)

	def test_split_refuses_duplicate_row_ids(self):
		rows = [obligation("Payable", "PINV-1", "Supplier A"), obligation("Payable", "PINV-1", "Supplier A")]
		with self.assertRaises(RuntimeError):
			store.split_result(schema2_result(payable=rows), SCOPE)

	def test_discard_purge_and_admit_cleanup_remove_every_proof_and_index_key(self):
		sid = self.publish(schema2_result())
		keys = [self.hash_key(sid, kind, side) for kind in ("proofs", "index") for side in SIDES]
		self.assertTrue(all(frappe.cache.execute_command("EXISTS", key) for key in keys))
		store.discard(self.actor, sid)
		self.assertFalse([key for key in keys if frappe.cache.execute_command("EXISTS", key)])
		sid = self.publish(schema2_result())
		store.purge_actor(self.actor)
		self.assertFalse([key for key in keys if frappe.cache.execute_command("EXISTS", key)])
		# Admit removes the leftovers of a snapshot whose status expired.
		sid = self.publish(schema2_result())
		keys = [self.hash_key(sid, kind, side) for kind in ("proofs", "index") for side in SIDES]
		frappe.cache.execute_command("DEL", store._key(self.actor, sid, "status"))
		store.admit(self.actor, uuid4().hex * 2, self.scope, "fingerprint")
		self.assertFalse([key for key in keys if frappe.cache.execute_command("EXISTS", key)])

	def test_assembling_merges_each_proof_back_and_checks_its_identity(self):
		parts = store.split_result(schema2_result(), SCOPE)
		assembled = store.assemble_result(parts)
		self.assertEqual(assembled["sources"]["Payable"]["rows"][0]["_permission"]["voucher_no"], "PINV-1")
		parts["proofs-payable"][obligation("Payable", "PINV-1", "Supplier A")["id"]]["voucher_no"] = "OTHER"
		with self.assertRaises(RuntimeError):
			store.assemble_result(parts)


class TestProofsOnTheReadPath(read_path.ReadPathCase):
	"""Test 19, API level: one HMGET of exactly the page ids; misses follow plan 3.11."""

	def spy_eval(self):
		real = frappe.cache.eval
		calls = []

		def run(script, numkeys, *args):
			calls.append(
				(
					script,
					[a.decode() if isinstance(a, bytes) else str(a) for a in args[:numkeys]],
					args[numkeys:],
				)
			)
			return real(script, numkeys, *args)

		patcher = patch.object(frappe.cache, "eval", side_effect=run)
		patcher.start()
		self.addCleanup(patcher.stop)
		return calls

	def detail_reads(self, side):
		return {
			"day": lambda sid, **kw: api.get_day_detail(sid, self.d10, side, **kw),
			"bucket": lambda sid, **kw: api.get_bucket_detail(sid, "window", side, **kw),
		}

	def proof_calls(self, calls, word):
		return [c for c in calls if word in c[0] and any("proofs-" in key for key in c[1])]

	def test_a_page_read_sends_one_hmget_of_exactly_the_page_ids(self):
		sid = self.publish()
		for side in SIDES:
			for kind, read in self.detail_reads(side).items():
				with self.subTest(side=side, read=kind):
					calls = self.spy_eval()
					response = read(sid, page_size=1)
					self.assertTrue(response["ok"], response)
					ids = [row["id"] for row in response["data"]["rows"]]
					self.assertEqual(len(ids), 1)
					hmget = self.proof_calls(calls, "HMGET")
					self.assertEqual(len(hmget), 1)
					self.assertEqual([str(a) for a in hmget[0][2]], ids)
					self.assertFalse(self.proof_calls(calls, "HGETALL"))

	def test_an_empty_page_reads_no_proofs(self):
		sid = self.publish()
		for side in SIDES:
			for kind, read in self.detail_reads(side).items():
				with self.subTest(side=side, read=kind):
					calls = self.spy_eval()
					response = read(sid, page=99)
					self.assertTrue(response["ok"], response)
					self.assertEqual(response["data"]["rows"], [])
					self.assertFalse(self.proof_calls(calls, "HMGET"))

	def test_status_upcoming_and_polls_touch_no_proofs_key(self):
		sid = self.publish()
		running, _ = store.admit(
			self.user, "e" * 64, self.scope, access.authorize_scope(self.scope)["fingerprint"]
		)
		commands, calls = self.spy_cache(), self.spy_eval()
		for read in (api.get_snapshot_status, lambda s: api.get_upcoming(s, self.fx.today)):
			self.assertTrue(read(sid)["ok"])
		api.get_snapshot_status(running)
		self.assertFalse([c for c in commands if any("proofs-" in a for a in c[1])])
		self.assertFalse([c for c in calls if any("proofs-" in key for key in c[1])])

	def drop_proofs(self, sid, side):
		frappe.cache.execute_command("DEL", store._key(self.user, sid, f"proofs-{side.lower()}"))

	def test_an_absent_hash_is_a_changed_scope_after_a_discard_and_expired_otherwise(self):
		for side in SIDES:
			for kind, read in self.detail_reads(side).items():
				with self.subTest(side=side, read=kind):
					sid = self.publish()
					self.drop_proofs(sid, side)
					response = read(sid)
					self.assertFalse(response["ok"])
					self.assertEqual(response["code"], "not_found_or_expired")
					sid = self.publish()
					self.drop_proofs(sid, side)
					frappe.cache.execute_command(
						"SET", store._key(self.user, sid, "discarded"), "1", "EX", 300
					)
					self.assertRefused(read(sid))

	def test_a_nil_field_is_an_internal_error_and_discards_nothing(self):
		for side in SIDES:
			for kind, read in self.detail_reads(side).items():
				with self.subTest(side=side, read=kind):
					sid = self.publish()
					ids = [r["id"] for r in read(sid)["data"]["rows"]]
					frappe.cache.execute_command(
						"HDEL", store._key(self.user, sid, f"proofs-{side.lower()}"), ids[0]
					)
					response = read(sid)
					self.assertEqual((response["ok"], response["code"]), (False, "internal_error"))
					self.assertKept(sid)

	def test_a_proof_of_another_row_is_an_internal_error_and_discards_nothing(self):
		sid = self.publish()
		key = store._key(self.user, sid, "proofs-payable")
		fields = frappe.cache.execute_command("HGETALL", key)
		by_voucher = {json.loads(v)["voucher_no"]: (f, v) for f, v in fields.items()}
		(first, _), (_, other) = list(by_voucher.values())[:2]
		frappe.cache.execute_command("HSET", key, first, other)
		response = api.get_bucket_detail(sid, "window", "Payable", page_size=100)
		self.assertEqual((response["ok"], response["code"]), (False, "internal_error"))
		self.assertKept(sid)


class TestIndexMatchesTheOldPath(TestCase):
	"""Test 20: every reply of the index path equals the rev-4 path, exact JSON."""

	def setUp(self):
		require_calendar_test_site()

	def requests(self, scope, result, side):
		rows = result["sources"][side]["rows"]
		days = sorted({row["due_date"] for row in rows if row["due_date"]})
		empty = [d for d in ("2026-11-24", "2026-12-31", "2026-11-02") if d not in days][:2]
		days = [d for d in days if scope["window_start"] <= d <= scope["window_end"]]
		for date in [*days, *(d for d in empty if scope["window_start"] <= d <= scope["window_end"])]:
			yield "day", date
		for bucket in side_buckets(side):
			yield "bucket", bucket

	def compare(self, scope, result, raw, label):
		status = {"scope": scope}
		for side in SIDES:
			for kind, key in self.requests(scope, raw, side):
				field = {"date": key} if kind == "day" else {"bucket": key}
				last = last_page(raw, status, kind, key, side)
				for sort, order in SORTS:
					for page in sorted({1, last}):
						for parties in (True, False):
							request = dict(
								side=side,
								page=page,
								page_size=2,
								party_page=page,
								sort=sort,
								order=order,
								parties=parties,
							)
							build = detail.day_detail if kind == "day" else detail.bucket_detail
							got = build(status, result, **field, **request)
							want = legacy_page(status, raw, kind=kind, key=key, **request)
							self.assertEqual(
								json.dumps(got, sort_keys=True),
								json.dumps(want, sort_keys=True),
								(label, side, kind, key, sort, order, page, parties),
							)

	def test_every_scenario_side_selection_sort_page_and_party_flag(self):
		for month in MONTHS:
			with self.subTest(month=month):
				scope, raw = golden.build_raw(month=month)
				_, assembled = golden.build_result(month=month)
				self.compare(scope, assembled, raw, month)

	def test_scenarios_with_terms_ties_and_net_changes(self):
		for name, rows in (
			("net_negative", golden.NET_NEGATIVE_ROWS),
			("grouped", golden.GROUPED_TERM_ROWS),
			("half", golden.STAR_HALF_ROWS),
		):
			with self.subTest(name=name):
				scope, raw = golden.build_raw(day_rows=rows)
				_, assembled = golden.build_result(day_rows=rows)
				self.compare(scope, assembled, raw, name)

	def test_an_incomplete_month_total_stays_null_and_an_empty_day_is_never_null(self):
		scope, raw = golden.build_raw()
		raw["projection"]["month_totals"] = None
		result = store.assemble_result(store.split_result(raw, scope))
		self.compare(scope, result, raw, "no totals")
		reply = detail.bucket_detail(
			{"scope": scope},
			result,
			bucket="window",
			side="Payable",
			page=1,
			page_size=5,
			party_page=1,
			sort=None,
			order=None,
		)
		self.assertIsNone(reply["total_remaining"])
		empty = detail.day_detail(
			{"scope": scope},
			result,
			date="2026-11-24",
			side="Receivable",
			page=1,
			page_size=5,
			party_page=1,
			sort=None,
			order=None,
		)
		self.assertEqual(empty["total_remaining"], "0.00")

	def test_a_missing_index_entry_is_computed_like_an_empty_selection(self):
		scope, _ = golden.build_raw()
		_, result = golden.build_result()
		source = result["sources"]["Payable"]
		for field in [f for f in source["index"] if f.startswith("day:2026-11-30")]:
			del source["index"][field]
		reply = detail.day_detail(
			{"scope": scope},
			result,
			date="2026-11-30",
			side="Payable",
			page=1,
			page_size=5,
			party_page=1,
			sort=None,
			order=None,
		)
		self.assertEqual((reply["rows"], reply["total"], reply["total_remaining"]), ([], 0, "0.00"))
		del source["index"]["bucket:window"]
		window = detail.bucket_detail(
			{"scope": scope},
			result,
			bucket="window",
			side="Payable",
			page=1,
			page_size=5,
			party_page=1,
			sort=None,
			order=None,
		)
		self.assertEqual(window["total_remaining"], result["projection"]["month_totals"]["payable"])
		self.assertEqual(window["rows"], [])


def last_page(raw, status, kind, key, side):
	rows = legacy_page(
		status,
		raw,
		kind=kind,
		key=key,
		side=side,
		page=1,
		page_size=2,
		party_page=1,
		sort=None,
		order=None,
		parties=False,
	)
	return max(1, -(-rows["total"] // 2))


class TestDefaultOrderReads(TestCase):
	"""Test 21: a default-sort read neither sorts nor filters; other sorts sort exactly the selection."""

	def setUp(self):
		require_calendar_test_site()
		self.scope, self.result = golden.build_result()
		self.status = {"scope": self.scope}
		self.args = dict(side="Payable", page=1, page_size=100, party_page=1)

	def reads(self, **sorting):
		yield (
			"day",
			lambda: detail.day_detail(self.status, self.result, date="2026-11-11", **self.args, **sorting),
		)
		yield (
			"bucket",
			lambda: detail.bucket_detail(self.status, self.result, bucket="window", **self.args, **sorting),
		)

	def test_default_sorts_call_neither_sort_rows_nor_bucket_filter(self):
		for sorting in ({"sort": None, "order": None}, {"sort": "remaining", "order": "desc"}):
			for kind, read in self.reads(**sorting):
				with self.subTest(kind=kind, sorting=sorting):
					with (
						patch.object(detail, "sort_rows", side_effect=AssertionError("sorted")),
						patch.object(detail, "bucket_filter", side_effect=AssertionError("filtered")),
					):
						self.assertTrue(read()["rows"])

	def test_other_sorts_sort_exactly_the_selection(self):
		for kind, read in self.reads(sort="party", order="asc"):
			with self.subTest(kind=kind), patch.object(detail, "sort_rows", wraps=detail.sort_rows) as spy:
				reply = read()
				self.assertEqual(spy.call_count, 1)
				self.assertEqual(
					sorted(row["id"] for row in spy.call_args.args[0]),
					sorted(row["id"] for row in reply["rows"]),
				)
				self.assertEqual(len(spy.call_args.args[0]), reply["total"])


class TestIndexShape(TestCase):
	"""Test 22: stored order, ascending positions, one day per listed dated row, totals of the selection."""

	def setUp(self):
		require_calendar_test_site()

	def parts(self, month="2026-11"):
		scope, raw = golden.build_raw(month=month)
		return scope, raw, store.split_result(raw, scope)

	def test_rows_are_stored_in_default_order_without_proofs(self):
		for month in MONTHS:
			scope, raw, parts = self.parts(month)
			for side in SIDES:
				with self.subTest(month=month, side=side):
					stored = parts[side.lower()]["rows"]
					self.assertEqual(
						[r["id"] for r in stored],
						[r["id"] for r in detail.sort_rows(raw["sources"][side]["rows"])],
					)
					self.assertEqual(
						{r["id"] for r in stored}, {r["id"] for r in raw["sources"][side]["rows"]}
					)
					self.assertFalse([r for r in stored if "_permission" in r])

	def test_positions_ascend_and_every_listed_dated_row_is_in_exactly_one_day(self):
		for month in MONTHS:
			scope, raw, parts = self.parts(month)
			for side in SIDES:
				with self.subTest(month=month, side=side):
					rows, index = parts[side.lower()]["rows"], parts[f"index-{side.lower()}"]
					for field, entry in index.items():
						self.assertEqual(entry["rows"], sorted(set(entry["rows"])), field)
					seen = [i for f, e in index.items() if f.startswith("day:") for i in e["rows"]]
					listed = [i for i, r in enumerate(rows) if r["due_date"] and detail._listed(r)]
					self.assertEqual(sorted(seen), listed)
					self.assertEqual(len(seen), len(set(seen)))

	def test_every_entry_total_equals_the_total_of_its_selection(self):
		for month in MONTHS:
			scope, raw, parts = self.parts(month)
			for side in SIDES:
				with self.subTest(month=month, side=side):
					rows, index = parts[side.lower()]["rows"], parts[f"index-{side.lower()}"]
					for field, entry in index.items():
						selection = [rows[i] for i in entry["rows"]]
						kind, key = field.split(":", 1)
						if kind == "day":
							self.assertEqual(entry["total"], detail._total(selection, raw), field)
						else:
							self.assertEqual(
								entry["total"], detail._bucket_total(key, selection, raw, side), field
							)
						self.assertEqual(entry["parties"], detail._party_rows(selection), field)

	def test_every_applicable_bucket_has_an_entry(self):
		_, _, parts = self.parts()
		for side in SIDES:
			buckets = {f.split(":", 1)[1] for f in parts[f"index-{side.lower()}"] if f.startswith("bucket:")}
			self.assertEqual(buckets, set(side_buckets(side)))

	def test_the_window_total_is_the_month_total_and_null_when_the_totals_are_incomplete(self):
		scope, raw, parts = self.parts()
		self.assertEqual(
			parts["index-payable"]["bucket:window"]["total"], raw["projection"]["month_totals"]["payable"]
		)
		raw["projection"]["month_totals"] = None
		again = store.split_result(raw, scope)
		self.assertIsNone(again["index-payable"]["bucket:window"]["total"])


class TestValidationComesBeforeAnySideRead(read_path.ReadPathCase):
	"""An invalid request answers before the side part, the index or the proofs are loaded."""

	def test_invalid_requests_load_nothing(self):
		sid = self.publish()
		bad = {
			"sort": {"sort": "bogus"},
			"order": {"order": "up"},
			"page_size": {"page_size": 101},
			"page": {"page": 0},
			"party_page": {"party_page": "x"},
			"parties": {"parties": "maybe"},
		}
		reads = {
			"day": lambda **kw: api.get_day_detail(sid, self.d10, "Payable", **kw),
			"day date": lambda **kw: api.get_day_detail(sid, "2026-02-30", "Payable", **kw),
			"bucket": lambda **kw: api.get_bucket_detail(sid, "window", "Payable", **kw),
			"bucket name": lambda **kw: api.get_bucket_detail(sid, "nope", "Payable", **kw),
			"payable-only on receivable": lambda **kw: api.get_bucket_detail(
				sid, "today", "Receivable", **kw
			),
		}
		with (
			patch.object(store, "load_part", wraps=store.load_part) as part,
			patch.object(store, "load_index_entry") as index,
			patch.object(store, "load_proofs") as proofs,
		):
			for name, read in reads.items():
				for field, kwargs in bad.items():
					if name in ("day date", "bucket name", "payable-only on receivable") and field != "sort":
						continue
					with self.subTest(read=name, bad=field):
						part.reset_mock()
						response = read(**(kwargs if name in ("day", "bucket") else {}))
						self.assertEqual((response["ok"], response["code"]), (False, "invalid_request"))
						self.assertEqual(
							[c.args[2] for c in part.call_args_list if c.args[2] != "status"], []
						)
			index.assert_not_called()
			proofs.assert_not_called()
		self.assertKept(sid)

	def test_a_gone_snapshot_still_answers_expired_before_the_flag_is_read(self):
		response = api.get_day_detail("0" * 32, self.d10, "Payable", parties="maybe")
		self.assertEqual(response["code"], "not_found_or_expired")
