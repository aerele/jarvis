"""Stage 2 storage: a snapshot is stored as status, payable and receivable parts, never as one key."""

import json
from copy import deepcopy
from hashlib import sha256
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from jarvis.payment_calendar import store
from jarvis.payment_calendar.access import ScopeChanged
from jarvis.payment_calendar.errors import SnapshotExpired
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site
from jarvis.tests.payment_calendar_results import SCOPE, obligation, schema2_result, top_invoice

STATUS_PART_KEYS = {
	"schema",
	"projection",
	"horizon",
	"currency",
	"floor",
	"bank",
	"bank_code",
	"unavailable",
	"generated_at",
	"_permission_columns",
	"_top_proofs",
}
PRIVATE_SOURCE_KEYS = {"excluded_rows", "permission_columns"}
COLUMNS = [{"fieldname": "party", "fieldtype": "Link", "options": "Supplier"}]


class TestSplitResult(TestCase):
	def test_status_part_holds_the_status_keys_and_the_private_side_state_only(self):
		parts = store.split_result(schema2_result(), SCOPE)
		self.assertEqual(set(parts["status"]), STATUS_PART_KEYS)
		self.assertEqual(
			parts["status"]["_permission_columns"],
			{side: COLUMNS for side in store.SIDES},
		)

	def test_side_parts_drop_excluded_rows_permission_columns_and_row_proofs(self):
		result = schema2_result()
		parts = store.split_result(result, SCOPE)
		for side, part in (("Payable", "payable"), ("Receivable", "receivable")):
			with self.subTest(side=side):
				source_row = result["sources"][side]["rows"][0]
				self.assertFalse(PRIVATE_SOURCE_KEYS & set(parts[part]))
				self.assertEqual(
					parts[part]["rows"], [{k: v for k, v in source_row.items() if k != "_permission"}]
				)
				self.assertEqual(parts[f"proofs-{part}"], {source_row["id"]: source_row["_permission"]})
				self.assertEqual(parts[part]["canonical_total"], "100.00")

	def test_the_input_result_is_not_changed(self):
		result = schema2_result()
		before = deepcopy(result)
		store.split_result(result, SCOPE)
		self.assertEqual(result, before)

	def test_an_unavailable_side_has_no_part_and_no_private_state(self):
		parts = store.split_result(schema2_result(unavailable={"Receivable": "forbidden"}), SCOPE)
		self.assertEqual(set(parts), {"status", "payable", "proofs-payable", "index-payable"})
		self.assertEqual(set(parts["status"]["_permission_columns"]), {"Payable"})
		self.assertEqual(set(parts["status"]["_top_proofs"]), {"Payable"})
		self.assertEqual(parts["status"]["unavailable"], {"Receivable": "forbidden"})

	def test_an_available_side_without_a_source_is_a_bug(self):
		result = schema2_result()
		result["sources"]["Payable"] = None
		with self.assertRaises(RuntimeError):
			store.split_result(result, SCOPE)

	def test_an_incomplete_schema_two_result_is_refused_naming_the_keys(self):
		cases = {
			"schema": lambda r: r.pop("schema"),
			"horizon": lambda r: r.pop("horizon"),
			"bank": lambda r: r.pop("bank"),
			"projection": lambda r: r.pop("projection"),
			"currency": lambda r: r.pop("currency"),
			"floor": lambda r: r.pop("floor"),
			"generated_at": lambda r: r.pop("generated_at"),
			"bank_code": lambda r: r.pop("bank_code"),
			"unavailable": lambda r: r.pop("unavailable"),
			"projection.month_totals": lambda r: r["projection"].pop("month_totals"),
		}
		for name, damage in cases.items():
			with self.subTest(missing=name):
				result = schema2_result()
				damage(result)
				with self.assertRaises(KeyError) as raised:
					store.split_result(result, SCOPE)
				self.assertEqual(raised.exception.args, (name,))

	def test_a_result_of_another_schema_is_refused(self):
		for schema in (1, 3, None, "2"):
			with self.subTest(schema=schema), self.assertRaises(KeyError) as raised:
				store.split_result(schema2_result(schema=schema), SCOPE)
			self.assertEqual(raised.exception.args, ("schema",))

	def test_every_missing_key_is_named_at_once(self):
		result = schema2_result()
		del result["horizon"], result["projection"]["month_totals"]
		with self.assertRaises(KeyError) as raised:
			store.split_result(result, SCOPE)
		self.assertEqual(set(raised.exception.args), {"horizon", "projection.month_totals"})

	def test_top_invoice_proofs_hold_every_party_of_a_multi_party_voucher(self):
		journal = [
			obligation("Payable", "JV-1", "Supplier A", "70.00", voucher_type="Journal Entry"),
			obligation("Payable", "JV-1", "Supplier B", "30.00", voucher_type="Journal Entry"),
			obligation("Payable", "PINV-9", "Supplier C", "10.00"),
		]
		top = top_invoice("JV-1", "Supplier A", "100.00", "Journal Entry")
		days = {"2026-11-11": {"payable_top_invoice": top}}
		proofs = store.split_result(schema2_result(payable=journal, days=days), SCOPE)["status"][
			"_top_proofs"
		]
		self.assertEqual(
			sorted((p["voucher_type"], p["voucher_no"], p["party"]) for p in proofs["Payable"]),
			[("Journal Entry", "JV-1", "Supplier A"), ("Journal Entry", "JV-1", "Supplier B")],
		)
		self.assertEqual(proofs["Receivable"], [])

	def test_top_invoice_proofs_have_the_stored_row_identity_and_proof(self):
		result = schema2_result()
		proofs = store.split_result(result, SCOPE)["status"]["_top_proofs"]["Payable"]
		stored = result["sources"]["Payable"]["rows"][0]
		self.assertEqual(
			proofs,
			[
				{
					"voucher_type": "Purchase Invoice",
					"voucher_no": "PINV-1",
					"party_type": "Supplier",
					"party": "Supplier A",
					"_permission": stored["_permission"],
				}
			],
		)

	def test_a_voucher_that_is_top_on_several_days_is_stored_once(self):
		days = {
			"2026-11-11": {"payable_top_invoice": top_invoice("PINV-1", "Supplier A")},
			"2026-11-12": {"payable_top_invoice": top_invoice("PINV-1", "Supplier A")},
		}
		proofs = store.split_result(schema2_result(days=days), SCOPE)["status"]["_top_proofs"]["Payable"]
		self.assertEqual(len(proofs), 1)

	def test_only_rows_with_something_remaining_are_proofs(self):
		rows = [
			obligation("Payable", "PINV-1", "Supplier A", "100.00"),
			obligation("Payable", "PINV-1", "Supplier B", "0.00"),
			obligation("Payable", "PINV-1", "Supplier C", "-5.00"),
		]
		proofs = store.split_result(schema2_result(payable=rows), SCOPE)["status"]["_top_proofs"]["Payable"]
		self.assertEqual([p["party"] for p in proofs], ["Supplier A"])

	def test_days_without_a_top_invoice_store_no_proofs(self):
		days = {"2026-11-11": {"payable_top_invoice": None, "receivable_top_invoice": None}}
		proofs = store.split_result(schema2_result(days=days), SCOPE)["status"]["_top_proofs"]
		self.assertEqual(proofs, {"Payable": [], "Receivable": []})

	def test_a_top_invoice_without_a_stored_row_is_a_bug(self):
		days = {"2026-11-11": {"payable_top_invoice": top_invoice("PINV-MISSING", "Supplier A")}}
		with self.assertRaises(RuntimeError):
			store.split_result(schema2_result(days=days), SCOPE)

	def test_a_receivable_top_invoice_reads_the_receivable_rows(self):
		top = top_invoice("SINV-1", "Customer A", "100.00", "Sales Invoice")
		days = {"2026-11-11": {"receivable_top_invoice": top}}
		proofs = store.split_result(schema2_result(days=days), SCOPE)["status"]["_top_proofs"]
		self.assertEqual([p["voucher_no"] for p in proofs["Receivable"]], ["SINV-1"])
		self.assertEqual(proofs["Payable"], [])


class TestSnapshotParts(TestCase):
	def setUp(self):
		require_calendar_test_site()
		self.actor = f"calendar-parts-{uuid4().hex}@example.com"
		self.other = f"calendar-parts-{uuid4().hex}@example.com"
		self.scope = {
			"actor": self.actor,
			"company": "Calendar fixture",
			"accounts": [],
			"anchor": "2026-11-11",
		}
		self.parts = store.split_result(schema2_result(), SCOPE)

	def tearDown(self):
		store.purge_actor(self.actor)
		store.purge_actor(self.other)

	def running(self, actor=None, kind="snapshot", digest=None):
		actor = actor or self.actor
		digest = digest or sha256(uuid4().bytes).hexdigest()
		sid, _ = store.admit(actor, digest, {**self.scope, "actor": actor}, "fp", kind=kind)
		self.assertTrue(store.mark_running(actor, sid))
		return sid

	def published(self, actor=None, parts=None):
		actor = actor or self.actor
		sid = self.running(actor)
		self.assertTrue(store.publish_snapshot(actor, sid, parts or self.parts))
		return sid

	def lock(self, sid):
		return store._active(store.load_status(self.actor, sid)["scope_hash"])

	def exists(self, actor, sid, name):
		return bool(frappe.cache.execute_command("EXISTS", store._key(actor, sid, name)))

	def stored_names(self):
		return ("result", *(f"part-{part}" for part in store.PARTS), *store.HASHES)

	def stored_keys_present(self, actor, sid):
		return [name for name in self.stored_names() if self.exists(actor, sid, name)]

	def test_publish_stores_every_part_and_completes_the_status(self):
		sid = self.published()
		self.assertEqual(store.load_status(self.actor, sid)["state"], "complete")
		for part in store.PARTS:
			stored = json.loads(store._json(self.parts[part]))
			self.assertEqual(store.load_part(self.actor, sid, part), stored)
		expected = [f"part-{p}" for p in store.PARTS] + list(store.HASHES)
		self.assertEqual(self.stored_keys_present(self.actor, sid), expected)

	def test_status_and_every_part_expire_together(self):
		sid = self.published()
		keys = [store._key(self.actor, sid, name) for name in ("status", *self.stored_names()[1:])]
		for key in keys:
			self.assertGreater(frappe.cache.ttl(key), 0)
			self.assertLessEqual(frappe.cache.ttl(key), store.RESULT_TTL)
		self.assertEqual(len({frappe.cache.ttl(key) for key in keys}), 1)

	def test_parts_are_stored_verbatim_so_empty_arrays_and_objects_survive(self):
		parts = store.split_result(schema2_result(payable=[], receivable=[], days={}), SCOPE)
		sid = self.published(parts=parts)
		self.assertEqual(store.load_part(self.actor, sid, "payable")["rows"], [])
		self.assertEqual(store.load_part(self.actor, sid, "status")["unavailable"], {})
		raw = frappe.cache.execute_command("GET", store._key(self.actor, sid, "part-payable"))
		self.assertEqual(raw.decode(), store._json(parts["payable"]))

	def test_a_publish_that_cannot_start_writes_no_part(self):
		cases = {
			"queued": lambda sid: None,
			"purged": lambda sid: store.purge_actor(self.actor),
			"lock_lost": lambda sid: frappe.cache.execute_command(
				"SET", self.lock(sid), "another-run", "EX", 60
			),
		}
		for name, break_it in cases.items():
			with self.subTest(case=name):
				if name == "queued":
					sid, _ = store.admit(self.actor, sha256(uuid4().bytes).hexdigest(), self.scope, "fp")
				else:
					sid = self.running()
					break_it(sid)
				self.assertFalse(store.publish_snapshot(self.actor, sid, self.parts))
				self.assertEqual(self.stored_keys_present(self.actor, sid), [])
				status = store.load_status(self.actor, sid)
				self.assertTrue(status is None or status["state"] != "complete")
				store.purge_actor(self.actor)

	def test_the_script_itself_refuses_a_run_that_is_not_running(self):
		# A stale read of the status must not publish: the script checks the state again, atomically.
		sid, _ = store.admit(self.actor, sha256(uuid4().bytes).hexdigest(), self.scope, "fp")
		stale = {**store.load_status(self.actor, sid), "state": "running"}
		with patch.object(store, "load_status", return_value=stale):
			self.assertFalse(store.publish_snapshot(self.actor, sid, self.parts))
		self.assertEqual(self.stored_keys_present(self.actor, sid), [])
		self.assertEqual(store.load_status(self.actor, sid)["state"], "queued")

	def test_publish_refuses_the_snapshot_kind_and_writes_nothing(self):
		sid = self.running()
		with self.assertRaises(ValueError):
			store.publish(self.actor, sid, {"summary": 1})
		self.assertEqual(store.load_status(self.actor, sid)["state"], "running")
		self.assertEqual(self.stored_keys_present(self.actor, sid), [])

	def test_publish_snapshot_refuses_a_balances_job_and_bad_part_sets(self):
		balances = self.running(kind="balances")
		with self.assertRaises(ValueError):
			store.publish_snapshot(self.actor, balances, self.parts)
		sid = self.running()
		for parts in ({}, {"payable": self.parts["payable"]}, {**self.parts, "extra": {}}):
			with self.subTest(parts=sorted(parts)), self.assertRaises(ValueError):
				store.publish_snapshot(self.actor, sid, parts)
		self.assertEqual(self.stored_keys_present(self.actor, sid), [])

	def test_discard_deletes_every_part_and_sets_a_tombstone_for_the_owner_only(self):
		sid = self.published()
		store.discard(self.actor, sid)
		self.assertIsNone(store.load_status(self.actor, sid))
		self.assertEqual(self.stored_keys_present(self.actor, sid), [])
		self.assertTrue(store.was_discarded(self.actor, sid))
		self.assertFalse(store.was_discarded(self.other, sid))
		ttl = frappe.cache.ttl(store._key(self.actor, sid, "discarded"))
		self.assertTrue(0 < ttl <= store.RESULT_TTL)

	def test_discarding_a_balances_job_leaves_no_tombstone(self):
		sid = self.running(kind="balances")
		self.assertTrue(store.publish(self.actor, sid, {"total": "1.00"}))
		store.discard(self.actor, sid)
		self.assertIsNone(store.load_status(self.actor, sid))
		self.assertFalse(store.was_discarded(self.actor, sid))
		self.assertFalse(self.exists(self.actor, sid, "discarded"))

	def test_a_status_row_without_a_kind_is_discarded_as_a_snapshot(self):
		sid = self.published()
		key = store._key(self.actor, sid, "status")
		row = store._read(key)
		del row["kind"]
		frappe.cache.execute_command("SET", key, store._json(row), "EX", 300)
		store.discard(self.actor, sid)
		self.assertTrue(store.was_discarded(self.actor, sid))

	def test_purge_deletes_every_part_of_the_actor_and_nothing_of_another(self):
		mine, theirs = self.published(), self.published(self.other)
		store.purge_actor(self.actor)
		self.assertEqual(self.stored_keys_present(self.actor, mine), [])
		self.assertEqual(
			len(self.stored_keys_present(self.other, theirs)), len(store.PARTS) + len(store.HASHES)
		)
		store.purge_site()
		self.assertEqual(self.stored_keys_present(self.other, theirs), [])

	def test_admission_cleanup_of_an_evicted_status_deletes_every_part(self):
		sid = self.published()
		frappe.cache.expire(store._key(self.actor, sid, "status"), 0)
		self.assertEqual(len(self.stored_keys_present(self.actor, sid)), len(store.PARTS) + len(store.HASHES))
		store.admit(self.actor, sha256(b"next").hexdigest(), self.scope, "fp")
		self.assertEqual(self.stored_keys_present(self.actor, sid), [])

	def test_a_missing_part_after_a_discard_is_a_changed_scope_for_the_owner(self):
		sid = self.published()
		store.discard(self.actor, sid)
		for part in store.PARTS:
			with self.subTest(part=part), self.assertRaises(ScopeChanged):
				store.load_part(self.actor, sid, part)

	def test_a_read_that_loaded_its_status_before_the_discard_sees_a_changed_scope(self):
		sid = self.published()
		status = store.load_status(self.actor, sid)
		store.discard(self.actor, sid)
		self.assertEqual(status["state"], "complete")
		with self.assertRaises(ScopeChanged):
			store.load_part(self.actor, sid, "payable")

	def test_an_evicted_part_without_a_tombstone_is_an_expired_snapshot(self):
		sid = self.published()
		for part in store.PARTS:
			with self.subTest(part=part):
				frappe.cache.expire(store._key(self.actor, sid, f"part-{part}"), 0)
				with self.assertRaises(SnapshotExpired):
					store.load_part(self.actor, sid, part)

	def test_another_actor_and_unknown_ids_get_expired_never_changed_scope(self):
		sid = self.published()
		store.discard(self.actor, sid)
		for actor, name in ((self.other, sid), (self.actor, uuid4().hex), (self.actor, "not-an-id")):
			with self.subTest(actor=actor == self.other), self.assertRaises(SnapshotExpired):
				store.load_part(actor, name, "payable")

	def test_a_part_is_not_delivered_for_a_running_or_balances_status(self):
		running, balances = self.running(), self.running(kind="balances")
		for sid in (running, balances):
			with self.subTest(sid=sid), self.assertRaises(SnapshotExpired):
				store.load_part(self.actor, sid, "payable")

	def test_an_unknown_part_name_is_refused(self):
		sid = self.published()
		for part in ("result", "status:x", ""):
			with self.subTest(part=part), self.assertRaises(ValueError):
				store.load_part(self.actor, sid, part)

	def test_a_part_is_not_delivered_to_another_site_name(self):
		sid = self.published()
		with patch.object(frappe.local, "site", "calendar-other-site"), self.assertRaises(SnapshotExpired):
			store.load_part(self.actor, sid, "payable")

	def test_load_result_assembles_the_parts_for_a_snapshot(self):
		sid = self.published()
		loaded = store.load_result(self.actor, sid)
		self.assertEqual(set(loaded), STATUS_PART_KEYS | {"sources"})
		self.assertEqual(set(loaded["sources"]), {"Payable", "Receivable"})
		stored = store.load_part(self.actor, sid, "payable")
		self.assertEqual(
			{k: v for k, v in loaded["sources"]["Payable"].items() if k != "index"}["canonical_total"],
			stored["canonical_total"],
		)
		self.assertEqual(
			[
				{k: v for k, v in row.items() if k != "_permission"}
				for row in loaded["sources"]["Payable"]["rows"]
			],
			stored["rows"],
		)
		self.assertEqual(loaded["sources"]["Payable"]["index"], self.parts["index-payable"])
		self.assertIsNone(store.load_result(self.other, sid))

	def test_load_result_gives_an_unavailable_side_as_none_not_as_eviction(self):
		parts = store.split_result(schema2_result(unavailable={"Receivable": "forbidden"}), SCOPE)
		sid = self.published(parts=parts)
		loaded = store.load_result(self.actor, sid)
		self.assertIsNone(loaded["sources"]["Receivable"])
		self.assertEqual(loaded["unavailable"], {"Receivable": "forbidden"})

	def test_load_result_is_none_when_an_available_part_was_evicted(self):
		for part in store.PARTS:
			with self.subTest(part=part):
				sid = self.published()
				frappe.cache.expire(store._key(self.actor, sid, f"part-{part}"), 0)
				self.assertIsNone(store.load_result(self.actor, sid))

	def test_no_snapshot_read_touches_a_result_key_but_a_balances_read_does(self):
		sid = self.published()
		job = self.running(kind="balances")
		self.assertTrue(store.publish(self.actor, job, {"total": "1.00"}))
		calls = []
		real = frappe.cache.execute_command

		def spy(*args, **kwargs):
			calls.append(args)
			return real(*args, **kwargs)

		with patch.object(frappe.cache, "execute_command", side_effect=spy):
			store.load_result(self.actor, sid)
			store.load_part(self.actor, sid, "payable")
			snapshot_reads = [key for command, key, *_ in calls if command == "GET"]
			calls.clear()
			self.assertEqual(store.load_result(self.actor, job), {"total": "1.00"})
			balances_reads = [key for command, key, *_ in calls if command == "GET"]
		self.assertFalse([key for key in snapshot_reads if b":result:" in key])
		self.assertTrue([key for key in balances_reads if b":result:" in key])


class TestRefusalCounter(TestCase):
	def setUp(self):
		require_calendar_test_site()
		self.actor = f"calendar-refusals-{uuid4().hex}@example.com"
		self.other = f"calendar-refusals-{uuid4().hex}@example.com"

	def tearDown(self):
		for actor in (self.actor, self.other):
			frappe.cache.delete(store._refusals(store._owner(actor)))

	def count(self, sid, actor=None):
		return store.count_refusal(actor or self.actor, sid)

	def test_a_new_snapshot_is_added_and_counted(self):
		self.assertEqual(self.count("a"), (1, 1))
		self.assertEqual(self.count("b"), (1, 2))
		self.assertEqual(self.count("c"), (1, 3))

	def test_a_repeated_snapshot_is_not_added_again(self):
		self.count("a")
		self.assertEqual(self.count("a"), (0, 1))
		self.assertEqual(self.count("a"), (0, 1))

	def test_the_window_starts_at_creation_and_later_adds_do_not_extend_it(self):
		self.count("a")
		self.assertTrue(0 < frappe.cache.ttl(store._refusals(store._owner(self.actor))) <= 300)
		frappe.cache.expire(store._refusals(store._owner(self.actor)), 100)
		self.count("b")
		self.assertLessEqual(frappe.cache.ttl(store._refusals(store._owner(self.actor))), 100)

	def test_each_owner_counts_alone(self):
		self.count("a")
		self.assertEqual(self.count("a", self.other), (1, 1))
