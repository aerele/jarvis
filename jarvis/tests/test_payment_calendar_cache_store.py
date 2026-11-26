"""Payment Calendar S4b B3: `cache_store`, the durable claim, chunk write, read-back, swap, janitor and purge.

The store commits (it is job code), so these tests commit rows too. Every row they create belongs to a
company name that starts with `PCAL-B3-` or to the one real company they claim for, and `tearDown` deletes
all of it and commits. Nothing here commits a mode: the switch is patched. Second connections are real
(`race_harness.other_connection`).

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_store
"""

import contextlib
import inspect
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.payment_calendar import cache, cache_state, cache_status, cache_store
from jarvis.payment_calendar.cache_eligibility import read_generation
from jarvis.tests.race_harness import other_connection

HEAD = "Jarvis Payment Calendar Cache Head"
GENERATION = "Jarvis Payment Calendar Cache Generation"
ROW = "Jarvis Payment Calendar Cache Row"
BUILD = "b" * 64


def _row(**fields):
	base = {
		"kind": "open",
		"voucher_type": "Sales Invoice",
		"voucher_no": "SI-0001",
		"party_type": "Customer",
		"party": "CUST-1",
		"party_name": "Customer One",
		"account": "Debtors",
		"posting_date": date(2026, 1, 2),
		"due_date": date(2026, 2, 1),
		"original": Decimal("100.00"),
		"paid": Decimal("0.00"),
		"remaining": Decimal("100.00"),
		"permission": {"company": "x"},
		"window_due_dates": ["2026-02-01"],
	}
	return cache_store.row_tuple({**base, **fields})


def _payload(**fields):
	return {
		"voucher_doctypes": ["Sales Invoice"],
		"party_doctypes": ["Customer"],
		"permission_columns": [],
		**fields,
	}


class TestRowCodec(TestCase):
	def test_row_tuple_orders_fields_and_defaults_flags(self):
		row = cache_store.row_tuple({"voucher_no": "A", "residue": True})
		self.assertEqual(len(row), len(cache_store.ROW_FIELDS))
		fields = dict(zip(cache_store.ROW_FIELDS, row, strict=True))
		self.assertEqual((fields["voucher_no"], fields["residue"], fields["hide_in_window"]), ("A", True, 0))

	def test_unknown_field_is_refused(self):
		with self.assertRaises(ValueError):
			cache_store.row_tuple({"nope": 1})

	def test_amounts_are_exact_decimal_text(self):
		self.assertEqual(cache_store.amount_text(Decimal("1E+2")), "100")
		self.assertEqual(cache_store.amount_text("10.50"), "10.50")
		self.assertEqual(cache_store.amount_text(7), "7")
		self.assertEqual(cache_store.amount_text(Decimal("0.000000001")), "0.000000001")

	def test_float_and_bool_and_bad_text_amounts_are_refused(self):
		for bad in (1.5, True, "abc", Decimal("NaN"), Decimal("Infinity")):
			with self.assertRaises((TypeError, ValueError), msg=repr(bad)):
				cache_store.amount_text(bad)

	def test_encode_converts_dates_before_the_checksum(self):
		stored = cache_store.encode_row(_row(posting_date=datetime(2026, 1, 2, 5, 0)))
		fields = dict(zip(cache_store.ROW_FIELDS, stored["values"], strict=True))
		self.assertEqual(fields["posting_date"], "2026-01-02")
		self.assertEqual(fields["due_date"], "2026-02-01")

	def test_a_string_in_a_json_field_is_refused(self):
		with self.assertRaises(TypeError):
			cache_store.encode_row(_row(permission='{"a": 1}'))

	def test_canonical_form_is_the_same_for_a_stored_value_read_back(self):
		encoded = cache_store.encode_row(_row())
		db_like = [
			date(2026, 1, 2)
			if field == "posting_date"
			else date(2026, 2, 1)
			if field == "due_date"
			else value
			for field, value in zip(cache_store.ROW_FIELDS, encoded["values"], strict=True)
		]
		self.assertEqual(cache_store.canonical_of_stored(db_like), encoded["canon"])

	def test_empty_text_and_null_are_one_value(self):
		first = cache_store.encode_row(_row(term=""))["canon"]
		second = cache_store.encode_row(_row(term=None))["canon"]
		self.assertEqual(first, second)

	def test_row_id_depends_on_side_body_and_occurrence(self):
		rows = [_row(), _row(), _row(voucher_no="SI-0002")]
		ids = [e["row_id"] for e in cache_store.encode_rows("Receivable", rows)]
		self.assertEqual(len(set(ids)), 3)
		again = [e["row_id"] for e in cache_store.encode_rows("Receivable", rows)]
		self.assertEqual(ids, again)
		other_side = [e["row_id"] for e in cache_store.encode_rows("Payable", rows)]
		self.assertNotEqual(ids[0], other_side[0])
		self.assertEqual(len(ids[0]), 64)


class TestChecksum(TestCase):
	def _sum(self, rows, **kw):
		digest = cache_store.RowDigest()
		for encoded in cache_store.encode_rows("Receivable", rows):
			digest.add(encoded)
		args = {
			"payload": _payload(),
			"anchor": date(2026, 11, 24),
			"cache_build": BUILD,
			"company_creation": datetime(2026, 1, 1, 0, 0, 0, 5),
			"side": "Receivable",
		}
		args.update(kw)
		return cache_store.identity_checksum(digest, **args)

	def test_it_is_order_free(self):
		a, b = _row(voucher_no="A"), _row(voucher_no="B")
		self.assertEqual(self._sum([a, b]), self._sum([b, a]))

	def test_one_changed_row_field_changes_it(self):
		self.assertNotEqual(self._sum([_row()]), self._sum([_row(remaining=Decimal("99.99"))]))
		self.assertNotEqual(self._sum([_row()]), self._sum([_row(permission={"company": "y"})]))
		self.assertNotEqual(self._sum([_row()]), self._sum([_row(hide_in_window=1)]))
		self.assertNotEqual(self._sum([_row()]), self._sum([_row(window_due_dates=["2026-03-01"])]))

	def test_a_missing_or_extra_row_changes_it(self):
		self.assertNotEqual(self._sum([_row()]), self._sum([_row(), _row()]))
		self.assertNotEqual(self._sum([_row()]), self._sum([]))

	def test_identity_parts_each_change_it(self):
		base = self._sum([_row()])
		for change in (
			{"payload": _payload(currency="EUR")},
			{"anchor": date(2026, 11, 25)},
			{"cache_build": "c" * 64},
			{"company_creation": datetime(2026, 1, 1, 0, 0, 0, 6)},
			{"side": "Payable"},
		):
			self.assertNotEqual(base, self._sum([_row()], **change), change)

	def test_payload_key_order_does_not_matter(self):
		first = self._sum([_row()], payload={"a": 1, "b": 2, **_payload()})
		second = self._sum([_row()], payload={**_payload(), "b": 2, "a": 1})
		self.assertEqual(first, second)


class TestPayloadContract(TestCase):
	def test_receivable_payload_needs_the_b8_keys(self):
		cache_store.check_payload("Receivable", "Co", _payload())
		for key in ("voucher_doctypes", "party_doctypes", "permission_columns"):
			bad = _payload()
			del bad[key]
			with self.assertRaises(ValueError, msg=key):
				cache_store.check_payload("Payable", "Co", bad)

	def test_bank_payload_needs_only_an_object(self):
		cache_store.check_payload("Bank", "Co", {"accounts": []})
		with self.assertRaises(ValueError):
			cache_store.check_payload("Bank", "Co", [])

	def test_payload_must_be_json_native(self):
		with self.assertRaises(TypeError):
			cache_store.check_payload("Receivable", "Co", _payload(total=Decimal("1")))

	def test_the_stored_payload_is_what_cache_eligibility_reads(self):
		text = cache_store.payload_text(_payload())
		shape = read_generation({"company": "Co", "side": "Receivable", "payload": text}, "Co")
		self.assertEqual(shape.doctypes, frozenset({"Sales Invoice", "Customer"}))
		self.assertEqual(json.loads(text)["permission_columns"], [])


class StoreCase(FrappeTestCase):
	"""Base for the tests that write: one real company, cleanup of everything the cache tables hold."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for doctype in (HEAD, GENERATION, ROW):
			frappe.reload_doc("jarvis", "doctype", frappe.scrub(doctype))
		cls.company = frappe.get_all("Company", pluck="name", order_by="creation desc", limit=1)[0]

	def setUp(self):
		frappe.set_user("Administrator")
		self._purge_everything()
		self.addCleanup(self._purge_everything)
		self.settings = cache_state.CacheSettings("shadow", (self.company,), False)
		patcher = patch.object(
			cache_store.cache_state, "cache_settings", side_effect=lambda **_: self.settings
		)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _purge_everything(self):
		frappe.db.rollback()
		frappe.db.sql(f"delete from `tab{ROW}`")
		frappe.db.sql(f"delete from `tab{GENERATION}`")
		frappe.db.sql(f"delete from `tab{HEAD}`")
		frappe.db.commit()


ANCHOR = date(2026, 11, 24)
AS_OF = datetime(2026, 11, 24, 8, 0, 0)


@contextlib.contextmanager
def _on(db):
	"""Run store calls on another real connection (frappe.db resolves `frappe.local.db`)."""
	previous = frappe.local.db
	frappe.local.db = db
	try:
		yield
	finally:
		frappe.local.db = previous


def _sql(query, values=()):
	return frappe.db.sql(query, values)


def site_offset_s() -> int:
	"""Seconds that site time is ahead of the database clock, read without the code under test (the zones of
	`frappe.utils.now_datetime` and of the database server differ on a real site)."""
	db_now = _sql("select now(6)")[0][0]
	return round((frappe.utils.now_datetime() - db_now).total_seconds() / 900) * 900


class TestSiteClockOffset(FrappeTestCase):
	"""`cache_store.site_clock_offset_s`: the one place that turns database time into site time."""

	def offset(self, seconds: float) -> int:
		database_now = _sql("select now(6)")[0][0]
		with patch("frappe.utils.now_datetime", return_value=database_now + timedelta(seconds=seconds)):
			return cache_store.site_clock_offset_s()

	def test_a_site_ahead_of_the_database_gives_a_positive_whole_zone_offset(self):
		self.assertEqual(self.offset(5.5 * 3600 + 0.4), 19800)

	def test_a_site_behind_the_database_gives_a_negative_offset(self):
		self.assertEqual(self.offset(-3 * 3600 - 0.7), -10800)

	def test_equal_clocks_give_zero_and_a_few_seconds_of_skew_are_dropped(self):
		self.assertEqual(self.offset(0.3), 0)
		self.assertEqual(self.offset(4), 0)
		self.assertEqual(self.offset(-12), 0)

	def test_the_offset_of_this_site_is_a_multiple_of_a_quarter_hour(self):
		self.assertEqual(cache_store.site_clock_offset_s() % 900, 0)
		self.assertEqual(cache_store.site_clock_offset_s(), site_offset_s())


class SideCase(StoreCase):
	"""Helpers for a test that works on the Receivable side of the one real company."""

	side = "Receivable"

	def claim(self, **kw):
		args = {"anchor": ANCHOR, "cache_build": BUILD, "night": False, **kw}
		return cache_store.claim(self.company, self.side, **args)

	def publish(self, held, rows=None, **kw):
		rows = [_row()] if rows is None else rows
		args = {"rows": rows, "payload": _payload(), "as_of": AS_OF, **kw}
		return cache_store.publish(held, **args)

	def head(self):
		frappe.db.rollback()
		return frappe.db.sql(
			f"select current_generation, claim_token, purge_epoch, hop_seq, failures, backoff_level, last_failure, "
			f"memo_until, max_age_s_today from `tab{HEAD}` where name=%s",
			(cache_store.head_name(self.company, self.side),),
			as_dict=True,
		)[0]

	def gen(self, name):
		frappe.db.rollback()
		return frappe.db.sql(f"select * from `tab{GENERATION}` where name=%s", (name,), as_dict=True)[0]

	def rows(self, name):
		frappe.db.rollback()
		return frappe.db.sql(
			f"select * from `tab{ROW}` where generation=%s order by name", (name,), as_dict=True
		)

	def row_count(self, name):
		frappe.db.rollback()
		return frappe.db.sql(f"select count(*) from `tab{ROW}` where generation=%s", (name,))[0][0]

	def expire_claim(self):
		frappe.db.sql(
			f"update `tab{HEAD}` set claim_expires=now(6) - interval 5 second where name=%s",
			(cache_store.head_name(self.company, self.side),),
		)
		frappe.db.commit()


class TestClaim(SideCase):
	def test_a_claim_creates_the_head_and_a_building_generation(self):
		held = self.claim()
		head, gen = self.head(), self.gen(held.generation)
		self.assertEqual((head.claim_token, head.current_generation, head.purge_epoch), (held.token, None, 0))
		self.assertEqual(
			(gen.state, gen.company, gen.side, gen.cache_build),
			("building", self.company, "Receivable", BUILD),
		)
		self.assertEqual(gen.company_creation, held.company_creation)
		self.assertIsNone(held.prev)
		self.assertEqual(held.epoch, 0)

	def test_the_generation_deadline_is_the_job_timeout_plus_sixty_seconds(self):
		held = self.claim(night=True)
		wait = _sql(
			f"select timestampdiff(second, now(6), deadline) from `tab{GENERATION}` where name=%s",
			(held.generation,),
		)[0][0]
		self.assertAlmostEqual(wait, cache_state.JOB_TIMEOUT_NIGHT_S + 60, delta=5)

	def test_the_claim_lasts_the_claim_ttl(self):
		self.claim(night=True)
		left = _sql(
			f"select timestampdiff(second, now(6), claim_expires) from `tab{HEAD}` where name=%s",
			(cache_store.head_name(self.company, "Receivable"),),
		)[0][0]
		self.assertAlmostEqual(left, cache_state.claim_ttl_s(night=True), delta=5)

	def test_a_live_claim_refuses_a_second_claim(self):
		self.claim()
		with self.assertRaises(cache_store.NotClaimed) as caught:
			self.claim()
		self.assertEqual(caught.exception.reason, "busy")

	def test_a_live_claim_refuses_a_claim_from_a_second_connection(self):
		held = self.claim()
		with other_connection() as db, _on(db):
			with self.assertRaises(cache_store.NotClaimed) as caught:
				self.claim()
			self.assertEqual(caught.exception.reason, "busy")
		self.assertEqual(self.head().claim_token, held.token)

	def test_an_expired_claim_is_taken_over_and_the_old_writer_is_fenced(self):
		old = self.claim()
		self.expire_claim()
		new = self.claim()
		self.assertNotEqual(old.token, new.token)
		with self.assertRaises(cache_store.Fenced) as caught:
			self.publish(old)
		self.assertEqual(caught.exception.reason, "claim")
		self.assertEqual(self.head().claim_token, new.token)

	def test_a_company_that_does_not_exist_is_not_claimed(self):
		with self.assertRaises(cache_store.NotClaimed) as caught:
			cache_store.claim(
				"PCAL-B3-no-such-company", "Receivable", anchor=ANCHOR, cache_build=BUILD, night=False
			)
		self.assertEqual(caught.exception.reason, "company_gone")

	def test_an_unknown_side_is_refused(self):
		with self.assertRaises(ValueError):
			cache_store.claim(self.company, "Sideways", anchor=ANCHOR, cache_build=BUILD, night=False)

	def test_a_memo_refuses_a_claim_for_the_build_that_failed_and_not_for_a_new_build(self):
		held = self.claim()
		cache_store.fail(held, "history_too_large")
		cache_store.release_claim(held)
		self.assertTrue(cache_store.memo_blocks(self.company, "Receivable", BUILD))
		with self.assertRaises(cache_store.NotClaimed) as caught:
			self.claim()
		self.assertEqual(caught.exception.reason, "memo")
		self.assertFalse(cache_store.memo_blocks(self.company, "Receivable", "d" * 64))
		self.claim(cache_build="d" * 64)
		self.assertEqual((self.head().backoff_level, self.head().memo_until), (0, None))

	def test_a_case_variant_head_of_the_same_company_is_busy_not_a_crash(self):
		variant = self.company.swapcase()
		self.assertNotEqual(variant, self.company)
		creation = cache_store.company_creation(self.company)
		frappe.db.sql(
			f"insert into `tab{HEAD}` (name, creation, modified, company, side, company_creation) "
			"values ('variant-head', now(6), now(6), %s, 'Receivable', %s)",
			(variant, creation),
		)
		frappe.db.commit()
		with self.assertRaises(cache_store.NotClaimed) as caught:
			self.claim()
		self.assertEqual(caught.exception.reason, "busy")

	def test_the_generation_bound_refuses_a_claim(self):
		now = frappe.db.sql("select now(6)")[0][0]
		for _ in range(cache_store.GENERATION_BOUND):
			number = frappe.db.get_next_sequence_val(GENERATION)
			frappe.db.sql(
				f"insert into `tab{GENERATION}` (name, creation, modified, company, side, state, deadline) "
				"values (%s, %s, %s, %s, 'Receivable', 'ready', %s)",
				(number, now, now, self.company, now),
			)
		frappe.db.commit()
		with self.assertRaises(cache_store.NotClaimed) as caught:
			self.claim()
		self.assertEqual(caught.exception.reason, "generation_bound")

	def test_retired_generations_inside_the_grace_do_not_hold_a_claim_back(self):
		for _ in range(cache_store.GENERATION_BOUND + 2):
			held = self.claim()
			self.publish(held, [_row(voucher_no=f"SI-{held.generation}")])
		retired = _sql(
			f"select count(*) from `tab{GENERATION}` g where g.state='retired' and exists "
			f"(select 1 from `tab{ROW}` r where r.generation=g.name)"
		)[0][0]
		self.assertGreaterEqual(retired, cache_store.GENERATION_BOUND)

	def test_a_reused_company_name_resets_the_head_and_purges_its_generations(self):
		first = self.claim()
		cache_store.release_claim(first)
		name = cache_store.head_name(self.company, "Receivable")
		frappe.db.sql(f"update `tab{HEAD}` set company_creation='2001-01-01 00:00:00' where name=%s", (name,))
		frappe.db.commit()
		with patch.object(frappe, "enqueue") as enqueue, patch.object(cache_store, "janitor"):
			second = self.claim()
		self.assertEqual(second.epoch, 1)
		self.assertEqual(self.gen(first.generation).state, "purged")
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(self.head().purge_epoch, 1)


class TestPublish(SideCase):
	def test_publish_writes_rows_and_swaps_the_generation_in(self):
		held = self.claim()
		rows = [_row(voucher_no="SI-1"), _row(voucher_no="SI-2", remaining=Decimal("0.10"), residue=1)]
		done = self.publish(held, rows)
		self.assertEqual((done.outcome, done.generation, done.rows), ("swapped", held.generation, 2))
		head, gen = self.head(), self.gen(held.generation)
		self.assertEqual(head.current_generation, held.generation)
		self.assertEqual(
			(gen.state, gen.outcome, gen.row_count, gen.residue_count), ("current", "swapped", 2, 1)
		)
		self.assertEqual(gen.as_of, AS_OF)
		self.assertEqual(len(gen.checksum), 64)

	def test_rows_are_stored_exact_with_the_documented_names(self):
		held = self.claim()
		self.publish(held, [_row(voucher_no="SI-1"), _row(voucher_no="SI-2", remaining=Decimal("12.3400"))])
		first, second = self.rows(held.generation)
		self.assertEqual(first.name, f"{held.generation:012d}00000000")
		self.assertEqual(second.name, f"{held.generation:012d}00000001")
		self.assertEqual((second.remaining, second.original), ("12.3400", "100.00"))
		self.assertEqual((second.posting_date, second.due_date), (date(2026, 1, 2), date(2026, 2, 1)))
		self.assertEqual(json.loads(second.permission), {"company": "x"})
		self.assertEqual(json.loads(second.window_due_dates), ["2026-02-01"])
		self.assertEqual(first.generation, held.generation)
		self.assertEqual(len(first.row_id), 64)

	def test_the_stored_generation_is_what_cache_eligibility_reads(self):
		held = self.claim()
		self.publish(held)
		as_dict = frappe.get_doc(GENERATION, held.generation).as_dict()
		shape = read_generation(as_dict, self.company)
		self.assertEqual(shape.doctypes, frozenset({"Sales Invoice", "Customer"}))
		self.assertEqual(shape.side, "Receivable")

	def test_a_bad_payload_is_refused_before_any_write_and_marks_the_generation_failed(self):
		held = self.claim()
		with self.assertRaises(ValueError):
			self.publish(held, payload={"voucher_doctypes": []})
		gen = self.gen(held.generation)
		self.assertEqual((gen.state, gen.failure_class), ("failed", "error:ValueError"))
		self.assertEqual(self.row_count(held.generation), 0)

	def test_a_second_publish_retires_the_previous_generation(self):
		first = self.claim()
		self.publish(first, keep_claim=True)
		second = cache_store.next_generation(first)
		self.assertEqual(second.prev, first.generation)
		self.publish(second, [_row(voucher_no="SI-9")])
		self.assertEqual(self.gen(first.generation).state, "retired")
		self.assertEqual(self.gen(second.generation).state, "current")
		self.assertEqual(self.head().current_generation, second.generation)

	def test_a_stopping_job_clears_the_claim_and_counts_a_hop(self):
		held = self.claim()
		done = self.publish(held)
		head = self.head()
		self.assertEqual((head.claim_token, head.hop_seq, done.hop_seq, done.kept_claim), (None, 1, 1, False))

	def test_a_looping_job_keeps_and_renews_its_claim(self):
		held = self.claim()
		frappe.db.sql(
			f"update `tab{HEAD}` set claim_expires=now(6) + interval 5 second where name=%s",
			(cache_store.head_name(self.company, "Receivable"),),
		)
		frappe.db.commit()
		done = self.publish(held, keep_claim=True)
		head = self.head()
		self.assertEqual(
			(head.claim_token, head.hop_seq, done.hop_seq, done.kept_claim), (held.token, 0, None, True)
		)
		left = _sql(
			f"select timestampdiff(second, now(6), claim_expires) from `tab{HEAD}` where name=%s",
			(cache_store.head_name(self.company, "Receivable"),),
		)[0][0]
		self.assertGreater(left, 100)

	def test_each_chunk_renews_the_claim(self):
		held = self.claim()
		name = cache_store.head_name(self.company, "Receivable")
		lefts = []

		def shorten_then_record(deadline_at):
			# Runs between chunk transactions: what the last chunk left is recorded, then the claim is made short.
			lefts.append(_sql(f"select timestampdiff(second, now(6), claim_expires) from `tab{HEAD}`")[0][0])
			frappe.db.sql(
				f"update `tab{HEAD}` set claim_expires=now(6) + interval 5 second where name=%s", (name,)
			)
			frappe.db.commit()

		with (
			patch.object(cache_store, "CHUNK_ROWS", 2),
			patch.object(cache_store, "_check_deadline", shorten_then_record),
		):
			self.publish(held, [_row(voucher_no=f"SI-{n}") for n in range(5)])
		self.assertGreater(len(lefts), 3)
		self.assertTrue(all(left > 100 for left in lefts[1:4]), lefts)
		self.assertEqual(self.row_count(held.generation), 5)

	def test_the_age_and_the_max_age_of_the_day_are_recorded(self):
		first = self.claim()
		self.publish(first, keep_claim=True, as_of=datetime.now().replace(microsecond=0))
		frappe.db.sql(
			f"update `tab{GENERATION}` set as_of=now(6) - interval 700 second where name=%s",
			(first.generation,),
		)
		frappe.db.commit()
		second = cache_store.next_generation(first)
		done = self.publish(second, [_row(voucher_no="other")])
		self.assertAlmostEqual(done.age_s, 700, delta=5)
		self.assertAlmostEqual(self.head().max_age_s_today, 700, delta=5)


class TestNoChangeSkip(SideCase):
	def _current(self, rows=None, payload=None, **claim_kw):
		first = self.claim(**claim_kw)
		self.publish(first, rows, payload=payload or _payload(), keep_claim=True)
		return first

	def test_equal_identity_writes_no_rows_and_advances_as_of(self):
		first = self._current()
		second = cache_store.next_generation(first)
		later = AS_OF.replace(hour=9)
		with patch.object(cache_store, "_insert_rows") as insert:
			done = self.publish(second, as_of=later)
		insert.assert_not_called()
		self.assertEqual((done.outcome, done.generation), ("unchanged", first.generation))
		self.assertEqual(self.head().current_generation, first.generation)
		self.assertEqual(self.gen(first.generation).as_of, later)
		self.assertEqual(self.gen(first.generation).state, "current")
		evidence = self.gen(second.generation)
		self.assertEqual((evidence.state, evidence.outcome, evidence.row_count), ("retired", "unchanged", 1))
		self.assertEqual(self.row_count(second.generation), 0)

	def test_a_skip_leaves_the_stored_rows_untouched(self):
		first = self._current()
		before = self.rows(first.generation)
		self.publish(cache_store.next_generation(first), as_of=AS_OF.replace(hour=9))
		self.assertEqual(self.rows(first.generation), before)

	def test_a_changed_row_a_payload_flag_a_new_day_or_a_build_each_write_a_new_generation(self):
		for label, rows, payload, claim_kw in (
			("row", [_row(remaining=Decimal("99.00"))], None, {}),
			("payload", None, _payload(book_flag=True), {}),
			("anchor", None, None, {"anchor": date(2026, 11, 25)}),
			("build", None, None, {"cache_build": "e" * 64}),
		):
			with self.subTest(label):
				self._purge_everything()
				first = self._current()
				second = cache_store.next_generation(first, **claim_kw)
				done = self.publish(second, rows, payload=payload or _payload())
				self.assertEqual(done.outcome, "swapped")
				self.assertEqual(self.row_count(second.generation), 1)

	def test_a_new_company_creation_is_a_new_generation(self):
		first = self._current()
		changed = replace_claim(first, company_creation=datetime(2020, 1, 1))
		with patch.object(cache_store, "company_creation", return_value=changed.company_creation):
			with patch.object(
				cache_store, "_check_world"
			):  # the world check is tested on its own; here only the identity matters
				frappe.db.sql(f"update `tab{HEAD}` set company_creation=%s", (changed.company_creation,))
				frappe.db.commit()
				second = cache_store.next_generation(changed)
				done = self.publish(second)
		self.assertEqual(done.outcome, "swapped")


def replace_claim(held, **fields):
	import dataclasses

	return dataclasses.replace(held, **fields)


class TestFence(SideCase):
	def test_a_purge_between_chunks_fences_the_writer_and_deletes_its_rows(self):
		held = self.claim()
		checks = []

		def purge_before_second_chunk(deadline_at):
			checks.append(1)
			if len(checks) == 2:  # the check that precedes chunk 2 runs between two chunk transactions
				with other_connection() as db, _on(db):
					cache_store.bump_epoch(self.company)
					frappe.db.commit()

		with (
			patch.object(cache_store, "CHUNK_ROWS", 2),
			patch.object(cache_store, "_check_deadline", purge_before_second_chunk),
			self.assertRaises(cache_store.Fenced) as caught,
		):
			self.publish(held, [_row(voucher_no=f"SI-{n}") for n in range(6)])
		self.assertEqual(caught.exception.reason, "epoch")
		self.assertEqual(len(checks), 2)
		self.assertEqual(self.row_count(held.generation), 0)
		self.assertEqual((self.head().current_generation, self.head().purge_epoch), (None, 1))
		self.assertEqual(self.gen(held.generation).state, "purged")

	def test_a_purge_that_lands_after_the_read_back_stops_the_swap(self):
		held = self.claim()
		real = cache_store._read_back

		def purge_then_check(claim_, expected, deadline_at):
			real(claim_, expected, deadline_at)
			with other_connection() as db, _on(db):
				cache_store.bump_epoch(self.company)
				frappe.db.commit()

		with patch.object(cache_store, "_read_back", purge_then_check):
			with self.assertRaises(cache_store.Fenced):
				self.publish(held)
		self.assertIsNone(self.head().current_generation)

	def test_a_stale_prev_is_fenced_by_the_compare_and_swap(self):
		held = self.claim()
		frappe.db.sql(f"update `tab{HEAD}` set current_generation=999999")
		frappe.db.commit()
		with self.assertRaises(cache_store.Fenced) as caught:
			self.publish(held)
		self.assertEqual(caught.exception.reason, "cas")
		self.assertEqual(self.head().current_generation, 999999)

	def test_the_head_deleted_under_a_writer_fences_it(self):
		held = self.claim()
		frappe.db.sql(f"delete from `tab{HEAD}`")
		frappe.db.commit()
		with self.assertRaises(cache_store.Fenced) as caught:
			self.publish(held)
		self.assertEqual(caught.exception.reason, "head_gone")

	def test_a_generation_that_is_no_longer_building_is_not_swapped(self):
		held = self.claim()
		real = cache_store._read_back

		def spoil(claim_, expected, deadline_at):
			real(claim_, expected, deadline_at)
			frappe.db.sql(f"update `tab{GENERATION}` set state='purged' where name=%s", (claim_.generation,))
			frappe.db.commit()

		with patch.object(cache_store, "_read_back", spoil):
			with self.assertRaises(cache_store.Fenced) as caught:
				self.publish(held)
		self.assertEqual(caught.exception.reason, "generation")

	def test_an_other_row_count_than_expected_is_not_swapped(self):
		held = self.claim()
		real = cache_store._read_back

		def spoil(claim_, expected, deadline_at):
			real(claim_, expected, deadline_at)
			frappe.db.sql(f"update `tab{GENERATION}` set row_count=77 where name=%s", (claim_.generation,))
			frappe.db.commit()

		with patch.object(cache_store, "_read_back", spoil):
			with self.assertRaises(cache_store.Fenced):
				self.publish(held)
		self.assertIsNone(self.head().current_generation)


class TestSwapChecks(SideCase):
	def _refused(self, expected_reason):
		held = self.claim()
		with self.assertRaises(cache_store.Fenced) as caught:
			self.publish(held)
		self.assertEqual(caught.exception.reason, expected_reason)
		self.assertIsNone(self.head().current_generation)
		self.assertEqual(self.gen(held.generation).state, "failed")

	def test_mode_off_stops_the_swap(self):
		self.settings = cache_state.CacheSettings("off", (self.company,), False)
		self._refused("mode")

	def test_a_company_that_left_the_list_stops_the_swap(self):
		self.settings = cache_state.CacheSettings("shadow", (), False)
		self._refused("unlisted")

	def test_mode_on_is_a_shadow_for_the_store(self):
		self.settings = cache_state.CacheSettings("on", (self.company,), False)
		held = self.claim()
		self.assertEqual(self.publish(held).outcome, "swapped")

	def test_a_company_with_another_creation_stops_the_swap(self):
		held = self.claim()
		with patch.object(cache_store, "company_creation", return_value=datetime(2020, 1, 1)):
			with self.assertRaises(cache_store.Fenced) as caught:
				self.publish(held)
		self.assertEqual(caught.exception.reason, "company_changed")

	def test_the_hook_check_runs_inside_the_swap_and_stops_it(self):
		held = self.claim()
		seen = []

		def verify():
			seen.append(
				_sql(
					f"select claim_token from `tab{HEAD}` where name=%s for update",
					(cache_store.head_name(self.company, "Receivable"),),
				)
			)
			return "not_buildable:query_hook"

		with self.assertRaises(cache_store.NotBuildable) as caught:
			self.publish(held, verify=verify)
		self.assertEqual(caught.exception.klass, "not_buildable:query_hook")
		self.assertEqual(len(seen), 1)
		self.assertIsNone(self.head().current_generation)
		self.assertEqual(self.gen(held.generation).failure_class, "not_buildable:query_hook")

	def test_a_passing_hook_check_lets_the_swap_through(self):
		held = self.claim()
		self.assertEqual(self.publish(held, verify=lambda: None).outcome, "swapped")


class TestSwapReadsTheWorldAfterTheReadBack(SideCase):
	def test_the_swap_checks_run_in_a_fresh_snapshot(self):
		held = self.claim()
		real = cache_store._read_back
		seen = []

		def change_then_read_back(claim_, expected, deadline_at):
			real(claim_, expected, deadline_at)
			with other_connection() as db:
				db.sql(f"update `tab{HEAD}` set failures=7")
				db.commit()

		def spy(claim_, head):
			seen.append(_sql(f"select failures from `tab{HEAD}`")[0][0])

		with (
			patch.object(cache_store, "_read_back", change_then_read_back),
			patch.object(cache_store, "_check_world", spy),
		):
			self.publish(held)
		self.assertEqual(seen, [7])

	def test_rows_missing_at_the_swap_stop_it_whatever_the_generation_row_says(self):
		held = self.claim()
		real = cache_store._read_back

		def lose_a_row(claim_, expected, deadline_at):
			real(claim_, expected, deadline_at)
			frappe.db.sql(f"delete from `tab{ROW}` where generation=%s limit 1", (claim_.generation,))
			frappe.db.commit()

		with (
			patch.object(cache_store, "_read_back", lose_a_row),
			self.assertRaises(cache_store.Fenced) as caught,
		):
			self.publish(held, [_row(voucher_no="A"), _row(voucher_no="B")])
		self.assertEqual(caught.exception.reason, "rows")
		self.assertIsNone(self.head().current_generation)
		self.assertEqual(self.row_count(held.generation), 0)


class TestReadBack(SideCase):
	def _corrupt(self, sql):
		real = cache_store._write_chunks

		def corrupt(claim_, rows, deadline_at):
			slowest = real(claim_, rows, deadline_at)
			frappe.db.sql(sql, (claim_.generation,))
			frappe.db.commit()
			return slowest

		return patch.object(cache_store, "_write_chunks", corrupt)

	def _no_swap(self, sql):
		held = self.claim()
		with self._corrupt(sql), self.assertRaises(cache_store.ReadbackFailed):
			self.publish(held, [_row(voucher_no="A"), _row(voucher_no="B")])
		self.assertIsNone(self.head().current_generation)
		gen = self.gen(held.generation)
		self.assertEqual((gen.state, gen.failure_class), ("failed", "readback"))
		self.assertEqual(self.row_count(held.generation), 0)

	def test_a_changed_amount_is_caught(self):
		self._no_swap(f"update `tab{ROW}` set remaining='99.99' where generation=%s limit 1")

	def test_a_lost_row_is_caught(self):
		self._no_swap(f"delete from `tab{ROW}` where generation=%s limit 1")

	def test_an_extra_row_is_caught(self):
		self._no_swap(
			f"insert into `tab{ROW}` (name, generation, voucher_no, row_id) select concat(name, 'x'), generation, 'zz', 'zz' "
			f"from `tab{ROW}` where generation=%s limit 1"
		)

	def test_a_changed_permission_or_flag_or_date_is_caught(self):
		for column in ('permission=\'{"company": "y"}\'', "hide_in_window=1", "due_date='2030-01-01'"):
			with self.subTest(column):
				self._purge_everything()
				self._no_swap(f"update `tab{ROW}` set {column} where generation=%s limit 1")

	def test_a_changed_stored_payload_or_anchor_is_caught(self):
		for column in ("payload='{\"x\": 1}'", "anchor='2020-01-01'", "cache_build='z'"):
			with self.subTest(column):
				self._purge_everything()
				held = self.claim()
				real = cache_store._write_chunks

				def corrupt(claim_, rows, deadline_at, real=real, column=column):
					slowest = real(claim_, rows, deadline_at)
					frappe.db.sql(
						f"update `tab{GENERATION}` set {column} where name=%s", (claim_.generation,)
					)
					frappe.db.commit()
					return slowest

				with (
					patch.object(cache_store, "_write_chunks", corrupt),
					self.assertRaises(cache_store.ReadbackFailed),
				):
					self.publish(held)
				self.assertIsNone(self.head().current_generation)

	def test_a_clean_read_back_passes_with_non_ascii_text_and_zero_rows(self):
		held = self.claim()
		self.assertEqual(self.publish(held, [_row(party_name='Zoë "Ünï" \\ O\'Neil 😀')]).outcome, "swapped")
		self.assertEqual(self.rows(held.generation)[0].party_name, 'Zoë "Ünï" \\ O\'Neil 😀')
		self._purge_everything()
		again = self.claim()
		self.assertEqual(self.publish(again, []).rows, 0)


class TestWriteDeadline(SideCase):
	def _ticking(self, step):
		state = {"now": 0.0}

		def clock():
			state["now"] += step
			return state["now"]

		return patch.object(cache_store, "_clock", clock)

	def test_a_slow_write_ends_as_its_own_class_with_no_swap_and_no_too_large_backoff(self):
		held = self.claim()
		with (
			self._ticking(50),
			patch.object(cache_store, "CHUNK_ROWS", 1),
			self.assertRaises(cache_store.WriteDeadline),
		):
			self.publish(held, [_row(voucher_no=f"SI-{n}") for n in range(5)])
		head, gen = self.head(), self.gen(held.generation)
		self.assertIsNone(head.current_generation)
		self.assertEqual((gen.state, gen.failure_class), ("failed", "write_deadline"))
		# A slow database is not a big history: one plain failure, no 6 h backoff.
		self.assertEqual((head.failures, head.backoff_level, head.last_failure), (1, 0, "write_deadline"))
		self.assertIsNone(head.memo_until)
		self.assertEqual(self.row_count(held.generation), 0)

	def test_the_deadline_is_checked_before_the_swap_too(self):
		held = self.claim()
		real = cache_store._read_back

		def late(claim_, expected, deadline_at):
			real(claim_, expected, deadline_at)
			raise cache_store.WriteDeadline("late")

		with patch.object(cache_store, "_read_back", late), self.assertRaises(cache_store.WriteDeadline):
			self.publish(held)
		self.assertIsNone(self.head().current_generation)

	def test_a_fast_write_passes(self):
		held = self.claim()
		with self._ticking(0.001):
			self.assertEqual(self.publish(held, [_row()]).outcome, "swapped")

	def test_the_default_deadline_is_the_write_deadline_of_cache_state(self):
		import inspect

		default = inspect.signature(cache_store.publish).parameters["deadline_s"].default
		self.assertEqual(default, cache_state.WRITE_DEADLINE_S)


class TestRelease(SideCase):
	def test_release_clears_the_claim_by_token_and_counts_a_hop(self):
		held = self.claim()
		self.assertEqual(cache_store.release_claim(held), 1)
		head = self.head()
		self.assertEqual((head.claim_token, head.hop_seq), (None, 1))
		self.assertIsNone(cache_store.release_claim(held), "a second release does nothing")
		self.assertEqual(self.head().hop_seq, 1)

	def test_a_release_with_another_token_does_nothing(self):
		held = self.claim()
		other = replace_claim(held, token="not-the-token")
		self.assertIsNone(cache_store.release_claim(other))
		self.assertEqual(self.head().claim_token, held.token)

	def test_a_release_with_an_old_epoch_does_nothing(self):
		held = self.claim()
		frappe.db.sql(f"update `tab{HEAD}` set purge_epoch=5")
		frappe.db.commit()
		self.assertIsNone(cache_store.release_claim(held))

	def test_release_works_while_the_main_connection_holds_the_head_lock(self):
		held = self.claim()
		_sql(f"select name from `tab{HEAD}` for update")  # an interrupted chunk transaction would hold this
		self.assertEqual(cache_store.release_claim(held), 1)

	def test_a_job_deadline_in_the_middle_of_a_chunk_leaves_a_claim_the_finally_releases(self):
		held = self.claim()

		def die(generation, start, chunk):
			raise TimeoutError

		with patch.object(cache_store, "_insert_rows", die), self.assertRaises(TimeoutError):
			self.publish(held, [_row(), _row(voucher_no="b")])
		self.assertEqual(self.head().claim_token, held.token, "the store does no database work on a deadline")
		self.assertEqual(cache_store.release_claim(held), 1)
		again = self.claim()
		self.assertNotEqual(again.token, held.token)
		self.assertEqual(self.head().current_generation, None)


class TestFailureTrace(SideCase):
	"""A failed generation keeps the frames of the error that failed it (no message, no data), cut short."""

	def error(self):
		text = "Acme" + " Ltd secret"  # a runtime value, not a literal of the source line
		try:
			raise ValueError(text)
		except ValueError as exc:
			return exc

	def test_the_trace_holds_the_class_and_the_frames_and_never_the_message(self):
		trace = cache_store.trace_of(self.error())
		self.assertTrue(trace.startswith("ValueError"))
		self.assertIn("in error", trace)
		self.assertNotIn("Acme", trace)
		self.assertNotIn("secret", trace)

	def test_a_long_trace_is_cut_to_the_limit_and_keeps_the_end_nearest_the_error(self):
		full = cache_store.trace_of(self.error())
		with patch.object(cache_store, "TRACE_CHARS", 60):
			cut = cache_store.trace_of(self.error())
		self.assertGreater(len(full), 60)
		self.assertEqual(cut, full[-60:])
		self.assertTrue(cut.rstrip().endswith("raise ValueError(text)"))

	def test_fail_stores_the_trace_on_the_generation(self):
		held = self.claim()
		cache_store.fail(held, "error:ValueError", cache_store.trace_of(self.error()))
		stored = self.gen(held.generation).failure_trace
		self.assertTrue(stored.startswith("ValueError"))
		self.assertNotIn("Acme", stored)

	def test_a_failure_with_no_trace_stores_none(self):
		held = self.claim()
		cache_store.fail(held, "error:ValueError")
		self.assertIsNone(self.gen(held.generation).failure_trace)

	def test_a_publish_that_raises_keeps_the_trace_of_the_error(self):
		held = self.claim()
		secret = "Acme" + " Ltd"  # the data is a runtime value, as in a job, and not a literal in the frame

		def verify():
			raise RuntimeError(secret)

		with self.assertRaises(RuntimeError):
			self.publish(held, verify=verify)
		generation = self.gen(held.generation)
		self.assertEqual(generation.failure_class, "error:RuntimeError")
		self.assertTrue(generation.failure_trace.startswith("RuntimeError"))
		self.assertIn("in verify", generation.failure_trace)
		self.assertNotIn("Acme", generation.failure_trace)

	def test_a_failure_is_counted_once_and_only_while_the_generation_is_still_building(self):
		held = self.claim()
		cache_store.fail(held, "error:ValueError")
		cache_store.fail(held, "error:ValueError")
		self.assertEqual(self.head().failures, 1)
		self.expire_claim()
		fresh = self.claim()
		self.publish(fresh, keep_claim=True)
		cache_store.fail(fresh, "error:ValueError")
		self.assertEqual(self.head().failures, 0)
		self.assertEqual(self.gen(fresh.generation).state, "current")


class TestFailureBookkeeping(SideCase):
	def _fail(self, klass):
		held = self.claim()
		cache_store.fail(held, klass)
		cache_store.release_claim(held)
		return held

	def _memo_seconds(self):
		return _sql(
			f"select timestampdiff(second, now(6), memo_until) from `tab{HEAD}` where name=%s",
			(cache_store.head_name(self.company, "Receivable"),),
		)[0][0]

	def test_history_too_large_walks_six_twelve_twenty_four_hours(self):
		for level, seconds in enumerate((6 * 3600, 12 * 3600, 24 * 3600, 24 * 3600), start=1):
			frappe.db.sql(f"update `tab{HEAD}` set memo_until=null")
			frappe.db.commit()
			self._fail("history_too_large")
			self.assertAlmostEqual(self._memo_seconds(), seconds, delta=10)
			self.assertEqual(self.head().backoff_level, min(level, 3))

	def test_three_other_failures_in_a_row_start_a_one_hour_memo(self):
		for expected in (1, 2):
			self._fail("error:KeyError")
			head = self.head()
			self.assertEqual((head.failures, head.memo_until), (expected, None))
		self._fail("reader_fallback:deadline")
		self.assertEqual(self.head().failures, 3)
		self.assertAlmostEqual(self._memo_seconds(), cache_store.FAILURE_MEMO_S, delta=10)

	def test_fenced_and_lease_lost_are_not_counted(self):
		self._fail("fenced")
		self._fail("lease_lost")
		head = self.head()
		self.assertEqual(
			(head.failures, head.backoff_level, head.memo_until, head.last_failure), (0, 0, None, None)
		)

	def test_a_success_resets_failures_backoff_and_memo(self):
		self._fail("error:KeyError")
		self._fail("error:KeyError")
		held = self.claim()
		self.publish(held)
		head = self.head()
		self.assertEqual(
			(head.failures, head.backoff_level, head.memo_until, head.last_failure), (0, 0, None, None)
		)

	def test_a_failure_by_a_writer_that_lost_the_claim_changes_nothing_on_the_head(self):
		old = self.claim()
		self.expire_claim()
		self.claim()
		cache_store.fail(old, "error:KeyError")
		self.assertEqual(self.head().failures, 0)
		self.assertEqual(self.gen(old.generation).state, "failed")

	def test_the_failure_class_is_sanitised_and_bounded(self):
		held = self.claim()
		cache_store.fail(held, "error:Bad Name\n" + "x" * 100)
		stored = self.gen(held.generation).failure_class
		self.assertLessEqual(len(stored), 64)
		self.assertNotIn(" ", stored)
		self.assertNotIn("\n", stored)


class TestNextGenerationAndRenew(SideCase):
	def test_next_generation_is_fenced_when_the_claim_is_gone(self):
		held = self.claim()
		cache_store.release_claim(held)
		with self.assertRaises(cache_store.Fenced):
			cache_store.next_generation(held)

	def test_renew_gives_the_claim_a_full_life_and_is_fenced_after_a_purge(self):
		held = self.claim()
		frappe.db.sql(f"update `tab{HEAD}` set claim_expires=now(6) + interval 3 second")
		frappe.db.commit()
		cache_store.renew(held)
		left = _sql(f"select timestampdiff(second, now(6), claim_expires) from `tab{HEAD}`")[0][0]
		self.assertGreater(left, 100)
		cache_store.bump_epoch(self.company)
		frappe.db.commit()
		with self.assertRaises(cache_store.Fenced):
			cache_store.renew(held)


class JanitorCase(SideCase):
	"""Fixtures written straight to the tables, so every state and age can be set."""

	def make_gen(
		self,
		state,
		*,
		company=None,
		side=None,
		age_s=0,
		deadline_in_s=3600,
		build=BUILD,
		outcome=None,
		row_count=0,
	):
		number = frappe.db.get_next_sequence_val(GENERATION)
		frappe.db.sql(
			f"insert into `tab{GENERATION}` (name, creation, modified, company, side, state, deadline, cache_build, outcome, "
			"row_count) values (%s, now(6), now(6) - interval %s second, %s, %s, %s, now(6) + interval %s second, %s, %s, %s)",
			(
				number,
				age_s,
				company or self.company,
				side or self.side,
				state,
				deadline_in_s,
				build,
				outcome,
				row_count,
			),
		)
		frappe.db.commit()
		return number

	def make_rows(self, generation, count=3):
		for seq in range(count):
			frappe.db.sql(
				f"insert into `tab{ROW}` (name, creation, modified, generation, row_id) values (%s, now(6), now(6), %s, %s)",
				(f"{generation:012d}{seq:08d}", generation, f"id{seq}"),
			)
		frappe.db.commit()

	def make_head(self, *, company=None, side=None, current=None, live_claim=False, creation="same"):
		company = company or self.company
		side = side or self.side
		real = cache_store.company_creation(company)
		stored = real if creation == "same" else creation
		frappe.db.sql(
			f"insert into `tab{HEAD}` (name, creation, modified, company, side, company_creation, current_generation, "
			"claim_token, claim_expires) values (%s, now(6), now(6), %s, %s, %s, %s, %s, "
			+ ("now(6) + interval 300 second)" if live_claim else "null)"),
			(
				cache_store.head_name(company, side),
				company,
				side,
				stored,
				current,
				"tok" if live_claim else None,
			),
		)
		frappe.db.commit()

	def gone(self):
		return "PCAL-B3-gone-company"

	def state(self, generation):
		frappe.db.rollback()
		row = frappe.db.sql(f"select state from `tab{GENERATION}` where name=%s", (generation,))
		return row[0][0] if row else None


class QueryPlanCase(JanitorCase):
	"""A table of about 5,000 generations and 30,000 rows, the size at which a bad plan shows."""

	COMPANIES, GENERATIONS_PER_SIDE = 30, 60

	def setUp(self):
		super().setUp()
		numbers = []
		for company in range(self.COMPANIES):
			for side in ("Receivable", "Payable", "Bank"):
				for position in range(self.GENERATIONS_PER_SIDE):
					state = "retired" if position < 58 else ("failed" if position == 58 else "building")
					numbers.append((len(numbers) + 1, f"PCAL-FW1-{company}", side, state))
		for start in range(0, len(numbers), 500):
			chunk = numbers[start : start + 500]
			frappe.db.sql(
				f"insert into `tab{GENERATION}` (name, creation, modified, company, side, state, deadline, as_of, "
				"cache_build, row_count) values "
				+ ",".join(
					[
						"(%s, now(6), now(6) - interval 2 hour, %s, %s, %s, now(6) - interval 1 hour, "
						"now(6) - interval 3 hour, 'b', 5)"
					]
					* len(chunk)
				),
				[value for entry in chunk for value in entry],
			)
		rows = [(f"{n:012d}{q:08d}", n, f"id{q}") for n, *_ in numbers if n % 4 == 0 for q in range(25)]
		for start in range(0, len(rows), 1000):
			chunk = rows[start : start + 1000]
			frappe.db.sql(
				f"insert into `tab{ROW}` (name, creation, modified, generation, row_id) values "
				+ ",".join(["(%s, now(6), now(6), %s, %s)"] * len(chunk)),
				[value for entry in chunk for value in entry],
			)
		frappe.db.commit()
		frappe.db.sql(f"analyze table `tab{GENERATION}`")
		frappe.db.sql(f"analyze table `tab{ROW}`")

	def plan_of(self, run) -> str:
		"""The `EXPLAIN FORMAT=JSON` text of the first statement on the Generation table that `run` sends."""
		sent = []
		real = frappe.db.sql

		def spy(query, values=(), *args, **kwargs):
			if (
				not sent
				and f"FROM `tab{GENERATION}`" in str(query)
				and str(query).lstrip().startswith("SELECT")
			):
				sent.append((query, values))
			return real(query, values, *args, **kwargs)

		with patch.object(frappe.db, "sql", spy):
			run()
		self.assertTrue(sent, "the code sent no SELECT on the generation table")
		return str(real("EXPLAIN FORMAT=JSON " + sent[0][0], sent[0][1])[0][0]).lower()


class TestJanitorQueryPlans(QueryPlanCase):
	def test_the_rows_probe_is_a_lookup_per_generation_and_not_a_set(self):
		probe = cache_store._has_rows("g.name")
		self.assertTrue(probe.startswith("(SELECT 1 FROM"), probe)
		self.assertIn("LIMIT 1", probe)
		self.assertNotIn("EXISTS", probe)

	def test_the_retired_scan_of_the_tick_does_not_build_the_set_of_all_rows(self):
		plan = self.plan_of(
			lambda: cache_store._delete_retired_rows(cache_store._Scope(), cache_store._Budget(0))
		)
		self.assertNotIn("materialized", plan)
		self.assertIn("generation_due_date_name", plan)

	def test_the_tick_scans_of_dead_and_purged_generations_use_an_index_led_by_the_state(self):
		budget = cache_store._Budget(0)
		for label, run in (
			("dead", lambda: cache_store._delete_dead(cache_store._Scope(), budget)),
			("purged", lambda: cache_store._resume_purged(cache_store._Scope(), budget)),
			("expired", cache._record_expired_builds),
		):
			with self.subTest(label):
				plan = self.plan_of(run)
				self.assertTrue("state_deadline" in plan or "state_modified" in plan, plan)

	def test_the_old_metadata_scan_uses_an_index_led_by_the_state(self):
		plan = self.plan_of(
			lambda: cache_store._delete_old_metadata(cache_store._Scope(), cache_store._Budget(0), None)
		)
		self.assertIn("state_modified", plan)

	def test_the_night_records_of_the_status_are_read_by_the_date_of_the_generation(self):
		plan = self.plan_of(lambda: cache_status.oracle_records(days=2))
		self.assertIn('"key": "as_of"', plan)

	def test_every_tick_select_on_the_generation_table_is_bounded(self):
		seen = []
		real = frappe.db.sql

		def spy(query, values=(), *args, **kwargs):
			text = " ".join(str(query).split())
			bounded_by_names = f"FROM `tab{GENERATION}` WHERE name IN (" in text  # at most 200 ids
			if (
				text.startswith("SELECT")
				and f"FROM `tab{GENERATION}`" in text
				and "COUNT(" not in text
				and not bounded_by_names
			):
				seen.append(text)
			return real(query, values, *args, **kwargs)

		with patch.object(frappe.db, "sql", spy):
			cache_store.janitor(listed=(self.company,), cache_build=BUILD)
		self.assertTrue(seen)
		self.assertEqual([text for text in seen if "LIMIT" not in text], [])


class TestJanitorBacklog(JanitorCase):
	def test_it_counts_purged_generations_and_old_retired_ones_that_still_hold_rows(self):
		purged = [self.make_gen("purged", age_s=120) for _ in range(3)]
		old = self.make_gen("retired", age_s=cache_store.GRACE_S + 3600)
		self.make_rows(old)
		self.make_gen("retired", age_s=cache_store.GRACE_S + 3600)  # no rows left: finished, not backlog
		fresh = self.make_gen("retired", age_s=10)
		self.make_rows(fresh)
		found = cache_store.janitor_backlog()
		self.assertEqual((found["purged"], found["retired_with_rows"]), (len(purged), 1))
		self.assertGreaterEqual(found["oldest_s"], cache_store.GRACE_S + 3500)

	def test_an_empty_table_has_no_backlog(self):
		self.assertEqual(cache_store.janitor_backlog(), {"purged": 0, "retired_with_rows": 0, "oldest_s": 0})

	def test_the_count_of_retired_generations_is_bounded(self):
		self.assertIn("LIMIT", " ".join(self.captured(cache_store.janitor_backlog)))

	def captured(self, run):
		seen = []
		real = frappe.db.sql

		def spy(query, values=(), *args, **kwargs):
			seen.append(" ".join(str(query).split()))
			return real(query, values, *args, **kwargs)

		with patch.object(frappe.db, "sql", spy):
			run()
		return seen


class TestJanitorRetired(JanitorCase):
	def test_rows_of_a_retired_generation_go_after_the_grace_and_the_metadata_stays(self):
		old = self.make_gen("retired", age_s=cache_store.GRACE_S + 60)
		fresh = self.make_gen("retired", age_s=cache_store.GRACE_S - 120)
		self.make_rows(old)
		self.make_rows(fresh)
		cache_store.janitor()
		self.assertEqual((self.row_count(old), self.row_count(fresh)), (0, 3))
		self.assertEqual((self.state(old), self.state(fresh)), ("retired", "retired"))

	def test_the_current_generation_is_never_deleted(self):
		current = self.make_gen("current", age_s=10 * 24 * 3600, deadline_in_s=-10 * 24 * 3600)
		self.make_rows(current)
		self.make_head(current=current)
		cache_store.janitor(cache_build=BUILD)
		self.assertEqual((self.row_count(current), self.state(current)), (3, "current"))

	def test_the_budget_bounds_a_run_and_the_next_run_finishes(self):
		old = self.make_gen("retired", age_s=cache_store.GRACE_S + 60)
		self.make_rows(old, 5)
		self.assertEqual(cache_store.janitor(budget=3), 3)
		self.assertEqual(self.row_count(old), 2)
		self.assertEqual(cache_store.janitor(budget=3), 2)
		self.assertEqual(self.row_count(old), 0)

	def test_a_run_can_be_narrowed_to_one_side(self):
		mine = self.make_gen("retired", age_s=cache_store.GRACE_S + 60)
		theirs = self.make_gen("retired", side="Payable", age_s=cache_store.GRACE_S + 60)
		self.make_rows(mine)
		self.make_rows(theirs)
		cache_store.janitor(company=self.company, side="Receivable")
		self.assertEqual((self.row_count(mine), self.row_count(theirs)), (0, 3))


class TestJanitorSizedToTheData(JanitorCase):
	"""E4 left retired generations unbounded; the own-side janitor must delete at least what the chain writes."""

	def test_the_own_side_run_removes_all_retired_rows_past_the_grace_in_one_cycle(self):
		old = self.make_gen("retired", age_s=cache_store.GRACE_S + 60, row_count=10)
		newer = self.make_gen("retired", age_s=cache_store.GRACE_S + 30, row_count=10)
		self.make_rows(old, 10)
		self.make_rows(newer, 10)
		with (
			patch.object(cache_store, "JANITOR_BUDGET_ROWS", 3),
			patch.object(cache_store, "JANITOR_CHUNK_ROWS", 2),
		):
			deleted = cache_store.janitor(company=self.company, side="Receivable")
		self.assertEqual((self.row_count(old), self.row_count(newer)), (0, 0))
		self.assertEqual(deleted, 20)

	def test_a_run_not_for_one_side_keeps_the_fixed_budget(self):
		old = self.make_gen("retired", age_s=cache_store.GRACE_S + 60, row_count=10)
		self.make_rows(old, 10)
		with (
			patch.object(cache_store, "JANITOR_BUDGET_ROWS", 3),
			patch.object(cache_store, "JANITOR_CHUNK_ROWS", 2),
		):
			cache_store.janitor()
		self.assertEqual(self.row_count(old), 7)

	def test_a_run_has_a_time_budget_of_40_seconds_by_default(self):
		self.assertEqual(cache_store.JANITOR_TIME_BUDGET_S, 40)
		self.assertEqual(inspect.signature(cache_store.janitor).parameters["time_budget_s"].default, 40)

	def test_the_time_budget_stops_a_run(self):
		old = self.make_gen("retired", age_s=cache_store.GRACE_S + 60, row_count=10)
		self.make_rows(old, 10)
		state = {"now": 0.0}

		def clock():
			state["now"] += 10
			return state["now"]

		with (
			patch.object(cache_store, "_clock", clock),
			patch.object(cache_store, "JANITOR_CHUNK_ROWS", 2),
		):
			cache_store.janitor(company=self.company, side="Receivable", time_budget_s=15)
		self.assertGreater(self.row_count(old), 0)
		self.assertLess(self.row_count(old), 10)

	def test_the_backlog_stays_bounded_over_several_rounds_of_a_big_company(self):
		per_round = 8
		totals = []
		with (
			patch.object(cache_store, "JANITOR_BUDGET_ROWS", 2),
			patch.object(cache_store, "JANITOR_CHUNK_ROWS", 2),
			patch.object(cache_store, "GRACE_S", 0),
		):
			for round_ in range(6):
				held = self.claim()
				self.publish(held, [_row(voucher_no=f"R{round_}-{n}") for n in range(per_round)])
				frappe.db.rollback()
				totals.append(_sql(f"select count(*) from `tab{ROW}`")[0][0])
		self.assertLessEqual(max(totals), 2 * per_round, totals)


class TestJanitorDead(JanitorCase):
	def test_a_building_generation_past_its_deadline_with_no_live_claim_is_deleted(self):
		dead = self.make_gen("building", deadline_in_s=-60)
		self.make_rows(dead)
		self.make_head()
		cache_store.janitor()
		self.assertEqual((self.row_count(dead), self.state(dead)), (0, "failed"))

	def test_a_building_generation_before_its_deadline_is_kept(self):
		running = self.make_gen("building", deadline_in_s=600)
		self.make_rows(running)
		cache_store.janitor()
		self.assertEqual((self.row_count(running), self.state(running)), (3, "building"))

	def test_the_live_claimants_newest_building_generation_is_never_deleted(self):
		stale, mine = (
			self.make_gen("building", deadline_in_s=-60),
			self.make_gen("building", deadline_in_s=-60),
		)
		self.make_rows(stale)
		self.make_rows(mine)
		self.make_head(live_claim=True)
		cache_store.janitor()
		self.assertEqual((self.row_count(stale), self.state(stale)), (0, "failed"))
		self.assertEqual((self.row_count(mine), self.state(mine)), (3, "building"))

	def test_a_failed_generation_past_its_deadline_loses_its_rows(self):
		failed = self.make_gen("failed", deadline_in_s=-60)
		self.make_rows(failed)
		cache_store.janitor()
		self.assertEqual((self.row_count(failed), self.state(failed)), (0, "failed"))

	def test_rows_with_no_generation_row_are_deleted_and_others_kept(self):
		kept = self.make_gen("current")
		self.make_rows(kept)
		self.make_rows(987654321)
		cache_store.janitor()
		self.assertEqual((self.row_count(987654321), self.row_count(kept)), (0, 3))


class TestJanitorMetadata(JanitorCase):
	def test_metadata_goes_after_thirty_days_for_retired_and_failed_only(self):
		day = 24 * 3600
		old_retired = self.make_gen("retired", age_s=31 * day, deadline_in_s=-day)
		old_failed = self.make_gen("failed", age_s=31 * day, deadline_in_s=-day)
		young = self.make_gen("retired", age_s=29 * day)
		current = self.make_gen("current", age_s=60 * day)
		self.make_head(current=current)
		cache_store.janitor()
		self.assertEqual(
			[self.state(n) for n in (old_retired, old_failed, young, current)],
			[None, None, "retired", "current"],
		)

	def test_an_old_build_goes_only_after_one_rebuild(self):
		old = self.make_gen("retired", age_s=cache_store.GRACE_S + 60, build="o" * 64)
		current = self.make_gen("current", build="o" * 64)
		self.make_head(current=current)
		cache_store.janitor(cache_build=BUILD)
		self.assertEqual(self.state(old), "retired", "no current generation of the new build yet")
		frappe.db.sql(f"update `tab{GENERATION}` set cache_build=%s where name=%s", (BUILD, current))
		frappe.db.commit()
		cache_store.janitor(cache_build=BUILD)
		self.assertEqual(self.state(old), None)
		self.assertEqual(self.state(current), "current")


class TestPurgeLockOrder(JanitorCase):
	def test_the_heads_are_locked_in_the_order_the_request_path_marks_them(self):
		"""`note_opened` marks Receivable, Payable, Bank; a purge that locks in another order can deadlock with it."""
		for side in reversed(cache_store.SIDES):
			self.make_head(side=side)
		seen = []
		real = frappe.db.sql

		def spy(query, values=(), *args, **kwargs):
			result = real(query, values, *args, **kwargs)
			if "FOR UPDATE" in str(query) and f"FROM `tab{HEAD}`" in str(query):
				seen.append([row[0] for row in result])
			return result

		with patch.object(frappe.db, "sql", spy):
			cache_store.bump_epoch(self.company)
		frappe.db.rollback()
		self.assertEqual(seen, [[cache_store.head_name(self.company, side) for side in cache_store.SIDES]])


class TestPurge(JanitorCase):
	def test_bump_epoch_fences_clears_and_marks_without_committing(self):
		current = self.make_gen("current")
		self.make_gen("retired")
		self.make_head(current=current, live_claim=True)
		self.assertTrue(cache_store.bump_epoch(self.company))
		# Read in the same transaction (the helpers roll back): the bump is visible here, and only here.
		head = _sql(f"select purge_epoch, claim_token, current_generation from `tab{HEAD}`")[0]
		states = _sql(f"select state from `tab{GENERATION}` where company=%s", (self.company,))
		self.assertEqual(tuple(head), (1, None, None))
		self.assertEqual({row[0] for row in states}, {"purged"})
		frappe.db.rollback()
		self.assertEqual(self.head().purge_epoch, 0)
		self.assertEqual(self.state(current), "current")

	def test_bump_epoch_reports_nothing_to_purge(self):
		self.assertFalse(cache_store.bump_epoch(self.company))

	def test_the_purge_job_is_queued_after_the_commit_with_a_digest_only(self):
		self.make_head()
		with patch.object(frappe, "enqueue") as enqueue:
			self.assertTrue(cache_store.purge_company(self.company))
			enqueue.assert_not_called()
			frappe.db.commit()
		enqueue.assert_called_once()
		args, kwargs = enqueue.call_args
		self.assertEqual((args[0], kwargs["queue"]), (cache_store.PURGE_METHOD, "long"))
		self.assertNotIn(self.company, json.dumps([args, {k: str(v) for k, v in kwargs.items()}]))
		self.assertEqual(set(kwargs) - {"queue", "job_id", "deduplicate", "digest"}, set())

	def test_a_rolled_back_purge_queues_no_job_and_changes_nothing(self):
		self.make_head()
		with patch.object(frappe, "enqueue") as enqueue:
			cache_store.purge_company(self.company)
			frappe.db.rollback()
			frappe.db.commit()
		enqueue.assert_not_called()
		self.assertEqual(self.head().purge_epoch, 0)

	def test_an_enqueue_failure_is_logged_by_class_and_never_raised(self):
		self.make_head()
		logger = patch.object(frappe, "logger")
		with (
			patch.object(frappe, "enqueue", side_effect=RuntimeError("secret " + self.company)),
			logger as log,
		):
			cache_store.purge_company(self.company)
			frappe.db.commit()
		warning = log.return_value.warning
		warning.assert_called_once()
		self.assertNotIn(self.company, str(warning.call_args))
		self.assertIn("RuntimeError", str(warning.call_args))

	def test_a_database_error_in_the_bump_propagates(self):
		with (
			patch.object(cache_store, "bump_epoch", side_effect=frappe.QueryDeadlockError),
			self.assertRaises(frappe.QueryDeadlockError),
		):
			cache_store.purge_company(self.company)

	def test_the_purge_hooks_get_the_digest_and_errors_are_handled_by_class(self):
		calls = []
		real = frappe.get_hooks

		def hooks(name=None, *a, **k):
			return (
				["jarvis.tests.test_payment_calendar_cache_store._hook"]
				if name == "pcal_cache_purge"
				else real(name, *a, **k)
			)

		global _hook
		_hook = calls.append
		with patch.object(frappe, "get_hooks", hooks):
			cache_store.notify_purged("abc")
			self.assertEqual(calls, ["abc"])
			for error, raises in (
				(ValueError("x"), False),
				(frappe.QueryDeadlockError(), True),
				(TimeoutError(), True),
			):
				_hook = lambda digest, error=error: (_ for _ in ()).throw(error)  # noqa: E731
				with self.subTest(type(error).__name__):
					if raises:
						with self.assertRaises(type(error)):
							cache_store.notify_purged("abc")
					else:
						cache_store.notify_purged("abc")

	def test_the_purge_job_deletes_only_purged_generations(self):
		purged, kept = self.make_gen("purged"), self.make_gen("current")
		self.make_rows(purged)
		self.make_rows(kept)
		cache_store.run_purge("digest")
		self.assertEqual((self.state(purged), self.row_count(purged)), (None, 0))
		self.assertEqual((self.state(kept), self.row_count(kept)), ("current", 3))

	def test_a_purge_killed_midway_is_finished_by_the_janitor(self):
		purged = self.make_gen("purged")
		self.make_rows(purged, 7)
		with patch.object(cache_store, "JANITOR_CHUNK_ROWS", 2):
			cache_store.janitor(budget=4)
			self.assertEqual((self.row_count(purged), self.state(purged)), (3, "purged"))
			cache_store.janitor()
		self.assertEqual((self.row_count(purged), self.state(purged)), (0, None))

	def test_heads_of_a_company_that_is_gone_go_with_their_generations(self):
		gone = self.gone()
		current = self.make_gen("current", company=gone)
		self.make_rows(current)
		self.make_head(company=gone, current=current, creation=datetime(2020, 1, 1))
		cache_store.janitor()
		cache_store.janitor()
		frappe.db.rollback()
		self.assertEqual(
			frappe.db.sql(f"select count(*) from `tab{HEAD}` where company=%s", (gone,))[0][0], 0
		)
		self.assertEqual(
			frappe.db.sql(f"select count(*) from `tab{GENERATION}` where company=%s", (gone,))[0][0], 0
		)
		self.assertEqual(self.row_count(current), 0)

	def test_a_head_of_a_reused_name_is_purged_too(self):
		current = self.make_gen("current")
		self.make_rows(current)
		self.make_head(current=current, creation=datetime(2001, 1, 1))
		cache_store.janitor()
		cache_store.janitor()
		self.assertEqual((self.state(current), self.row_count(current)), (None, 0))

	def test_a_company_off_the_list_is_purged_once_not_every_tick(self):
		current = self.make_gen("current")
		self.make_rows(current)
		self.make_head(current=current)
		cache_store.janitor(listed=())
		cache_store.janitor(listed=())
		cache_store.janitor(listed=())
		self.assertEqual(self.head().purge_epoch, 1)
		self.assertEqual((self.state(current), self.row_count(current)), (None, 0))

	def test_a_listed_company_is_left_alone_and_mode_off_purges_every_company(self):
		current = self.make_gen("current")
		self.make_rows(current)
		self.make_head(current=current)
		cache_store.janitor(listed=(self.company,))
		self.assertEqual((self.head().purge_epoch, self.state(current)), (0, "current"))
		cache_store.janitor(listed=())
		self.assertEqual(self.head().purge_epoch, 1)

	def test_an_in_flight_build_of_an_unlisted_company_is_fenced_by_the_purge(self):
		held = self.claim()
		cache_store.janitor(listed=())
		with self.assertRaises(cache_store.Fenced) as caught:
			self.publish(held)
		self.assertEqual(caught.exception.reason, "epoch")
		self.assertEqual(self.row_count(held.generation), 0)

	def test_the_janitor_before_a_claim_removes_a_dead_generation(self):
		dead = self.make_gen("building", deadline_in_s=-60)
		self.make_rows(dead)
		self.claim()
		self.assertEqual(self.row_count(dead), 0)


class TestFailOrphan(JanitorCase):
	def _dead(self, **kw):
		generation = self.make_gen("building", deadline_in_s=-60, **kw)
		self.make_rows(generation)
		self.make_head()
		return generation

	def test_an_expired_building_generation_is_failed_and_the_backoff_is_counted(self):
		generation = self._dead()
		self.assertTrue(cache_store.fail_orphan(self.company, "Receivable", generation, "history_too_large"))
		gen, head = self.gen(generation), self.head()
		self.assertEqual((gen.state, gen.failure_class), ("failed", "history_too_large"))
		self.assertEqual((head.backoff_level, head.last_failure), (1, "history_too_large"))
		self.assertIsNotNone(head.memo_until)
		self.assertEqual(self.row_count(generation), 0)

	def test_it_works_when_the_claim_token_was_cleared(self):
		generation = self._dead()
		frappe.db.sql(f"update `tab{HEAD}` set claim_token=NULL, claim_expires=NULL")
		frappe.db.commit()
		self.assertTrue(cache_store.fail_orphan(self.company, "Receivable", generation, "history_too_large"))
		self.assertEqual(self.head().backoff_level, 1)

	def test_a_newer_generation_of_a_live_claimant_decides_the_head_so_the_orphan_is_not_counted(self):
		# The retry that is running now owns the outcome: a success resets the head, a failure counts itself.
		generation = self._dead()
		frappe.db.sql(
			f"update `tab{HEAD}` set claim_token='other', claim_expires=now(6) + interval 300 second"
		)
		self.make_gen("building", deadline_in_s=3600)
		frappe.db.commit()
		self.assertTrue(cache_store.fail_orphan(self.company, "Receivable", generation, "worker_lost"))
		self.assertEqual(self.state(generation), "failed")
		self.assertEqual(self.row_count(generation), 0)
		self.assertNoFailureOnTheHead()

	def assertNoFailureOnTheHead(self):
		head = self.head()
		self.assertEqual((head.failures, head.backoff_level, head.last_failure), (0, 0, None))
		self.assertIsNone(head.memo_until)

	def test_an_orphan_older_than_the_current_generation_is_failed_and_not_counted(self):
		orphan = self.make_gen("building", deadline_in_s=-60)
		self.make_rows(orphan)
		current = self.make_gen("current")
		self.make_head(current=current)
		self.assertTrue(cache_store.fail_orphan(self.company, "Receivable", orphan, "history_too_large"))
		self.assertEqual(self.state(orphan), "failed")
		self.assertEqual(self.row_count(orphan), 0)
		self.assertNoFailureOnTheHead()

	def test_an_orphan_is_not_counted_when_the_head_succeeded_after_it_began(self):
		# The newer generation is gone (a no-change success keeps the old current one, a janitor removed the rest).
		orphan = self._dead()
		frappe.db.sql(f"update `tab{HEAD}` set last_success=now(6) + interval 1 second")
		frappe.db.commit()
		self.assertTrue(cache_store.fail_orphan(self.company, "Receivable", orphan, "history_too_large"))
		self.assertNoFailureOnTheHead()

	def test_an_orphan_is_not_counted_when_the_head_holds_a_newer_current_generation_row_that_is_gone(self):
		orphan = self.make_gen("building", deadline_in_s=-60)
		self.make_head(
			current=orphan + 100
		)  # a current generation newer than the orphan, its row long deleted
		self.assertTrue(cache_store.fail_orphan(self.company, "Receivable", orphan, "history_too_large"))
		self.assertNoFailureOnTheHead()

	def test_an_orphan_older_than_a_newer_failed_generation_is_not_counted_again(self):
		orphan = self._dead()
		self.make_gen("failed", deadline_in_s=-30, outcome="failed")
		self.assertTrue(cache_store.fail_orphan(self.company, "Receivable", orphan, "worker_lost"))
		self.assertNoFailureOnTheHead()

	def test_a_generation_that_is_still_the_newest_with_no_success_since_is_counted(self):
		orphan = self._dead()
		frappe.db.sql(f"update `tab{HEAD}` set last_success=now(6) - interval 1 hour")
		frappe.db.commit()
		self.assertTrue(cache_store.fail_orphan(self.company, "Receivable", orphan, "worker_lost"))
		head = self.head()
		self.assertEqual((head.failures, head.last_failure), (1, "worker_lost"))

	def test_a_generation_before_its_deadline_is_left_alone(self):
		generation = self.make_gen("building", deadline_in_s=600)
		self.make_head()
		self.assertFalse(cache_store.fail_orphan(self.company, "Receivable", generation, "history_too_large"))
		self.assertEqual((self.state(generation), self.head().backoff_level), ("building", 0))

	def test_the_live_claimants_newest_generation_is_left_alone(self):
		generation = self.make_gen("building", deadline_in_s=-60)
		self.make_head(live_claim=True)
		self.assertFalse(cache_store.fail_orphan(self.company, "Receivable", generation, "history_too_large"))
		self.assertEqual((self.state(generation), self.head().backoff_level), ("building", 0))

	def test_the_failure_is_counted_once(self):
		generation = self._dead()
		cache_store.fail_orphan(self.company, "Receivable", generation, "history_too_large")
		self.assertFalse(cache_store.fail_orphan(self.company, "Receivable", generation, "history_too_large"))
		self.assertEqual(self.head().backoff_level, 1)

	def test_another_class_counts_as_a_plain_failure(self):
		generation = self._dead()
		cache_store.fail_orphan(self.company, "Receivable", generation, "error:MemoryError")
		head = self.head()
		self.assertEqual((head.failures, head.backoff_level), (1, 0))

	def test_a_generation_of_another_side_is_not_touched(self):
		generation = self._dead()
		self.assertFalse(cache_store.fail_orphan(self.company, "Payable", generation, "history_too_large"))
		self.assertEqual(self.state(generation), "building")
