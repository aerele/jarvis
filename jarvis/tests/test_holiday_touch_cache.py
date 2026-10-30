"""Holiday touch cache.

Real Redis and a real Employee/Company/Holiday List. ``frappe.db.after_commit.run()`` and an
explicit ``frappe.db.rollback()`` stand in for a request's commit and rollback (no real commit).
"""

from __future__ import annotations

import ast
import inspect
import sys
from datetime import date, datetime, timezone
from unittest import mock

import frappe
from redis.exceptions import RedisError

from jarvis.holiday_touch import cache, resolve
from jarvis.tests.test_holiday_touch_resolve import (
	DIWALI,
	HAS_HRMS,
	IST,
	_assign,
	_Base,
	_company,
	_employee,
	_holiday_list,
	_now,
	_user,
)

NOW = _now(DIWALI)  # 11:30 in Kolkata on 2026-11-08
HOLIDAY_HANDLER = "jarvis.holiday_touch.cache.rotate_after_commit"


def _cached(user, now=NOW, zone=None):
	"""``cache.occasion_cached`` the way ``resolve.answer`` calls it."""
	zone = zone or resolve.zone_for(user)
	return cache.occasion_cached(user, zone, now, lambda: resolve.occasion(user, now, zone))


def _spy_set_value():
	"""A spy on the cache's set_value that still writes."""
	instance = frappe.cache()
	return mock.patch.object(instance, "set_value", wraps=instance.set_value)


def _ttls(spy) -> list:
	"""TTLs of the occasion writes under the prefix (not gen, the marker or the ph keys)."""
	keys = [(str(c.args[0]), c.kwargs.get("expires_in_sec")) for c in spy.call_args_list]
	return [
		ttl
		for key, ttl in keys
		if key.startswith(cache.PREFIX) and ":ph:" not in key and key != cache.GEN_KEY
	]


def _fail_for_holiday_keys(method):
	"""A cache method that raises for jarvis:holiday: keys only, so Frappe's own reads work."""

	def wrapper(key, *args, **kwargs):
		if str(key).startswith(cache.PREFIX):
			raise RedisError("down")
		return method(key, *args, **kwargs)

	return wrapper


class TestGen(_Base):
	def test_gen_is_a_random_token_created_on_first_use(self):
		frappe.cache().delete_value(cache.GEN_KEY)
		first = cache.current_gen()
		self.assertRegex(first, r"^[0-9a-f]{16}$")
		self.assertEqual(cache.current_gen(), first)

	def test_rotate_gives_a_new_random_token_not_a_counter(self):
		seen = {cache.current_gen()}
		for _ in range(5):
			cache.rotate()
			gen = cache.current_gen()
			self.assertRegex(gen, r"^[0-9a-f]{16}$")
			seen.add(gen)
		self.assertEqual(len(seen), 6)

	def test_a_stale_gen_write_is_not_read_after_rotation(self):
		user = self.person(dob="1990-11-08")
		self.assertEqual(_cached(user, NOW)["kind"], "birthday")
		cache.rotate()
		sentinel = resolve._none("sentinel")
		with mock.patch.object(resolve, "occasion", return_value=sentinel) as compute:
			self.assertEqual(_cached(user, NOW)["reason"], "sentinel")
		compute.assert_called_once()


class TestPersistence(_Base):
	def test_the_prefix_is_a_persistent_cache_key(self):
		self.assertIn("jarvis:holiday:", frappe.get_hooks("persistent_cache_keys"))

	def test_answers_survive_clear_cache(self):
		user = self.person(dob="1990-11-08")
		gen = cache.current_gen()
		self.assertEqual(resolve.answer(user, NOW)["kind"], "birthday")
		frappe.clear_cache()
		self.assertEqual(cache.current_gen(), gen)
		with mock.patch.object(resolve, "occasion", side_effect=AssertionError("recomputed")):
			self.assertEqual(resolve.answer(user, NOW)["kind"], "birthday")

	def test_the_placeholder_set_survives_clear_cache(self):
		calls = []
		compute = lambda: calls.append(1) or {date(1990, 11, 8)}  # noqa: E731
		cache.placeholder_dates("Co", DIWALI, compute)
		frappe.clear_cache()
		cache.placeholder_dates("Co", DIWALI, compute)
		self.assertEqual(len(calls), 1)


class TestTtl(_Base):
	def test_positive_ttl_is_at_most_two_hours_and_at_most_the_time_to_local_midnight(self):
		cases = (
			(datetime(2026, 11, 8, 6, 0, tzinfo=timezone.utc), IST, 7200),  # 11:30 local
			(datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc), IST, 1800),  # 23:30 local
			(datetime(2026, 11, 8, 18, 29, 30, tzinfo=timezone.utc), IST, 30),  # 23:59:30 local
			(datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc), "America/New_York", 7200),
		)
		for now, zone, expected in cases:
			with self.subTest(now=now, zone=zone):
				self.assertEqual(cache.positive_ttl(now, zone), expected)

	def test_an_answer_is_stored_with_that_ttl(self):
		user = self.person(dob="1990-11-08", tz=IST)
		with _spy_set_value() as spy:
			_cached(user, datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc))
		self.assertEqual(_ttls(spy), [1800])

	def test_an_answer_is_never_stored_for_longer_than_two_hours(self):
		user = self.person(dob="1990-11-08")
		with _spy_set_value() as spy:
			_cached(user, NOW)
		ttls = _ttls(spy)
		self.assertEqual(len(ttls), 1)
		self.assertLessEqual(ttls[0], 2 * 3600)


class TestOccasionKey(_Base):
	def _computes(self, user, nows):
		with mock.patch.object(resolve, "occasion", wraps=resolve.occasion) as compute:
			for now in nows:
				_cached(user, now)
		return compute.call_count

	def test_the_same_user_zone_and_date_hits(self):
		user = self.person(dob="1990-11-08")
		self.assertEqual(self._computes(user, [NOW, NOW]), 1)

	def test_a_different_date_misses(self):
		user = self.person(dob="1990-11-08")
		self.assertEqual(self._computes(user, [NOW, _now(date(2026, 11, 9))]), 2)

	def test_a_different_zone_misses(self):
		user = self.person(dob="1990-11-08")
		self.assertEqual(self._computes(user, [NOW]), 1)
		frappe.db.set_value("User", user, "time_zone", "Asia/Tokyo")  # still 8 Nov at NOW
		self.assertEqual(self._computes(user, [NOW]), 1)
		self.assertEqual(self._computes(user, [NOW]), 0)

	def test_a_different_user_misses(self):
		first, second = self.person(dob="1990-11-08"), self.person(dob="1990-11-08")
		self.assertEqual(self._computes(first, [NOW]) + self._computes(second, [NOW]), 2)


class TestNegativeCache(_Base):
	def test_an_error_is_remembered_for_at_most_ten_minutes_and_logged_once(self):
		user = self.person(dob="1990-11-08")
		with (
			mock.patch.object(resolve, "occasion", side_effect=RuntimeError("boom")) as compute,
			mock.patch("frappe.log_error") as log,
			_spy_set_value() as spy,
		):
			for _ in range(3):
				self.assertEqual(resolve.safe_answer(user, NOW)["kind"], "none")
		self.assertEqual(compute.call_count, 1)
		self.assertEqual(log.call_count, 1)
		self.assertEqual(_ttls(spy), [600])
		with mock.patch.object(resolve, "occasion", side_effect=AssertionError("recomputed")):
			out = _cached(user, NOW)
		self.assertEqual((out["kind"], out["reason"]), ("none", "error"))

	def test_a_holiday_error_answer_is_cached_for_ten_minutes_not_the_positive_ttl(self):
		user = self.person(dob="1990-11-08")
		failed = resolve._none("holiday_error")
		with (
			mock.patch.object(resolve, "occasion", return_value=failed) as compute,
			_spy_set_value() as spy,
		):
			for _ in range(3):
				self.assertEqual(_cached(user, NOW)["reason"], "holiday_error")
		self.assertEqual(compute.call_count, 1)
		self.assertEqual(_ttls(spy), [cache.ERROR_TTL])

	def test_an_error_entry_is_not_served_after_a_rotation(self):
		user = self.person(dob="1990-11-08")
		with mock.patch.object(resolve, "occasion", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				_cached(user, NOW)
			self.assertEqual(_cached(user, NOW)["reason"], "error")
		cache.rotate()
		out = _cached(user, NOW)
		self.assertEqual((out["kind"], out["reason"]), ("birthday", "birthday"))

	def test_a_cache_outage_is_not_an_error(self):
		user = self.person(dob="1990-11-08")
		instance = frappe.cache()
		for name in ("get_value", "set_value"):
			with self.subTest(method=name):
				flaky = _fail_for_holiday_keys(getattr(instance, name))
				with mock.patch.object(instance, name, side_effect=flaky):
					self.assertEqual(_cached(user, NOW)["kind"], "birthday")


class TestHitValidation(_Base):
	def _store(self, user, value):
		key = cache._occasion_key(user, IST, NOW)
		frappe.cache().set_value(key, value, expires_in_sec=600)

	def test_an_entry_missing_a_required_key_is_a_miss(self):
		user = self.person(dob="1990-11-08")
		old_shape = {"kind": "none", "design": None, "occasion": None}  # an older release's entry
		for bad in (old_shape, {}, ["not", "a", "dict"], "x"):
			with self.subTest(bad=bad):
				self._store(user, bad)
				out = _cached(user)
				self.assertEqual((out["kind"], out["reason"]), ("birthday", "birthday"))

	def test_a_bad_fallback_makes_the_entry_a_miss(self):
		user = self.person(dob="1990-11-08")
		good = resolve._none("sentinel")
		self._store(user, {**good, "fallback": {"kind": "day"}})
		self.assertEqual(_cached(user)["reason"], "birthday")
		self._store(user, {**good, "fallback": resolve._none("inner")})
		self.assertEqual(_cached(user)["reason"], "sentinel")  # a complete entry is a hit

	def test_the_cache_module_does_not_import_resolve(self):
		imported = set()
		for node in ast.walk(ast.parse(inspect.getsource(cache))):
			if isinstance(node, ast.ImportFrom):
				imported |= {a.name for a in node.names} | {node.module}
			elif isinstance(node, ast.Import):
				imported |= {a.name for a in node.names}
		self.assertFalse({n for n in imported if "resolve" in n})


class TestPlaceholderCache(_Base):
	def test_stores_dates_only_and_a_rotation_invalidates_it(self):
		company = _company("India")
		for _ in range(5):
			_employee(company, "1990-11-08")
		user = _user()
		_employee(company, "1990-11-08", user)
		self.assertEqual(resolve.occasion(user, NOW)["reason"], "placeholder_dob")
		key = f"{cache.PREFIX}{cache.current_gen()}:ph:{company}:{DIWALI.isoformat()}"
		self.assertEqual(frappe.cache().get_value(key, expires=True), ["1990-11-08"])
		calls = []
		real = resolve._placeholder_dates
		with mock.patch.object(
			resolve, "_placeholder_dates", side_effect=lambda c: calls.append(c) or real(c)
		):
			resolve.occasion(user, NOW)
			self.assertEqual(calls, [])
			cache.rotate()
			resolve.occasion(user, NOW)
			self.assertEqual(calls, [company])

	def test_a_rotation_makes_the_helper_recompute(self):
		calls = []
		compute = lambda: calls.append(1) or {date(1990, 11, 8)}  # noqa: E731
		self.assertEqual(cache.placeholder_dates("Co", DIWALI, compute), {date(1990, 11, 8)})
		cache.placeholder_dates("Co", DIWALI, compute)
		self.assertEqual(len(calls), 1)
		cache.rotate()
		cache.placeholder_dates("Co", DIWALI, compute)
		self.assertEqual(len(calls), 2)


class TestRotationAfterCommit(_Base):
	def _reset_queue(self):
		"""Forget queued callbacks and the once-per-transaction guard (as a commit would)."""
		frappe.db.after_commit.reset()
		frappe.flags.pop(cache.QUEUED_FLAG, None)

	def _fixtures(self):
		company = _company("India")
		user = self.person(dob="1990-11-08")
		employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
		holiday_list = _holiday_list([(DIWALI, "Diwali", 0)])
		self._reset_queue()
		return company, employee, holiday_list

	def _events(self):
		company, employee, holiday_list = self._fixtures()
		cases = [
			("Employee", employee, "on_update"),
			("Employee", employee, "on_trash"),
			("Company", company, "on_update"),
			("Holiday List", holiday_list, "on_update"),
			("Holiday List", holiday_list, "on_trash"),
		]
		if HAS_HRMS and frappe.db.exists("DocType", "Holiday List Assignment"):
			_assign(employee, company, holiday_list)
			name = frappe.db.get_value("Holiday List Assignment", {"assigned_to": employee}, "name")
			cases += [
				("Holiday List Assignment", name, "on_submit"),
				("Holiday List Assignment", name, "on_cancel"),
			]
			self._reset_queue()
		return cases

	def _fire(self, dt, name, event):
		"""Run the doc event the way a save would. Company's own ``on_update`` builds
		warehouses this site has no types for, so only its hooks are run, with a country
		change (a Company save that changed nothing relevant does not rotate)."""
		doc = frappe.get_doc(dt, name)
		if dt == "Company":
			doc.load_doc_before_save()
			doc.country = "United States"
		self._fire_doc(doc, event)

	def _save_with(self, dt, name, **changes):
		"""A save with the given changes, so ``has_value_changed`` is real (a document with no
		earlier copy counts as new). Employee and Holiday List are really saved; Company only
		runs its hooks, as ``_fire`` does."""
		doc = frappe.get_doc(dt, name)
		doc.update(changes)
		if dt == "Company":
			doc.load_doc_before_save()
			self._fire_doc(doc, "on_update")
		else:
			doc.save(ignore_permissions=True)

	def _fire_doc(self, doc, event):
		if doc.doctype == "Company":
			with mock.patch.object(type(doc), event, lambda self: None, create=True):
				doc.run_method(event)
		else:
			doc.run_method(event)

	def _rotated_by(self, dt, name, **changes) -> bool:
		self._reset_queue()
		before = cache.current_gen()
		self._save_with(dt, name, **changes)
		frappe.db.after_commit.run()
		return cache.current_gen() != before

	def test_an_employee_save_rotates_only_when_a_relevant_field_changed(self):
		_, employee, _ = self._fixtures()
		self.assertFalse(self._rotated_by("Employee", employee, middle_name="Quokka"))
		for field, value in (
			("date_of_birth", "1991-01-01"),
			("status", "Inactive"),
			("user_id", None),
			("company", _company("United States")),
		):
			with self.subTest(field=field):
				self.assertTrue(self._rotated_by("Employee", employee, **{field: value}))

	def test_a_company_save_rotates_only_when_country_or_default_list_changed(self):
		company, _, _ = self._fixtures()
		self.assertFalse(self._rotated_by("Company", company, website="https://example.com"))
		self.assertTrue(self._rotated_by("Company", company, country="United States"))
		self.assertTrue(self._rotated_by("Company", company, default_holiday_list="Some List"))

	def test_a_holiday_list_save_always_rotates(self):
		_, _, holiday_list = self._fixtures()
		self.assertTrue(self._rotated_by("Holiday List", holiday_list, color="#ffffff"))

	def test_a_new_employee_rotates(self):
		self._fixtures()
		self._reset_queue()
		before = cache.current_gen()
		_employee(_company("India"), "1990-01-01")
		frappe.db.after_commit.run()
		self.assertNotEqual(cache.current_gen(), before)

	def test_one_transaction_queues_one_rotation(self):
		_, employee, holiday_list = self._fixtures()
		self._reset_queue()
		with mock.patch.object(cache, "rotate", wraps=cache.rotate) as rotate:
			for _ in range(20):
				self._save_with("Holiday List", holiday_list, color="#ffffff")
			frappe.db.after_commit.run()
		self.assertEqual(rotate.call_count, 1)

	def test_the_guard_resets_after_a_commit_and_after_a_rollback(self):
		for end in (frappe.db.after_commit.run, frappe.db.rollback):
			with self.subTest(end=end.__name__):
				_, _, holiday_list = self._fixtures()  # a rollback undoes the fixtures: rebuild
				self._reset_queue()
				self._save_with("Holiday List", holiday_list, color="#ffffff")
				self.assertTrue(frappe.flags.get(cache.QUEUED_FLAG))
				end()
				self.assertFalse(frappe.flags.get(cache.QUEUED_FLAG))
				# Frappe 15's CallbackManager has no __len__; its queue is the same deque on 15 and 16.
				self.assertEqual(len(frappe.db.after_commit._functions), 0)
		_, _, holiday_list = self._fixtures()
		self.assertTrue(self._rotated_by("Holiday List", holiday_list, color="#000000"))  # not stuck

	def test_the_handler_is_declared_for_every_event(self):
		hooks = frappe.get_hooks("doc_events")
		for dt, events in {
			"Holiday List": ("on_update", "on_trash"),
			"Holiday List Assignment": ("on_submit", "on_cancel"),
			"Company": ("on_update",),
			"Employee": ("on_update", "on_trash"),
		}.items():
			for event in events:
				with self.subTest(dt=dt, event=event):
					self.assertIn(HOLIDAY_HANDLER, hooks[dt][event])

	def test_each_event_rotates_after_commit_and_not_before(self):
		for dt, name, event in self._events():
			with self.subTest(dt=dt, event=event):
				self._reset_queue()
				before = cache.current_gen()
				self._fire(dt, name, event)
				self.assertEqual(cache.current_gen(), before)  # nothing before the commit
				frappe.db.after_commit.run()
				self.assertNotEqual(cache.current_gen(), before)

	def test_each_event_does_not_rotate_after_a_rollback(self):
		for index in range(len(self._events())):
			dt, name, event = self._events()[index]  # a rollback undoes the fixtures: rebuild
			with self.subTest(dt=dt, event=event):
				before = cache.current_gen()
				self._fire(dt, name, event)
				frappe.db.rollback()
				frappe.db.after_commit.run()
				self.assertEqual(cache.current_gen(), before)

	def test_100_employee_saves_use_no_keys_scan(self):
		_, employee, _ = self._fixtures()
		scans = []
		real = type(frappe.cache()).delete_keys

		def spy(this, *args, **kwargs):
			caller = sys._getframe(1).f_code.co_filename
			if "holiday_touch" in caller:
				scans.append(caller)
			return real(this, *args, **kwargs)

		before = cache.current_gen()
		with mock.patch.object(type(frappe.cache()), "delete_keys", spy):
			for n in range(100):
				doc = frappe.get_doc("Employee", employee)
				doc.date_of_birth = "1990-11-08" if n % 2 else "1990-11-09"
				doc.save()
			frappe.db.after_commit.run()
		self.assertEqual(scans, [])
		self.assertNotEqual(cache.current_gen(), before)  # and one rotation covered all 100

	def test_the_cache_module_never_scans_keys(self):
		source = inspect.getsource(cache)
		for banned in ("delete_keys(", "get_keys(", ".keys("):
			self.assertNotIn(banned, source)

	def test_a_redis_error_in_rotate_leaves_the_save_and_later_callbacks_alone(self):
		_, employee, _ = self._fixtures()
		instance = frappe.cache()
		real = instance.set_value

		def flaky(key, *args, **kwargs):
			if key == cache.GEN_KEY:
				raise RedisError("down")
			return real(key, *args, **kwargs)

		ran = []
		with mock.patch.object(instance, "set_value", side_effect=flaky), mock.patch("frappe.log_error"):
			frappe.get_doc("Employee", employee).save()  # the save itself is fine
			frappe.db.after_commit.add(lambda: ran.append("later"))
			frappe.db.after_commit.run()  # run() does not catch: rotate must not raise
		self.assertEqual(ran, ["later"])

	def test_the_handlers_never_raise(self):
		broken = mock.Mock(add=mock.Mock(side_effect=RuntimeError("x")))
		with mock.patch.object(frappe.db, "after_commit", broken):
			cache.rotate_after_commit(None, "on_update")
		with mock.patch.object(frappe, "cache", side_effect=RuntimeError("x")):
			cache.rotate()
			cache.on_after_migrate()


class TestAfterMigrate(_Base):
	def test_it_is_declared(self):
		self.assertIn("jarvis.holiday_touch.cache.on_after_migrate", frappe.get_hooks("after_migrate"))

	def test_it_rotates_and_the_next_read_recomputes(self):
		user = self.person(dob="1990-11-08")
		self.assertEqual(resolve.answer(user, NOW)["kind"], "birthday")
		before = cache.current_gen()
		cache.on_after_migrate()
		self.assertNotEqual(cache.current_gen(), before)
		with mock.patch.object(resolve, "occasion", wraps=resolve.occasion) as compute:
			self.assertEqual(resolve.answer(user, NOW)["kind"], "birthday")
		compute.assert_called_once()

	def test_it_swallows_every_error(self):
		with mock.patch.object(cache, "rotate", side_effect=RuntimeError("x")):
			cache.on_after_migrate()
		instance = frappe.cache()
		flaky = _fail_for_holiday_keys(instance.set_value)
		with mock.patch.object(instance, "set_value", side_effect=flaky), mock.patch("frappe.log_error"):
			cache.on_after_migrate()
