"""Tests for jarvis.chat.cards_enrich - the deterministic backend floor that fills an
id-only ``jarvis-cards`` block with the record's own fields (B).

The logic tests patch ``_record_summary.summary_rows`` so they exercise the parsing,
the only-fill-empty rule, idempotency, and the fence handling without needing seeded
records - the permission-checked read itself is covered by test_record_summary.py. A
few wiring tests cover the owner impersonation, the finalize effect's errored-skip, and
the write-back.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis.chat import cards_enrich

_SUMMARY = "jarvis.chat._record_summary.summary_rows"


def _block(cards: list[dict], title: str = "Records") -> str:
	"""A ```jarvis-cards fence carrying ``cards``, as the agent would emit it."""
	payload = json.dumps({"title": title, "cards": cards})
	return f"Here you go:\n\n```jarvis-cards\n{payload}\n```"


def _first_card(content: str) -> dict:
	"""Parse the first jarvis-cards block out of ``content`` and return card[0]."""
	m = cards_enrich._CARDS_RE.search(content)
	return json.loads(m.group(1))["cards"][0]


class TestEnrichCard(FrappeTestCase):
	def test_id_only_card_is_filled_and_title_promoted(self):
		rows = [{"label": "Department", "value": "Engineering"}, {"label": "Status", "value": "Active"}]
		content = _block([{"doctype": "Employee", "name": "HR-EMP-0001", "title": "HR-EMP-0001"}])
		with patch(_SUMMARY, return_value={"title": "Priya Nair", "rows": rows}) as sr:
			out = cards_enrich.enrich_text(content)
		sr.assert_called_once_with("Employee", "HR-EMP-0001")
		card = _first_card(out)
		self.assertEqual(card["fields"], rows)
		# the title was the id, so it is promoted to the record's human name
		self.assertEqual(card["title"], "Priya Nair")

	def test_card_with_fields_is_left_untouched(self):
		# The agent's own dynamic choice (skill A) wins: never override a populated card.
		agent_fields = [{"label": "Sales", "value": "1.20L"}]
		content = _block(
			[{"doctype": "Customer", "name": "Acme", "title": "Acme Ltd", "fields": agent_fields}]
		)
		with patch(_SUMMARY) as sr:
			out = cards_enrich.enrich_text(content)
		sr.assert_not_called()
		self.assertEqual(out, content)

	def test_card_without_doctype_or_name_is_skipped(self):
		content = _block([{"title": "Mystery"}])  # nothing to look the record up by
		with patch(_SUMMARY) as sr:
			out = cards_enrich.enrich_text(content)
		sr.assert_not_called()
		self.assertEqual(out, content)

	def test_unreadable_or_missing_record_is_left_as_id(self):
		# summary_rows returns None when the record is gone or the user cannot read it.
		# Never fabricate - the card stays id-only rather than inventing values.
		content = _block([{"doctype": "Employee", "name": "HR-EMP-9999", "title": "HR-EMP-9999"}])
		with patch(_SUMMARY, return_value=None):
			out = cards_enrich.enrich_text(content)
		self.assertEqual(out, content)
		self.assertNotIn("fields", _first_card(out))

	def test_summary_with_no_rows_is_left(self):
		content = _block([{"doctype": "Employee", "name": "HR-EMP-1", "title": "HR-EMP-1"}])
		with patch(_SUMMARY, return_value={"title": "x", "rows": []}):
			out = cards_enrich.enrich_text(content)
		self.assertEqual(out, content)

	def test_title_kept_when_agent_set_a_real_one(self):
		content = _block([{"doctype": "Employee", "name": "HR-EMP-1", "title": "Priya Nair"}])
		with patch(
			_SUMMARY, return_value={"title": "Something Else", "rows": [{"label": "S", "value": "A"}]}
		):
			out = cards_enrich.enrich_text(content)
		self.assertEqual(_first_card(out)["title"], "Priya Nair")  # not overwritten


class TestEnrichText(FrappeTestCase):
	def test_no_cards_block_returns_verbatim(self):
		content = "Just a plain reply with no card fence."
		with patch(_SUMMARY) as sr:
			self.assertEqual(cards_enrich.enrich_text(content), content)
		sr.assert_not_called()

	def test_malformed_block_is_left_verbatim(self):
		content = "```jarvis-cards\n{ this is not json\n```"
		with patch(_SUMMARY) as sr:
			out = cards_enrich.enrich_text(content)
		sr.assert_not_called()
		self.assertEqual(out, content)

	def test_multiple_blocks_all_enriched(self):
		rows = [{"label": "Dept", "value": "Eng"}]
		content = (
			_block([{"doctype": "Employee", "name": "E1", "title": "E1"}])
			+ "\n"
			+ _block([{"doctype": "Employee", "name": "E2", "title": "E2"}])
		)
		with patch(_SUMMARY, return_value={"title": "n", "rows": rows}):
			out = cards_enrich.enrich_text(content)
		blocks = cards_enrich._CARDS_RE.findall(out)
		self.assertEqual(len(blocks), 2)
		for raw in blocks:
			self.assertEqual(json.loads(raw)["cards"][0]["fields"], rows)

	def test_other_fences_are_untouched(self):
		ask = '```jarvis-ask\n[{"q":"Which?","type":"yesno"}]\n```'
		content = ask + "\n" + _block([{"doctype": "Employee", "name": "E1", "title": "E1"}])
		with patch(_SUMMARY, return_value={"title": "n", "rows": [{"label": "D", "value": "E"}]}):
			out = cards_enrich.enrich_text(content)
		self.assertIn(ask, out)  # the ask fence survives byte-for-byte

	def test_idempotent(self):
		content = _block([{"doctype": "Employee", "name": "E1", "title": "E1"}])
		with patch(_SUMMARY, return_value={"title": "Nm", "rows": [{"label": "D", "value": "E"}]}) as sr:
			once = cards_enrich.enrich_text(content)
			twice = cards_enrich.enrich_text(once)
		self.assertEqual(once, twice)  # second pass is a no-op
		sr.assert_called_once()  # the now-filled card is not re-read

	def test_exception_returns_original(self):
		content = _block([{"doctype": "Employee", "name": "E1", "title": "E1"}])
		with patch(_SUMMARY, side_effect=RuntimeError("boom")):
			out = cards_enrich.enrich_text(content)
		self.assertEqual(out, content)  # degrades to the original, never raises

	def test_reads_are_bounded_by_max_cards(self):
		cards = [
			{"doctype": "Employee", "name": f"E{i}", "title": f"E{i}"}
			for i in range(cards_enrich._MAX_CARDS + 5)
		]
		with patch(_SUMMARY, return_value={"title": "n", "rows": [{"label": "D", "value": "E"}]}) as sr:
			cards_enrich.enrich_text(_block(cards))
		self.assertLessEqual(sr.call_count, cards_enrich._MAX_CARDS)

	def test_owner_is_impersonated_for_the_read(self):
		content = _block([{"doctype": "Employee", "name": "E1", "title": "E1"}])
		with (
			patch("jarvis.chat.cards_enrich.impersonate") as imp,
			patch(_SUMMARY, return_value={"title": "n", "rows": [{"label": "D", "value": "E"}]}),
			patch("jarvis.chat.cards_enrich.frappe.session") as sess,
		):
			sess.user = "administrator@example.com"
			cards_enrich.enrich_text(content, owner="tenant@example.com")
		imp.assert_called_once_with("tenant@example.com")


class TestEnrichMessageAndEffect(FrappeTestCase):
	def test_enrich_message_writes_back_only_when_changed(self):
		row = {"content": _block([{"doctype": "Employee", "name": "E1", "title": "E1"}]), "owner": "u@x"}
		enriched = "ENRICHED"
		with (
			patch("jarvis.chat.cards_enrich.frappe.db.get_value", return_value=row),
			patch("jarvis.chat.cards_enrich.enrich_text", return_value=enriched),
			patch("jarvis.chat.cards_enrich.frappe.db.set_value") as sv,
		):
			cards_enrich.enrich_message("MSG-1")
		sv.assert_called_once_with(cards_enrich.MSG, "MSG-1", "content", enriched)

	def test_enrich_message_no_write_when_unchanged(self):
		row = {"content": "same", "owner": "u@x"}
		with (
			patch("jarvis.chat.cards_enrich.frappe.db.get_value", return_value=row),
			patch("jarvis.chat.cards_enrich.enrich_text", return_value="same"),
			patch("jarvis.chat.cards_enrich.frappe.db.set_value") as sv,
		):
			cards_enrich.enrich_message("MSG-1")
		sv.assert_not_called()

	def test_finalize_effect_skips_errored_turn(self):
		from jarvis.chat import finalize

		ctx = MagicMock(errored=True, turn={"assistant_message": "MSG-1"}, owner="u@x")
		with patch("jarvis.chat.cards_enrich.enrich_message") as em:
			finalize._effect_enrich_cards(ctx)
		em.assert_not_called()

	def test_finalize_effect_enriches_success_turn(self):
		from jarvis.chat import finalize

		ctx = MagicMock(errored=False, turn={"assistant_message": "MSG-1"}, owner="u@x")
		with patch("jarvis.chat.cards_enrich.enrich_message") as em:
			finalize._effect_enrich_cards(ctx)
		em.assert_called_once_with("MSG-1", owner="u@x")
