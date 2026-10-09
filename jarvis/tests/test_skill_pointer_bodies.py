"""What the container is sent for a company skill: its name and description, and a
pointer in place of its instructions.

The instructions are served by the bench when the skill is used (``jarvis__get_skill``),
where "enabled" and who may use it are checked at that moment. Learned skills still
travel with their full text.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import custom_skills
from jarvis.chat.custom_skills import (
	build_push_payload,
	prefixed_slug,
	render_learned_skill_md,
	render_skill_md,
)

SKILL = "Jarvis Custom Skill"
OWNER = "ptr-owner@example.com"
SECRET = "POINTER-TEST-INSTRUCTIONS-never-pushed"


def _mk(suffix: str, *, description: str = "does a thing", user_invocable: int = 0):
	"""A raw Org row owned by ``OWNER`` (``build_push_payload(owner=...)`` then sees an
	exact set whatever else the site holds)."""
	doc = frappe.new_doc(SKILL)
	doc.update(
		{
			"skill_name": f"ptr-{suffix}",
			"description": description,
			"instructions": f"{SECRET} {suffix}",
			"enabled": 1,
			"user_invocable": user_invocable,
			"scope": "Org",
			"managed_by_learning": 0,
		}
	)
	doc.creation = doc.modified = frappe.utils.now()
	doc.owner = doc.modified_by = OWNER
	doc.flags.name_set = True
	doc.name = f"ptr-row-{suffix}"
	doc.db_insert()
	return doc


class TestThePointer(FrappeTestCase):
	def test_the_body_points_at_the_fetch_tool_by_wire_name(self):
		md = render_skill_md("invoicing", "Books invoices", True)
		body = md.split("---\n", 2)[2]
		self.assertIn('skill_name "custom-invoicing"', body)
		self.assertIn("jarvis__get_skill", body)
		# The two answers the tool really gives for a skill that is gone or not yours.
		self.assertIn('"unknown skill"', body)
		self.assertIn('"no access to skill"', body)
		self.assertNotIn("—", md)

	def test_the_frontmatter_is_what_it_was(self):
		md = render_skill_md("invoicing", 'Books "sales"\ninvoices', True)
		head = md.split("---\n")[1]
		self.assertEqual(
			head,
			'name: custom-invoicing\ndescription: "Books \\"sales\\" invoices"\nuser-invocable: true\n',
		)
		self.assertIn("user-invocable: false", render_skill_md("x-y", "d", False))

	def test_the_body_is_the_same_for_every_skill_apart_from_its_name(self):
		a = render_skill_md("alpha", "one", True).split("---\n", 2)[2]
		b = render_skill_md("beta", "two", False).split("---\n", 2)[2]
		self.assertEqual(a.replace("custom-alpha", "X"), b.replace("custom-beta", "X"))

	def test_a_learned_skill_still_travels_with_its_text(self):
		md = render_learned_skill_md("learned-selling", "Selling defaults", "Quote in INR.")
		self.assertIn("Quote in INR.", md)
		self.assertNotIn("jarvis__get_skill", md)


class TestWhatIsPushed(FrappeTestCase):
	def setUp(self):
		self._sweep()

	def tearDown(self):
		self._sweep()

	@staticmethod
	def _sweep():
		frappe.db.delete(SKILL, {"name": ["like", "ptr-row-%"]})

	def test_an_item_carries_a_pointer_and_none_of_the_instructions(self):
		_mk("one", description="First", user_invocable=1)
		_mk("two", description="Second")
		payload = build_push_payload(owner=OWNER)
		self.assertEqual([p["slug"] for p in payload], [prefixed_slug("ptr-one"), prefixed_slug("ptr-two")])
		for item in payload:
			self.assertEqual(set(item), {"slug", "description", "user_invocable", "body"})
			self.assertNotIn(SECRET, item["body"])
			self.assertIn(f'skill_name "{item["slug"]}"', item["body"])
			self.assertIn(f"name: {item['slug']}\n", item["body"])
		self.assertEqual((payload[0]["description"], payload[0]["user_invocable"]), ("First", True))

	def test_the_push_does_not_read_the_instructions_column(self):
		_mk("light")
		rows = custom_skills._pushable_org_rows(OWNER)
		self.assertTrue(rows)
		self.assertNotIn("instructions", rows[0])
