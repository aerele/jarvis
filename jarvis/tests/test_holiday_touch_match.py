"""Matching a Holiday row description to a design. Pure, no database."""

import unittest
from datetime import date

from jarvis.holiday_touch.match import match_occasion, normalise
from jarvis.holiday_touch.names import ALIASES, DESIGNS, NEAR_MISSES, OCCASIONS

ANY = date(2026, 6, 10)  # a day no date guard accepts
DIWALI_DAY = date(2026, 11, 8)


def m(text, on=ANY, country="India", extra=None):
	return match_occasion(text, on, country, extra)


class TestNormalise(unittest.TestCase):
	def test_lowercases_and_splits_on_non_alphanumerics(self):
		self.assertEqual(normalise("Id-ul-Fitr (estimated)"), ["id", "ul", "fitr", "estimated"])
		self.assertEqual(normalise("New Year's Day"), ["new", "year", "s", "day"])

	def test_strips_html_and_unescapes_entities(self):
		self.assertEqual(
			normalise("<p>Diwali&nbsp;<b>(Deepavali)</b> &amp; Co</p>"), ["diwali", "deepavali", "co"]
		)
		self.assertEqual(normalise("Holi&#39;s"), ["holi", "s"])

	def test_zero_width_and_soft_hyphen_characters_are_dropped_not_read_as_spaces(self):
		# Format characters (Unicode category Cf) are invisible: "Diwa<ZWSP>li" is "Diwali".
		for text in ("Diwa\u200bli", "Di\u00adwali", "\ufeffDiwali", "Di\u200dwa\u2060li", "Diwali\u200f"):
			with self.subTest(text=text):
				self.assertEqual(normalise(text), ["diwali"])
				self.assertEqual(m(text), "diwali")
		self.assertEqual(normalise("Diwali&#8203;Sunday"), ["diwalisunday"])  # an entity is no different

	def test_compatibility_forms_are_folded_but_accents_are_not(self):
		# NFKC: full-width letters and the like become their plain forms. Accents are NOT
		# stripped ("d\u012bwali" stays its own word); a one-letter difference in a long
		# enough name is the matcher's ordinary typo tolerance, not accent folding.
		self.assertEqual(normalise("\uff24\uff49\uff57\uff41\uff4c\uff49"), ["diwali"])
		self.assertEqual(m("\uff24\uff49\uff57\uff41\uff4c\uff49"), "diwali")
		self.assertEqual(normalise("d\u012bwali"), ["d\u012bwali"])
		self.assertEqual(normalise("Cafe\u0301"), ["caf\u00e9"])  # composed, still one word

	def test_blank_and_none(self):
		self.assertEqual(normalise(None), [])
		self.assertEqual(normalise("  -- "), [])

	def test_capped_at_300_characters(self):
		tokens = normalise("a" * 200 + " " + "b" * 99 + " " + "c" * 50)
		self.assertEqual(len(" ".join(tokens)), 300)

	def test_cap_drops_a_word_it_cut_in_half(self):
		tokens = normalise("a" * 200 + " " + "b" * 200)
		self.assertEqual(tokens, ["a" * 200])

	def test_cap_cannot_create_a_false_match(self):
		# "x"*295 + " holiday" is cut to "...xxxxx holi", which must not read as "holi".
		self.assertEqual(normalise("x" * 295 + " holiday"), ["x" * 295])
		self.assertIsNone(m("x" * 295 + " holiday"))
		# A word that fits whole is still kept.
		self.assertEqual(m("x" * 294 + " holi"), "holi")


class TestNormaliseIsLinear(unittest.TestCase):
	def _timed(self, text):
		import time

		start = time.perf_counter()
		tokens = normalise(text)
		return tokens, time.perf_counter() - start

	def test_many_open_angle_brackets(self):
		tokens, took = self._timed("<" * 100_000)
		self.assertLess(took, 0.5)
		self.assertEqual(tokens, [])

	def test_mixed_junk(self):
		tokens, took = self._timed("<a " * 50_000 + "<" * 50_000)
		self.assertLess(took, 0.5)
		self.assertTrue(all(t.isalnum() for t in tokens))
		self.assertLessEqual(len(" ".join(tokens)), 300)

	def test_oversized_html_still_matches_its_start(self):
		self.assertEqual(m("<p>Diwali</p>" + "<" * 100_000), "diwali")


class TestTables(unittest.TestCase):
	def test_every_occasion_maps_to_a_design_and_has_aliases(self):
		self.assertEqual(len(DESIGNS), 9)
		for occasion, design in OCCASIONS.items():
			self.assertIn(design, DESIGNS)
			self.assertTrue(ALIASES[occasion])
		self.assertEqual(OCCASIONS["republic"], "independence")

	def test_seed_phrases_are_already_normalised(self):
		for phrase in NEAR_MISSES + tuple(a for names in ALIASES.values() for a in names):
			self.assertEqual(" ".join(normalise(phrase)), phrase)


class TestPythonHolidaysStrings(unittest.TestCase):
	def test_strings_that_must_match(self):
		cases = [
			("Diwali (Deepavali)", "diwali"),
			("Id-ul-Fitr (estimated)", "eid"),
			("Id-ul-Zuha (Bakrid)", "eid"),
			("Holi", "holi"),
			("Pongal", "pongal"),
			("Thiruvalluvar Day / Mattu Pongal", "pongal"),
			("Milad-un-Nabi; Onam", "onam"),
			("Deepavali (Naraka Chaturdashi)", "diwali"),
			("Diwali (Bali Pratipada)", "diwali"),
			("Onam / Thiruvonam", "onam"),
			("Holi (Dhulandi)", "holi"),
		]
		for text, want in cases:
			with self.subTest(text=text):
				self.assertEqual(m(text), want)

	def test_dated_names(self):
		self.assertEqual(m("New Year's Day", date(2026, 1, 1)), "newyear")
		self.assertEqual(m("Christmas", date(2026, 12, 25)), "christmas")
		self.assertEqual(m("Republic Day", date(2026, 1, 26)), "republic")
		self.assertEqual(m("Independence Day", date(2026, 8, 15)), "independence")
		self.assertEqual(m("Independence Day / Parsi New Year", date(2026, 8, 15)), "independence")

	def test_strings_that_must_give_none(self):
		cases = [
			"Meshadi (Tamil New Year's Day)",
			"Christmas Eve",
			"Holika Dahan",
			"Parsi New Year",
			"Milad-un-Nabi",
			"Eid-e-Milad",
			"Day after Diwali",
			"Choti Diwali",
			"Holiday",
			"Holy",
			"Public Holiday",
			"Sunday",
			"",
			None,
		]
		for text in cases:
			with self.subTest(text=text):
				self.assertIsNone(m(text, date(2026, 1, 1)))
				self.assertIsNone(m(text))


class TestWholeWordAndTypos(unittest.TestCase):
	def test_holiday_is_not_holi(self):
		self.assertIsNone(m("Holiday"))
		self.assertIsNone(m("Winter Holiday"))
		self.assertEqual(m("Holi Holiday"), "holi")

	def test_typos_within_the_bound_match(self):
		for text, want in [
			("Deepavalli", "diwali"),
			("Chrismas", "christmas"),
			("Pongol", "pongal"),
			("Thiruvonum", "onam"),
		]:
			with self.subTest(text=text):
				self.assertEqual(m(text, date(2026, 12, 25)), want)

	def test_short_names_are_exact_only(self):
		self.assertIsNone(m("Hola"))
		self.assertIsNone(m("Onum"))
		self.assertIsNone(m("Eed"))

	def test_edit_bound_scales_with_length(self):
		self.assertIsNone(m("Pongxx"))  # 6 letters: 2 edits is too many
		self.assertEqual(m("Pongxl"), "pongal")  # 1 edit
		self.assertEqual(m("Deepaxxli"), "diwali")  # 9 letters, 2 edits
		self.assertIsNone(m("Deepaxxxli"))  # 3 edits

	def test_glued_and_split_spellings(self):
		self.assertEqual(m("Newyear", date(2026, 1, 1)), "newyear")
		self.assertEqual(m("X-mas", date(2026, 12, 25)), "christmas")
		self.assertEqual(m("Xmas", date(2026, 12, 25)), "christmas")


class TestNearMissMask(unittest.TestCase):
	def test_mask_removes_the_span_but_keeps_the_rest(self):
		self.assertIsNone(m("Christmas Eve", date(2026, 12, 25)))
		self.assertEqual(m("Christmas Eve / Christmas", date(2026, 12, 25)), "christmas")
		self.assertEqual(m("Parsi New Year / Pongal"), "pongal")

	def test_longest_near_miss_wins_over_a_shorter_prefix_or_suffix(self):
		# "eid e milad" must go whole; masking only "milad" would leave "eid".
		self.assertIsNone(m("Eid-e-Milad"))
		self.assertEqual(m("Eid-e-Milad / Eid ul Fitr"), "eid")

	def test_milad_variants_are_near_misses(self):
		for text in (
			"Eid Milad",
			"Eid ul Milad",
			"Eid Milad un Nabi",
			"Eid-ul-Milad",
			"Eid Milad-un-Nabi",
			"Eid-e-Milad",
			"Milad-un-Nabi",
		):
			with self.subTest(text=text):
				self.assertIsNone(m(text))
		self.assertEqual(m("Eid Milad un Nabi / Eid ul Fitr"), "eid")

	def test_masked_span_does_not_glue_neighbours(self):
		# "new" + "year" must not be rebuilt from the two sides of a removed phrase.
		self.assertIsNone(m("New Holika Dahan Year", date(2026, 1, 1)))


class TestDateGuards(unittest.TestCase):
	def test_christmas_only_on_25_dec(self):
		self.assertIsNone(m("Christmas", date(2026, 12, 24)))
		self.assertEqual(m("Christmas", date(2026, 12, 25)), "christmas")

	def test_new_year_only_on_1_jan(self):
		self.assertIsNone(m("New Year's Day", date(2026, 4, 14)))
		self.assertEqual(m("New Year", date(2027, 1, 1)), "newyear")

	def test_independence_needs_15_aug_and_india(self):
		self.assertIsNone(m("Independence Day", date(2026, 7, 4), "United States"))
		self.assertIsNone(m("Independence Day", date(2026, 8, 15), "United States"))
		self.assertIsNone(m("Independence Day", date(2026, 7, 4), "India"))
		self.assertEqual(m("Independence Day", date(2026, 8, 15), "india"), "independence")
		self.assertEqual(m("Independence Day", date(2026, 8, 15), " India "), "independence")

	def test_republic_needs_26_jan_and_india(self):
		self.assertIsNone(m("Republic Day", date(2026, 1, 26), "United States"))
		self.assertIsNone(m("Republic Day", date(2026, 1, 26), None))
		self.assertIsNone(m("Republic Day", date(2026, 1, 27), "India"))
		self.assertEqual(m("Republic Day", date(2026, 1, 26), "India"), "republic")

	def test_unguarded_occasions_match_any_date_and_country(self):
		self.assertEqual(m("Diwali", DIWALI_DAY, None), "diwali")
		self.assertEqual(m("Diwali", date(2026, 3, 3), "Germany"), "diwali")


class TestTies(unittest.TestCase):
	def test_two_occasions_in_one_row_give_none(self):
		self.assertIsNone(m("Diwali / Pongal"))
		self.assertIsNone(m("Holi and Onam"))

	def test_a_failed_guard_removes_the_rival(self):
		# Christmas is guarded off on 1 Jan, so the new-year half stands alone.
		self.assertEqual(m("Christmas / New Year", date(2026, 1, 1)), "newyear")
		self.assertEqual(m("Christmas / New Year", date(2026, 12, 25)), "christmas")
		self.assertIsNone(m("Christmas / Diwali", date(2026, 12, 25)))

	def test_aliases_of_one_occasion_are_not_a_tie(self):
		self.assertEqual(m("Onam / Thiruvonam"), "onam")
		self.assertEqual(m("Deepavali / Diwali / Divali"), "diwali")


class TestExtra(unittest.TestCase):
	def test_extra_alias_matches(self):
		self.assertIsNone(m("Tihar"))
		self.assertEqual(m("Tihar", extra={"aliases": {"diwali": ["Tihar"]}}), "diwali")

	def test_extra_alias_is_normalised(self):
		extra = {"aliases": {"pongal": ["  Uzhavar  THIRUNAAL "]}}
		self.assertEqual(m("Uzhavar Thirunaal", extra=extra), "pongal")

	def test_extra_alias_for_an_unknown_occasion_is_ignored(self):
		self.assertIsNone(m("Tihar", extra={"aliases": {"kwanzaa": ["Tihar"]}}))
		self.assertIsNone(m("Tihar", extra={"aliases": {"birthday": ["Tihar"]}}))

	def test_extra_alias_still_obeys_guards_and_ties(self):
		extra = {"aliases": {"christmas": ["Noel"]}}
		self.assertIsNone(m("Noel", date(2026, 12, 24), extra=extra))
		self.assertEqual(m("Noel", date(2026, 12, 25), extra=extra), "christmas")
		self.assertIsNone(m("Noel Diwali", date(2026, 12, 25), extra=extra))

	def test_extra_near_miss_masks(self):
		self.assertIsNone(m("Pongal Holiday Special Diwali Week"))
		extra = {"near_misses": ["Diwali Week"]}
		self.assertEqual(m("Pongal Diwali Week", extra=extra), "pongal")
		self.assertIsNone(m("Pongal Diwali Week"))

	def test_extra_does_not_leak_into_later_calls(self):
		m("Tihar", extra={"aliases": {"diwali": ["Tihar"]}})
		self.assertIsNone(m("Tihar"))

	def test_malformed_extra_is_tolerated(self):
		self.assertEqual(m("Holi", extra={}), "holi")
		self.assertEqual(m("Holi", extra={"aliases": None, "near_misses": None}), "holi")
		self.assertEqual(m("Holi", extra={"aliases": {"diwali": None, "holi": [None, "", 5]}}), "holi")


class TestShortAliasesNeedWholeTokens(unittest.TestCase):
	def test_alias_inside_a_longer_token_does_not_match(self):
		for text in ("Eidgah", "Onama", "Eidd", "Holidays"):
			with self.subTest(text=text):
				self.assertIsNone(m(text))


class TestRichText(unittest.TestCase):
	def test_html_description(self):
		self.assertEqual(
			m("<div class='ql-editor'><p>Happy <b>Diwali</b>&nbsp;(Deepavali)</p></div>"), "diwali"
		)

	def test_a_stray_less_than_does_not_swallow_the_text_up_to_the_next_tag(self):
		# "<3 Diwali <b>" must not be read as one tag from the first "<" to the ">".
		self.assertEqual(m("<3 Diwali <b>x</b>"), "diwali")
		self.assertEqual(normalise("<3 Diwali <b>x</b>"), ["3", "diwali", "x"])

	def test_tag_attribute_text_is_not_matched(self):
		self.assertIsNone(m('<span title="Diwali">Sunday</span>'))

	def test_entity_encoded_markup_is_text_not_tags(self):
		self.assertEqual(m("Pongal &amp; Sankranti"), "pongal")

	def test_date_accepts_a_datetime(self):
		from datetime import datetime

		self.assertEqual(m("Christmas", datetime(2026, 12, 25, 8, 0)), "christmas")


class TestRawCutDoesNotOpenATag(unittest.TestCase):
	def test_a_tag_cut_in_half_by_the_raw_limit_is_dropped(self):
		# 4982 chars of balanced tags, then a tag whose ">" lies past char 5000: the cut would
		# leave "<span class=diwali" and the tag regex needs a ">" to strip it.
		text = "<b></b>" * 711 + "x" * 5 + "<span class=diwali>Sunday</span>"
		self.assertEqual(len("<b></b>" * 711 + "x" * 5), 4982)
		self.assertIsNone(m(text))

	def test_the_same_tag_in_a_short_text_is_still_stripped_whole(self):
		self.assertIsNone(m("<span class=diwali>Sunday</span>"))

	def test_text_before_the_open_tag_is_kept(self):
		text = "Diwali " + "<b></b>" * 711 + "<span class=holi>Sunday</span>"
		self.assertEqual(m(text), "diwali")
