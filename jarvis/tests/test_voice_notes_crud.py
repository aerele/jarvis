"""Tests for the Skills-page IA v2 voice-note CRUD extensions
(``jarvis.chat.voice_notes_api``): ``update_voice_note`` (owner-only AND
status ``New`` only, so the edited transcript re-feeds the daily sweep
untouched) and the escaped ``search`` param on ``list_my_voice_notes_page``,
plus the ``source`` contract every shipped client depends on.

Sibling of test_voice_facts.py (same fixture/idiom set: insert-as-owner,
System User fixtures, ``_as`` set_user wrap, explicit cleanup); the sweep and
the pre-existing endpoints are covered there.
"""

from __future__ import annotations

import contextlib

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import voice_notes_api
from jarvis.permissions import JARVIS_USER_ROLE, ensure_jarvis_user_role

NOTE = "Jarvis Voice Note"

USER_A = "vnc-user-a@example.com"
USER_B = "vnc-user-b@example.com"


def _ensure_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				# Explicit: a role-less insert becomes a Website User, which
				# the voice endpoints' System-User gate rejects.
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	# Unconditional post-insert repair (mirrors test_voice_facts._ensure_user):
	# User.set_system_user() recomputes user_type on validate, and a role-less
	# "System User" has no desk access, so the INSERT above persists a Website
	# User on the very first run — not just a stale fixture from an earlier run.
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
	# Jarvis access is a ROLE now (chat permission hardening, #284): the voice
	# endpoints gate on System Manager or Jarvis User, so a role-less fixture is
	# rejected before it reaches the logic under test. These users are meant to be
	# ordinary, non-admin Jarvis users — which is exactly the Jarvis User role — so
	# grant it rather than escalating them to System Manager, whose broad
	# permissions would hide the owner-scoping these tests exist to prove.
	ensure_jarvis_user_role()
	if JARVIS_USER_ROLE not in frappe.get_roles(email):
		frappe.get_doc("User", email).add_roles(JARVIS_USER_ROLE)
		frappe.clear_cache(user=email)
	frappe.db.commit()
	return email


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


class VoiceNotesCrudTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._wipe()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._wipe()
		super().tearDown()

	def _wipe(self):
		# The endpoints commit; FrappeTestCase's rollback cannot undo everything,
		# so sweep our fixture owners' rows explicitly.
		frappe.db.delete(NOTE, {"owner": ["in", [USER_A, USER_B]]})
		frappe.db.commit()

	def _note(self, owner: str, transcript: str, status: str = "New") -> str:
		# Insert AS the owner: frappe stamps owner from the session user on
		# insert, silently ignoring a passed "owner" key.
		prev = frappe.session.user
		frappe.set_user(owner)
		try:
			doc = frappe.get_doc(
				{
					"doctype": NOTE,
					"transcript": transcript,
					"context_type": "Business",
					"source": "Business Tab",
					"status": "New",
				}
			)
			doc.insert(ignore_permissions=True)
			frappe.db.commit()
		finally:
			frappe.set_user(prev)
		if status != "New":
			# db.set_value: the status transition is the sweep's job; the
			# fixture just needs the row parked in a non-New state.
			frappe.db.set_value(NOTE, doc.name, "status", status, update_modified=False)
			frappe.db.commit()
		return doc.name


# --------------------------------------------------------------------------- #
# update_voice_note
# --------------------------------------------------------------------------- #
class TestUpdateVoiceNote(VoiceNotesCrudTestCase):
	def test_owner_edits_new_note(self):
		name = self._note(USER_A, "We ship Acme from Bombay.")
		with _as(USER_A):
			out = voice_notes_api.update_voice_note(name, "  We ship Acme from Mumbai.  ")
		self.assertEqual(out, {"ok": True})
		row = frappe.db.get_value(NOTE, name, ["transcript", "status"], as_dict=True)
		self.assertEqual(row.transcript, "We ship Acme from Mumbai.")  # stripped
		# still New: the edited text re-feeds the daily sweep untouched.
		self.assertEqual(row.status, "New")

	def test_update_requires_transcript(self):
		name = self._note(USER_A, "original body")
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			voice_notes_api.update_voice_note(name, "   ")
		self.assertEqual(frappe.db.get_value(NOTE, name, "transcript"), "original body")

	def test_update_is_owner_only(self):
		name = self._note(USER_A, "mine, not yours")
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			voice_notes_api.update_voice_note(name, "hijacked")
		self.assertEqual(frappe.db.get_value(NOTE, name, "transcript"), "mine, not yours")

	def test_update_only_while_new(self):
		for parked in ("Processed", "Archived"):
			name = self._note(USER_A, f"{parked} history", status=parked)
			with _as(USER_A), self.assertRaises(frappe.ValidationError):
				voice_notes_api.update_voice_note(name, "rewritten history")
			self.assertEqual(frappe.db.get_value(NOTE, name, "transcript"), f"{parked} history")

	def test_update_unknown_note(self):
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			voice_notes_api.update_voice_note("JVN-does-not-exist", "body")

	def test_update_enforces_transcript_cap(self):
		# The save runs through the doctype controller, so save_voice_note's
		# MAX_TRANSCRIPT_LEN cap applies to edits too.
		from jarvis.jarvis.doctype.jarvis_voice_note.jarvis_voice_note import (
			MAX_TRANSCRIPT_LEN,
		)

		name = self._note(USER_A, "short")
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			voice_notes_api.update_voice_note(name, "x" * (MAX_TRANSCRIPT_LEN + 1))
		self.assertEqual(frappe.db.get_value(NOTE, name, "transcript"), "short")

	def test_guest_is_rejected(self):
		name = self._note(USER_A, "keep out")
		with _as("Guest"), self.assertRaises(frappe.PermissionError):
			voice_notes_api.update_voice_note(name, "guest edit")


# --------------------------------------------------------------------------- #
# list_my_voice_notes_page search param
# --------------------------------------------------------------------------- #
class TestVoiceNotesSearch(VoiceNotesCrudTestCase):
	def test_search_filters_transcript(self):
		self._note(USER_A, "Acme deliveries always ship from Mumbai.")
		self._note(USER_A, "Quotations are valid fifteen days.")
		with _as(USER_A):
			page = voice_notes_api.list_my_voice_notes_page(search="mumbai")
		self.assertEqual(page["total"], 1)
		self.assertEqual(len(page["rows"]), 1)
		self.assertIn("Mumbai", page["rows"][0]["transcript"])
		self.assertFalse(page["has_more"])
		# the envelope shape is unchanged by the new param
		for key in ("rows", "total", "has_more", "start", "page_length"):
			self.assertIn(key, page)

	def test_blank_search_returns_everything(self):
		self._note(USER_A, "one")
		self._note(USER_A, "two")
		with _as(USER_A):
			page = voice_notes_api.list_my_voice_notes_page(search="   ")
		self.assertEqual(page["total"], 2)

	def test_search_stays_owner_scoped(self):
		self._note(USER_A, "Mumbai note owned by A")
		with _as(USER_B):
			page = voice_notes_api.list_my_voice_notes_page(search="Mumbai")
		self.assertEqual(page["total"], 0)

	def test_search_escapes_like_wildcards(self):
		self._note(USER_A, "staff discount is 100% on samples")
		self._note(USER_A, "no discount whatsoever")
		self._note(USER_A, "token with under_score inside")
		with _as(USER_A):
			# '%' is a literal, not match-anything
			pct = voice_notes_api.list_my_voice_notes_page(search="100%")
			self.assertEqual(pct["total"], 1)
			self.assertIn("100%", pct["rows"][0]["transcript"])
			# '_' is a literal, not match-any-one-char (unescaped it would
			# match every non-empty transcript)
			us = voice_notes_api.list_my_voice_notes_page(search="_")
			self.assertEqual(us["total"], 1)
			self.assertIn("under_score", us["rows"][0]["transcript"])
			# backslash is a literal too, never a dangling escape
			none = voice_notes_api.list_my_voice_notes_page(search="\\")
			self.assertEqual(none["total"], 0)

	def test_search_combines_with_status_filter(self):
		self._note(USER_A, "Mumbai backlog note")
		self._note(USER_A, "Mumbai processed note", status="Processed")
		with _as(USER_A):
			page = voice_notes_api.list_my_voice_notes_page(search="Mumbai", status="New")
		self.assertEqual(page["total"], 1)
		self.assertEqual(page["rows"][0]["status"], "New")

	def test_search_truncated_to_140_chars(self):
		# The 141st-and-beyond chars are dropped, not an error: a note matching
		# only the first 140 chars of an overlong query still hits.
		self._note(USER_A, "x" * 140)
		with _as(USER_A):
			page = voice_notes_api.list_my_voice_notes_page(search="x" * 140 + "z")
		self.assertEqual(page["total"], 1)


# --------------------------------------------------------------------------- #
# save_voice_note `source` contract (JF-020)
# --------------------------------------------------------------------------- #
# Every capture surface that ships a client sending an explicit `source`. Each
# entry names the client file that sends it, so deleting a value from
# voice_notes_api._SOURCES fails here with the surface it would break instead of
# silently 417-ing that client in the field:
#
#   Business Tab  frontend/src/api/voice.js         (web SPA, Personalise tab)
#   Chat Nudge    frontend/src/views/ChatView.vue   (post-turn "remember this?")
#   Mobile        jarvis_mobile src/api/endpoints.ts saveVoiceNote
#                 (repo Aerele-RnD/jarvis_mobile)
#
# The PWA (pwa/src/api.js) deliberately sends NO source and rides the server
# default, so it is not listed.
CLIENT_SOURCES = {
	"Business Tab": "frontend/src/api/voice.js",
	"Chat Nudge": "frontend/src/views/ChatView.vue",
	"Mobile": "jarvis_mobile src/api/endpoints.ts (saveVoiceNote)",
}


class TestVoiceNoteSourceContract(VoiceNotesCrudTestCase):
	def test_every_shipped_client_source_is_accepted(self):
		for source, client in CLIENT_SOURCES.items():
			self.assertIn(
				source,
				voice_notes_api._SOURCES,
				msg=f"{source!r} was dropped from voice_notes_api._SOURCES - {client} now 417s",
			)

	def test_allowlist_is_a_subset_of_the_doctype_options(self):
		# frappe rejects an off-list Select value at insert, so an allowlist entry
		# missing from the doctype would pass validation here and then explode on
		# doc.insert() for the caller.
		options = frappe.get_meta(NOTE).get_field("source").options.split("\n")
		for source in voice_notes_api._SOURCES:
			self.assertIn(source, options)

	def test_each_allowed_source_round_trips(self):
		# End-to-end through the endpoint (not just the constant): proves frappe's
		# Select validation accepts the value on a real insert.
		with _as(USER_A):
			for source in voice_notes_api._SOURCES:
				out = voice_notes_api.save_voice_note(
					transcript=f"note captured from {source}",
					context_type="Business",
					source=source,
				)
				self.assertEqual(frappe.db.get_value(NOTE, out["name"], "source"), source)

	def test_unknown_source_is_still_rejected(self):
		# Fail closed: the allowlist grew, it did not become a passthrough.
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			voice_notes_api.save_voice_note(transcript="from nowhere", source="Telepathy")
		self.assertEqual(frappe.db.count(NOTE, {"owner": USER_A}), 0)

	def test_mobile_capture_is_org_scoped_not_private(self):
		# Scope invariant: a phone capture is the Business tab on a phone, so its
		# facts belong on the shared Org wiki exactly like a desktop capture -
		# only "Personalise"/question-linked notes are private to their owner.
		from jarvis.learning import voice_facts

		for source in voice_notes_api._SOURCES:
			self.assertFalse(voice_facts._is_personalise_note(source, None))
		self.assertTrue(voice_facts._is_personalise_note("Personalise", None))
		self.assertTrue(voice_facts._is_personalise_note("Mobile", "JPQ-0001"))
