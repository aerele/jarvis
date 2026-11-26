"""Payment Calendar S2 T3: the reader switch in Jarvis Settings and ``routing.reader_settings()``.

The three fields are hidden, permlevel 2 and Administrator-only. Nothing here commits a mode:
values live in the test transaction and roll back. The fields reach ``test_jarvis`` through
``reload_doc`` only (never a migrate of a shared site).

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_routing
"""

import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.jarvis.doctype.jarvis_settings import jarvis_settings
from jarvis.payment_calendar import routing
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.snapshot import consistent_read, view_is_open
from jarvis.tools._preview_sandbox import preview_sandbox

SETTINGS = "Jarvis Settings"
MODE = "payment_calendar_reader"
SERVED = "payment_calendar_reader_companies"
SHADOW = "payment_calendar_reader_shadow_companies"
FIELDS = (MODE, SERVED, SHADOW)
SYSMAN = "pc-routing-sysman@example.com"


@contextmanager
def _as(user):
	original = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(original)


def _ensure_system_manager(email):
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "pcrouting",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	if "System Manager" not in frappe.get_roles(email):
		frappe.get_doc("User", email).add_roles("System Manager")
	frappe.db.commit()


def _raw(field, value):
	"""Write a Singles row directly: a stale or hand-edited value that never went through validate."""
	frappe.db.sql("DELETE FROM `tabSingles` WHERE doctype=%s AND field=%s", (SETTINGS, field))
	frappe.db.sql(
		"INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s, %s, %s)", (SETTINGS, field, value)
	)


def _save_as_administrator(**values):
	"""The documented change procedure: Administrator, get_single, set, save."""
	with _as("Administrator"):
		settings = frappe.get_single(SETTINGS)
		for field, value in values.items():
			settings.set(field, value)
		# Frappe skips the Version row under test by default; the history is part of the contract.
		settings.save(ignore_version=False)
	return settings


def _stored(field):
	rows = frappe.db.sql("SELECT value FROM `tabSingles` WHERE doctype=%s AND field=%s", (SETTINGS, field))
	return rows[0][0] if rows else None


def _version_count():
	return frappe.db.count("Version", {"ref_doctype": SETTINGS})


def _existing_companies(count):
	return frappe.get_all("Company", pluck="name", order_by="name", limit=count)


def _companies(count):
	"""`count` company names: the site's own, and fresh bare rows (this test's transaction) for the rest.

	A CI site can have one company or none. The settings check only asks whether a name exists, so a bare row
	made by `db_insert` (no accounts, no hooks) is enough; `tearDown` rolls it back.
	"""
	names = _existing_companies(count)
	for index in range(len(names), count):
		name = f"_PCR Company {frappe.generate_hash(length=8)} {index}"
		doc = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": name,
				"abbr": f"PCR{frappe.generate_hash(length=4)}".upper(),
				"default_currency": "INR",
				"country": "India",
			}
		)
		doc.name = name
		doc.db_insert()
		_made_companies.append(name)
		names.append(name)
	return names


# Fresh company rows of this run: a settings save can commit, so `tearDown` deletes them by name.
_made_companies: list[str] = []


def _remove_made_companies():
	frappe.db.rollback()
	while _made_companies:
		frappe.db.delete("Company", {"name": _made_companies.pop()})
	frappe.db.commit()


_start_settings = []


def setUpModule():
	# The reader switch as the site has it now: the last test checks that the run left it as it was.
	_start_settings.append(routing.reader_settings())


class RoutingSettingsCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		frappe.reload_doc("jarvis", "doctype", "jarvis_settings")
		frappe.clear_cache(doctype=SETTINGS)
		_ensure_system_manager(SYSMAN)

	def setUp(self):
		frappe.set_user("Administrator")
		# The settings save runs the unified LLM path when a model row or a preset exists, and a test earlier in a
		# shard can leave one (an enabled model with no key refuses every save). These tests are about the reader
		# fields, so they start from the plain state, inside this test's transaction (rolled back in tearDown).
		frappe.db.delete("Jarvis LLM Pool Model", {"parent": SETTINGS, "parenttype": SETTINGS})
		frappe.db.set_single_value(SETTINGS, "preset", "")

	def tearDown(self):
		frappe.set_user("Administrator")
		_remove_made_companies()


class TestReaderSettingsFields(RoutingSettingsCase):
	def test_fields_are_hidden_permlevel_two_and_default_off(self):
		meta = frappe.get_meta(SETTINGS)
		for fieldname in FIELDS:
			df = meta.get_field(fieldname)
			self.assertEqual((df.hidden, df.permlevel), (1, 2), fieldname)
			self.assertIn(fieldname, jarvis_settings.JarvisSettings._ADMIN_ONLY_FIELDS)
		self.assertEqual(meta.get_field(MODE).fieldtype, "Select")
		self.assertEqual(meta.get_field(MODE).options.split("\n"), ["off", "shadow", "on"])
		self.assertEqual(meta.get_field(MODE).default, "off")
		self.assertEqual(meta.get_field(SERVED).fieldtype, "Small Text")
		self.assertEqual(meta.get_field(SHADOW).fieldtype, "Small Text")

	def test_defaults_read_as_off_with_empty_lists(self):
		for field in FIELDS:
			frappe.db.sql("DELETE FROM `tabSingles` WHERE doctype=%s AND field=%s", (SETTINGS, field))
		self.assertEqual(routing.reader_settings(), routing.ReaderSettings("off", (), ()))

	def test_a_fresh_settings_doc_defaults_the_mode_to_off(self):
		self.assertEqual(frappe.new_doc(SETTINGS).get(MODE), "off")


class TestReaderSettingsRead(RoutingSettingsCase):
	def test_lines_are_stripped_and_blanks_dropped(self):
		_raw(MODE, "on")
		_raw(SERVED, "  Alpha Co \n\n\t\nBeta Co\r\n   ")
		_raw(SHADOW, "Gamma Co")
		self.assertEqual(
			routing.reader_settings(), routing.ReaderSettings("on", ("Alpha Co", "Beta Co"), ("Gamma Co",))
		)

	def test_names_are_exact_not_case_folded(self):
		_raw(MODE, "on")
		_raw(SERVED, "alpha co")
		self.assertEqual(routing.reader_settings().served, ("alpha co",))
		self.assertNotIn("Alpha Co", routing.reader_settings().served)

	def test_an_unknown_mode_value_reads_as_off(self):
		for value in ("ON", "enabled", "1", "", "shadow "):
			_raw(MODE, value)
			self.assertEqual(routing.reader_settings().mode, "off", repr(value))

	def test_each_known_mode_reads_back(self):
		for value in ("off", "shadow", "on"):
			_raw(MODE, value)
			self.assertEqual(routing.reader_settings().mode, value)

	def test_a_missing_field_reads_as_off_without_a_read(self):
		meta = MagicMock()
		meta.has_field.return_value = False
		with (
			patch.object(frappe, "get_meta", return_value=meta),
			patch.object(frappe.db, "get_single_value") as read,
		):
			self.assertEqual(routing.reader_settings(), routing.ReaderSettings("off", (), ()))
		read.assert_not_called()

	def test_any_error_reads_as_off(self):
		_raw(MODE, "on")
		errors = (
			RuntimeError("boom"),
			frappe.db.OperationalError(1054, "Unknown column"),
			frappe.db.OperationalError(2013, "lost"),
		)
		for exc in errors:
			with patch.object(frappe.db, "get_single_value", side_effect=exc):
				self.assertEqual(routing.reader_settings(), routing.ReaderSettings("off", (), ()))

	def test_a_job_deadline_is_not_swallowed(self):
		from rq.timeouts import JobTimeoutException

		_raw(MODE, "on")
		for exc in (TimeoutError(), JobTimeoutException()):
			with self.subTest(type(exc).__name__):
				with patch.object(frappe.db, "get_single_value", side_effect=exc):
					with self.assertRaises(type(exc)) as raised:
						routing.reader_settings()
				self.assertIs(raised.exception, exc)

	def test_the_mode_is_read_first_and_the_lists_are_skipped_when_it_is_off_on_request(self):
		_raw(MODE, "off")
		_raw(SERVED, "Alpha Co")
		with patch.object(frappe.db, "get_single_value", wraps=frappe.db.get_single_value) as read:
			self.assertEqual(
				routing.reader_settings(lists_when_off=False), routing.ReaderSettings("off", (), ())
			)
		self.assertEqual([call.args[1] for call in read.call_args_list], [MODE])
		# The default still reads the lists (the shadow's daily row reports a company that was switched off).
		self.assertEqual(routing.reader_settings().served, ("Alpha Co",))

	def test_a_deadlock_in_the_settings_read_inside_a_view_breaks_the_view(self):
		"""The read answers `off` like any error, but the job must not go on with a view that was rolled back."""
		with self.assertRaises(CalendarSourceUnavailable) as raised:
			with consistent_read():
				with patch.object(frappe.db, "get_single_value", side_effect=frappe.QueryDeadlockError("x")):
					self.assertEqual(routing.reader_settings(), routing.ReaderSettings("off", (), ()))
		self.assertEqual(raised.exception.code, "source_changed")

	def test_a_statement_error_in_the_settings_read_leaves_the_view_intact(self):
		with consistent_read():
			with patch.object(
				frappe.db, "get_single_value", side_effect=frappe.db.OperationalError(1054, "Unknown column")
			):
				self.assertEqual(routing.reader_settings(), routing.ReaderSettings("off", (), ()))

	def test_a_deadlock_outside_a_view_changes_nothing(self):
		with patch.object(frappe.db, "get_single_value", side_effect=frappe.QueryDeadlockError("x")):
			self.assertEqual(routing.reader_settings(), routing.ReaderSettings("off", (), ()))

	def test_a_listed_name_that_no_company_has_is_listed_as_missing(self):
		(company,) = _companies(1)
		current = routing.ReaderSettings("on", (company, "Ghost Co"), ("Ghost Co", "Another Ghost"))
		self.assertEqual(routing.listed_but_missing(current), ("Another Ghost", "Ghost Co"))
		self.assertEqual(routing.listed_but_missing(routing.ReaderSettings("on", (company,), ())), ())
		self.assertEqual(routing.listed_but_missing(routing.ReaderSettings("off", (), ())), ())

	def test_the_save_only_check_and_the_llm_classifier_share_one_field_list(self):
		import inspect

		self.assertEqual(jarvis_settings.LLM_STRUCTURAL_FIELDS, ("llm_provider", "llm_model", "llm_base_url"))
		for method in ("_is_reader_routing_save_only", "_classify_llm_change"):
			source = inspect.getsource(getattr(jarvis_settings.JarvisSettings, method))
			self.assertIn("LLM_STRUCTURAL_FIELDS", source, method)

	def test_values_are_fresh_on_every_call(self):
		_raw(MODE, "shadow")
		self.assertEqual(routing.reader_settings().mode, "shadow")
		# Prime the per-process single-value cache, then change the row behind its back.
		self.assertEqual(frappe.db.get_single_value(SETTINGS, MODE), "shadow")
		_raw(MODE, "on")
		self.assertEqual(routing.reader_settings().mode, "on")

	def test_a_default_read_inside_the_view_gives_off_and_leaves_the_view_unbroken(self):
		# The fields hold no row on a site that never saved them; the view starts with a cold meta.
		frappe.clear_cache(doctype=SETTINGS)
		saved = routing.reader_settings()  # the committed value, whatever this site has
		frappe.clear_cache(doctype=SETTINGS)
		with consistent_read():
			self.assertEqual(routing.reader_settings(), saved)
			# A second read in the same view, with the meta now cached.
			self.assertEqual(routing.reader_settings(), saved)


class TestReaderSettingsSave(RoutingSettingsCase):
	def test_administrator_save_is_accepted_with_one_version_row(self):
		served, shadow = _companies(2)
		before = _version_count()
		_save_as_administrator(**{MODE: "shadow", SERVED: served, SHADOW: shadow})
		self.assertEqual(_version_count(), before + 1)
		self.assertEqual(routing.reader_settings(), routing.ReaderSettings("shadow", (served,), (shadow,)))

	def test_a_save_normalizes_the_lists(self):
		first, second = _companies(2)
		_save_as_administrator(**{SERVED: f"  {first}  \n\n{second}\n  \n"})
		self.assertEqual(_stored(SERVED), f"{first}\n{second}")

	def test_a_system_manager_save_cannot_change_any_field(self):
		(company,) = _companies(1)
		before = routing.reader_settings()  # whatever the site has saved: the saves below must not change it
		for field, value in ((MODE, "on"), (SERVED, company), (SHADOW, company)):
			with _as(SYSMAN), patch("frappe.enqueue"):
				settings = frappe.get_single(SETTINGS)
				settings.set(field, value)
				settings.save()
			self.assertNotEqual(_stored(field), value, field)
		self.assertEqual(routing.reader_settings(), before)

	def test_a_self_granted_permlevel_write_is_still_refused(self):
		from frappe import permissions as frappe_permissions
		from frappe.core.page.permission_manager import permission_manager

		(company,) = _companies(1)
		with _as(SYSMAN):
			permission_manager.update(SETTINGS, "System Manager", 2, "write", 1)
		frappe.db.commit()
		try:
			self.assertIn(2, frappe.get_meta(SETTINGS).get_permlevel_access("write", user=SYSMAN))
			for field, value in ((MODE, "on"), (SERVED, company), (SHADOW, company)):
				with _as(SYSMAN), patch("frappe.enqueue"), self.assertRaises(frappe.PermissionError):
					settings = frappe.get_single(SETTINGS)
					settings.set(field, value)
					settings.save()
		finally:
			# Drop anything a refused save left in this transaction before the commit below.
			frappe.db.rollback()
			frappe_permissions.reset_perms(SETTINGS)
			frappe.clear_cache(doctype=SETTINGS)
			frappe.db.commit()

	def test_a_system_manager_save_of_other_fields_is_not_refused(self):
		with _as(SYSMAN), patch("frappe.enqueue"):
			frappe.get_single(SETTINGS).save()

	def test_an_added_unknown_company_is_refused_in_either_list(self):
		for field in (SERVED, SHADOW):
			with self.assertRaises(frappe.ValidationError, msg=field):
				_save_as_administrator(**{field: "No Such Company Ltd"})

	def test_the_refusal_escapes_the_typed_name(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			_save_as_administrator(**{SERVED: "<img src=x onerror=alert(1)>"})
		self.assertNotIn("<img", str(raised.exception))
		self.assertIn("&lt;img", str(raised.exception))

	def test_a_company_name_must_match_exactly(self):
		(company,) = _companies(1)
		wrong_case = company.swapcase()
		self.assertNotEqual(wrong_case, company)
		with self.assertRaises(frappe.ValidationError):
			_save_as_administrator(**{SERVED: wrong_case})

	def test_a_valid_line_added_beside_an_unknown_one_is_still_refused(self):
		(company,) = _companies(1)
		with self.assertRaises(frappe.ValidationError):
			_save_as_administrator(**{SERVED: f"{company}\nNo Such Company Ltd"})

	def test_a_stale_line_does_not_block_a_save_that_adds_a_known_one(self):
		first, second = _companies(2)
		_raw(SERVED, f"Deleted Company Ltd\n{first}")
		_save_as_administrator(**{SERVED: f"Deleted Company Ltd\n{first}\n{second}"})
		self.assertEqual(_stored(SERVED), f"Deleted Company Ltd\n{first}\n{second}")

	def test_a_stale_line_does_not_block_a_save_of_another_field(self):
		_raw(SERVED, "Deleted Company Ltd")
		_raw(SHADOW, "Also Deleted Ltd")
		_raw(MODE, "shadow")
		with patch.object(jarvis_settings.JarvisSettings, "_on_update_single_model_legacy"):
			settings = _save_as_administrator(payment_calendar_chat_handoff=1)
		self.assertEqual(settings.get(SERVED), "Deleted Company Ltd")
		self.assertEqual(frappe.utils.cint(_stored("payment_calendar_chat_handoff")), 1)
		self.assertEqual(routing.reader_settings().mode, "shadow")


class TestReaderSettingsSaveIsInert(RoutingSettingsCase):
	"""A save of only the reader fields must not run any LLM sync, even on a site whose last sync did not end ok."""

	def test_a_save_of_only_the_reader_fields_enqueues_nothing_and_leaves_the_sync_status(self):
		(company,) = _companies(1)
		failed = "failed: pc routing test"
		frappe.db.set_single_value(SETTINGS, "last_sync_status", failed)
		with (
			patch("frappe.enqueue") as enqueue,
			patch.object(jarvis_settings, "_switch_or_enqueue") as switch_or_enqueue,
			patch.object(jarvis_settings, "_write_settings_fields") as write_fields,
			patch("jarvis.chat.wiki_mirror.enqueue_scrub") as scrub,
		):
			before = _version_count()
			_save_as_administrator(**{MODE: "shadow", SERVED: company})
		enqueue.assert_not_called()
		switch_or_enqueue.assert_not_called()
		write_fields.assert_not_called()
		scrub.assert_not_called()
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "last_sync_status", cache=False), failed)
		self.assertEqual(_version_count(), before + 1)
		self.assertEqual(routing.reader_settings().mode, "shadow")

	def test_a_save_that_also_changes_the_llm_config_still_syncs(self):
		# A model makes the legacy single-model path "configured", and api_key mode (the default) then requires
		# a key. A CI site has none, so the save carries a valid config of its own. The key and the mode live in
		# this test's transaction and roll back with it.
		llm_config = {
			"llm_model": "pc-routing-test-model",
			"llm_auth_mode": "api_key",
			"llm_api_key": "pc-routing-test-key",
		}
		with patch.object(jarvis_settings.JarvisSettings, "_on_update_single_model_legacy") as legacy:
			_save_as_administrator(**{MODE: "shadow", **llm_config})
		legacy.assert_called_once()

	def test_an_unrelated_save_on_a_failed_sync_still_retries(self):
		frappe.db.set_single_value(SETTINGS, "last_sync_status", "failed: pc routing test")
		with patch.object(jarvis_settings.JarvisSettings, "_on_update_single_model_legacy") as legacy:
			_save_as_administrator(payment_calendar_chat_handoff=1)
		legacy.assert_called_once()


class TestReaderBuild(FrappeTestCase):
	"""The build id hashes the figure-deciding modules and the two versions, once per process (owner A3)."""

	def setUp(self):
		routing._build_once.cache_clear()
		self.addCleanup(routing._build_once.cache_clear)

	def _identity(self, directory, names=("a", "b"), frappe_version="1", erpnext_version="1"):
		return routing.build_identity(Path(directory), names, frappe_version, erpnext_version)

	def test_it_is_a_stable_hex_digest_of_the_real_package(self):
		first = routing.reader_build()
		self.assertEqual(first, routing.reader_build())
		self.assertRegex(first, r"^[0-9a-f]{64}$")

	def test_it_is_computed_once_per_process(self):
		with patch.object(routing, "build_identity", return_value="x" * 64) as compute:
			routing.reader_build()
			routing.reader_build()
		self.assertEqual(compute.call_count, 1)

	def test_a_file_edited_after_the_first_call_does_not_change_the_loaded_identity(self):
		with tempfile.TemporaryDirectory() as directory:
			(Path(directory) / "a.py").write_text("A = 1\n")
			(Path(directory) / "b.py").write_text("B = 1\n")
			with (
				patch.object(routing, "FIGURE_MODULES", ("a", "b")),
				patch.object(routing, "__file__", str(Path(directory) / "routing")),
			):
				first = routing.reader_build()
				(Path(directory) / "b.py").write_text("B = 2\n")
				self.assertEqual(first, routing.reader_build())

	def test_every_python_file_of_the_package_is_classified(self):
		files = {path.stem for path in Path(routing.__file__).parent.glob("*.py")}
		figure, other = set(routing.FIGURE_MODULES), set(routing.OTHER_MODULES)
		self.assertEqual(figure & other, set(), "a file is in both lists")
		self.assertEqual(
			files - figure - other,
			set(),
			"a new payment_calendar module must be classified in routing.FIGURE_MODULES or OTHER_MODULES",
		)
		self.assertEqual((figure | other) - files, set(), "a classified file does not exist")

	def test_the_figure_modules_are_the_ones_that_decide_a_figure(self):
		for name in (
			"builtin_reader",
			"eligibility",
			"sources",
			"permissions",
			"compatibility",
			"books",
			"scope",
			"errors",
		):
			self.assertIn(name, routing.FIGURE_MODULES)
		for name in ("routing", "shadow", "read_log", "snapshot"):
			self.assertNotIn(name, routing.FIGURE_MODULES)

	def test_it_changes_when_a_named_file_changes_and_not_for_an_unnamed_one(self):
		with tempfile.TemporaryDirectory() as directory:
			(Path(directory) / "a.py").write_text("A = 1\n")
			(Path(directory) / "b.py").write_text("B = 1\n")
			(Path(directory) / "other.py").write_text("O = 1\n")
			before = self._identity(directory)
			self.assertEqual(before, self._identity(directory))
			(Path(directory) / "other.py").write_text("O = 2\n")
			self.assertEqual(
				before, self._identity(directory), "an unclassified-as-figure file is not part of it"
			)
			(Path(directory) / "b.py").write_text("B = 2\n")
			self.assertNotEqual(before, self._identity(directory))

	def test_the_same_text_in_other_files_is_not_the_same_build(self):
		with tempfile.TemporaryDirectory() as directory:
			(Path(directory) / "a.py").write_text("X\n")
			(Path(directory) / "b.py").write_text("Y\n")
			before = self._identity(directory)
			(Path(directory) / "a.py").write_text("Y\n")
			(Path(directory) / "b.py").write_text("X\n")
			self.assertNotEqual(before, self._identity(directory))

	def test_it_changes_with_either_version(self):
		with tempfile.TemporaryDirectory() as directory:
			(Path(directory) / "a.py").write_text("A\n")
			(Path(directory) / "b.py").write_text("B\n")
			base = self._identity(directory)
			self.assertNotEqual(base, self._identity(directory, frappe_version="2"))
			self.assertNotEqual(base, self._identity(directory, erpnext_version="2"))


class TestViewMarker(FrappeTestCase):
	"""`snapshot.view_is_open` is the only test of an open view; what other code leaves on `frappe.db` is not."""

	def setUp(self):
		super().setUp()
		# A preview sandbox leaves `commit` and `rollback` on the connection object after a clean exit. The
		# test removes what it made, so it leaves nothing behind for the next test of the process.
		left = set(vars(frappe.db))
		self.addCleanup(lambda: [vars(frappe.db).pop(name, None) for name in set(vars(frappe.db)) - left])

	def sent_statements(self):
		return [call.args[0].strip().lower() for call in self.sql.call_args_list if call.args]

	def spy_on_sql(self):
		patcher = patch.object(frappe.db, "sql", wraps=frappe.db.sql)
		self.sql = patcher.start()
		self.addCleanup(patcher.stop)

	def test_the_marker_is_set_inside_a_view_and_not_before_or_after_it(self):
		self.assertFalse(view_is_open())
		with consistent_read():
			self.assertTrue(view_is_open())
		self.assertFalse(view_is_open())

	def test_the_marker_is_reset_when_the_body_raises(self):
		with self.assertRaises(ValueError), consistent_read():
			self.assertTrue(view_is_open())
			raise ValueError("test")
		self.assertFalse(view_is_open())

	def test_the_marker_is_reset_when_the_view_is_refused(self):
		with self.assertRaises(CalendarSourceUnavailable), consistent_read():
			frappe.db.rollback()
		self.assertFalse(view_is_open())

	def test_a_preview_sandbox_does_not_make_a_view(self):
		with preview_sandbox():
			pass
		self.assertIn(
			"rollback", vars(frappe.db), "the sandbox leaves its attributes: the cause of the defect"
		)
		self.assertFalse(view_is_open())

	def test_poison_outside_a_view_sends_no_rollback_after_a_preview_sandbox(self):
		with preview_sandbox():
			pass
		self.spy_on_sql()
		routing._poison_view()
		self.assertEqual([s for s in self.sent_statements() if s.startswith(("rollback", "start"))], [])

	def test_poison_inside_a_view_still_breaks_it(self):
		with preview_sandbox():
			pass
		with self.assertRaises(CalendarSourceUnavailable), consistent_read():
			routing._poison_view()


class TestZZNoModeLeftBehind(FrappeTestCase):
	"""Runs last in the module (alphabetical): the site must read off after the suite."""

	def test_the_mode_is_as_the_site_had_it(self):
		self.assertEqual(routing.reader_settings(), _start_settings[-1])
