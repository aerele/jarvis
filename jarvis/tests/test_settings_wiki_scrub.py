"""Tests for the "Enable Business Wiki" ON -> OFF scrub trigger (#731).

Disabling the wiki must enqueue a one-shot scrub that removes the tenant's
already-mirrored page files from the container. The trigger lives on the Jarvis
Settings controller: validate() captures the RAW pre-save toggle state (so the
NULL=ON idiom is honoured, which has_value_changed cannot), and on_update() fires
``wiki_mirror.enqueue_scrub`` only on a real 1 -> 0 flip.

``Jarvis Settings`` is a Single (one row shared by the whole DB), and
``wiki_enabled`` may be absent from tabSingles entirely (NULL = ON). setUp
snapshots the RAW value - including "row absent" - and tearDown restores it so the
suite leaves the shared toggle exactly as it found it.

The two LLM dispatch methods are patched out in the save-path tests: saving Jarvis
Settings runs the LLM sync INLINE under ``frappe.flags.in_test``, and this trigger
is orthogonal to it.
"""

from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import wiki_mirror

SETTINGS = "Jarvis Settings"


def _raw_wiki_enabled():
	"""The stored ``wiki_enabled`` exactly as ``wiki_enabled()`` reads it: the
	tabSingles value, or None when the row is absent (NULL = ON)."""
	rows = frappe.db.sql(
		"select value from `tabSingles` where doctype=%s and field=%s",
		(SETTINGS, "wiki_enabled"),
	)
	return rows[0][0] if rows else None


def _set_raw_wiki_enabled(value):
	"""Force the stored toggle to ``value`` (0/1) or, for None, remove the row so
	the NULL=ON default applies. Commits so a subsequent get_single reads it."""
	frappe.db.sql(
		"delete from `tabSingles` where doctype=%s and field=%s",
		(SETTINGS, "wiki_enabled"),
	)
	if value is not None:
		frappe.db.set_single_value(SETTINGS, "wiki_enabled", value, update_modified=False)
	frappe.db.commit()


class TestWikiScrubGuardUnit(FrappeTestCase):
	"""The guard method in isolation: the 1 -> 0 truth table, no save machinery."""

	def _guard(self, was_enabled, new_value):
		settings = frappe.get_single(SETTINGS)
		if was_enabled is None:
			settings.flags.pop("wiki_was_enabled", None)
		else:
			settings.flags.wiki_was_enabled = was_enabled
		settings.wiki_enabled = new_value
		with mock.patch.object(wiki_mirror, "enqueue_scrub") as enq:
			settings._maybe_scrub_wiki_on_disable()
		return enq

	def test_on_to_off_scrubs(self):
		self.assertTrue(self._guard(was_enabled=True, new_value=0).called)

	def test_off_to_off_does_nothing(self):
		self.assertFalse(self._guard(was_enabled=False, new_value=0).called)

	def test_still_on_does_nothing(self):
		self.assertFalse(self._guard(was_enabled=True, new_value=1).called)

	def test_untouched_field_while_on_does_nothing(self):
		# NULL=ON on the new value too: an unset field still reads as enabled.
		self.assertFalse(self._guard(was_enabled=True, new_value=None).called)

	def test_missing_capture_flag_does_nothing(self):
		# validate() skipped -> no captured state -> err toward not scrubbing.
		self.assertFalse(self._guard(was_enabled=None, new_value=0).called)


class TestWikiScrubGuardOnSave(FrappeTestCase):
	"""The full save lifecycle: validate() captures the raw pre-save state and
	on_update() fires. Locks in the NULL(=ON) -> 0 case that motivates the raw
	read - the coerced before-doc would report 0 -> 0 and miss it."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._wiki_enabled_before = _raw_wiki_enabled()

	def tearDown(self):
		_set_raw_wiki_enabled(self._wiki_enabled_before)
		super().tearDown()

	def _save_toggling_to(self, new_value):
		"""Save Jarvis Settings with wiki_enabled set to ``new_value``, LLM
		dispatch stubbed out, and enqueue_scrub captured. Returns the mock."""
		settings = frappe.get_single(SETTINGS)
		settings.wiki_enabled = new_value
		with (
			mock.patch.object(type(settings), "_on_update_unified_llm"),
			mock.patch.object(type(settings), "_on_update_single_model_legacy"),
			mock.patch.object(wiki_mirror, "enqueue_scrub") as enq,
		):
			settings.save(ignore_permissions=True)
		return enq

	def test_a_real_save_from_on_to_off_scrubs(self):
		_set_raw_wiki_enabled(1)
		self.assertTrue(self._save_toggling_to(0).called)

	def test_a_real_save_from_null_on_to_off_scrubs(self):
		# Row absent => NULL => effectively ON. This is the case has_value_changed
		# misses (the Check coerces NULL to 0, so it sees 0 -> 0) and the whole
		# reason validate() re-reads the raw tabSingles value.
		_set_raw_wiki_enabled(None)
		self.assertTrue(self._save_toggling_to(0).called)

	def test_a_real_save_while_already_off_does_nothing(self):
		_set_raw_wiki_enabled(0)
		self.assertFalse(self._save_toggling_to(0).called)

	def test_a_real_re_enable_does_nothing(self):
		_set_raw_wiki_enabled(0)
		self.assertFalse(self._save_toggling_to(1).called)
