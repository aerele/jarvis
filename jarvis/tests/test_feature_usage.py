"""Coverage for ``jarvis.chat.feature_usage.get_used_features`` - one positive
test per tracked feature, the "nothing used" case, the ``since`` cutoff, and
the "one broken source must not blank the survey" guarantee.

Ownership note that applies to nearly every fixture here: Frappe's
``set_user_and_timestamp`` overwrites ``owner`` with the session user on EVERY
insert, so an ``"owner"`` key in a ``get_doc`` payload is silently discarded.
Every row that has to belong to the test user is therefore re-owned with
``frappe.db.set_value(..., update_modified=False)`` afterwards, the same way
``test_chat_mining`` and ``test_agent_run_steps`` do it.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

import jarvis.chat.feature_usage as fu
from jarvis.chat.feature_usage import get_used_features

USER = "test_feature_usage@example.com"

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"


class TestGetUsedFeatures(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		if not frappe.db.exists("User", USER):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": USER,
					"first_name": "Feature Usage Test",
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
				}
			)
			user.flags.ignore_permissions = True
			user.insert(ignore_permissions=True)
		self.since = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-7)

	# ---------------------------------------------------------------- helpers
	def _own(self, doctype: str, name: str) -> str:
		"""Hand a just-inserted row to USER (see the module docstring)."""
		frappe.db.set_value(doctype, name, "owner", USER, update_modified=False)
		return name

	def _conv(self, file_box=0, **kwargs) -> str:
		doc = frappe.get_doc({"doctype": CONV, "title": "feature usage test", **kwargs})
		doc.insert(ignore_permissions=True)
		if file_box:
			# ``file_box`` 0 -> 1 is admin-gated in the controller; the real
			# enabler (filebox.drop_file) writes it with db.set_value too.
			frappe.db.set_value(CONV, doc.name, "file_box", 1, update_modified=False)
		return self._own(CONV, doc.name)

	def _msg(self, conversation: str, seq: int, role: str, *, own=False, **kwargs) -> str:
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conversation,
				"seq": seq,
				"role": role,
				**kwargs,
			}
		)
		doc.insert(ignore_permissions=True)
		return self._own(MSG, doc.name) if own else doc.name

	def _backdate(self, doctype: str, name: str, days: int) -> None:
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=days)
		frappe.db.set_value(doctype, name, "creation", old, update_modified=False)

	# -------------------------------------------------------------- file box
	def test_file_box_detected(self):
		self._conv(file_box=1)
		self.assertIn("file_box", get_used_features(USER, self.since))

	def test_file_box_outside_window_not_detected(self):
		conv = self._conv(file_box=1)
		self._backdate(CONV, conv, days=-30)
		self.assertNotIn("file_box", get_used_features(USER, self.since))

	def test_another_users_file_box_not_detected(self):
		doc = frappe.get_doc({"doctype": CONV, "title": "someone else", "file_box": 0})
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(CONV, doc.name, "file_box", 1, update_modified=False)
		self.assertNotIn("file_box", get_used_features(USER, self.since))

	# ------------------------------------------------------ dashboard builder
	def test_dashboard_builder_detected_via_tool_message(self):
		conv = self._conv()
		# BARE registry id: jarvis__* tool receipts are persisted by
		# jarvis.api.call_tool, which is dispatched on bare names, and the chat
		# worker explicitly skips persisting a duplicate jarvis__ row.
		self._msg(
			conv,
			1,
			"tool",
			tool_name="save_dashboard",
			content="save_dashboard → completed",
		)
		self.assertIn("dashboard_builder", get_used_features(USER, self.since))

	def test_dashboard_builder_ignores_unrelated_tools(self):
		conv = self._conv()
		self._msg(conv, 1, "tool", tool_name="get_list", content="get_list → completed")
		self.assertNotIn("dashboard_builder", get_used_features(USER, self.since))

	# ---------------------------------------------------------------- skills
	def _skill(self, slug: str) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Custom Skill",
				"skill_name": slug,
				"description": "feature usage test skill",
				"instructions": "Do the thing.",
				"enabled": 1,
				"scope": "User",
			}
		)
		doc.insert(ignore_permissions=True)
		return self._own("Jarvis Custom Skill", doc.name)

	def _org_skill(self, slug: str) -> str:
		"""An unrestricted Org-scope skill, invocable by anyone - NOT owned by
		USER (mirrors a real promoted/team skill, unlike ``_skill`` above). The
		engine flag bypasses the reviewer-only scope-creation guard."""
		prev = frappe.flags.jarvis_pattern_engine
		frappe.flags.jarvis_pattern_engine = True
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Jarvis Custom Skill",
					"skill_name": slug,
					"description": "feature usage org-wide test skill",
					"instructions": "Do the thing.",
					"enabled": 1,
					"scope": "Org",
				}
			)
			doc.insert(ignore_permissions=True)
		finally:
			frappe.flags.jarvis_pattern_engine = prev
		return doc.name

	def test_skills_detected_from_message_content(self):
		conv = self._conv()
		self._skill("recon-helper")
		self._msg(
			conv,
			1,
			"user",
			hidden=0,
			content="hey /recon-helper can you check this",
			own=True,
		)
		self.assertIn("skills", get_used_features(USER, self.since))

	def test_skills_not_detected_for_unknown_slug(self):
		conv = self._conv()
		self._msg(
			conv,
			1,
			"user",
			hidden=0,
			content="check /this-is-not-a-real-skill please",
			own=True,
		)
		self.assertNotIn("skills", get_used_features(USER, self.since))

	def test_skills_reports_the_newest_invoking_message(self):
		"""The joined-text gate must not cost per-row accuracy: with one real
		and one bogus slug present, the timestamp comes from the real one."""
		conv = self._conv()
		self._skill("recon-helper")
		older = self._msg(conv, 1, "user", hidden=0, content="run /recon-helper", own=True)
		self._msg(conv, 2, "user", hidden=0, content="and /not-a-skill too", own=True)
		self._backdate(MSG, older, days=-1)
		result = get_used_features(USER, self.since)
		self.assertIn("skills", result)
		self.assertEqual(result["skills"], str(frappe.db.get_value(MSG, older, "creation")))

	def test_skills_ignores_hidden_continuation_messages(self):
		conv = self._conv()
		self._skill("recon-helper")
		self._msg(conv, 1, "user", hidden=1, content="run /recon-helper", own=True)
		self.assertNotIn("skills", get_used_features(USER, self.since))

	def test_skills_not_detected_for_an_org_wide_only_mention(self):
		"""Code review on #580: an unrestricted Org-scope skill is invocable by
		every user in the tenant, so mentioning one is not evidence THIS user
		set up or personalized anything - it must not count as "Skills" usage
		(_skills passes include_org_wide=False)."""
		conv = self._conv()
		self._org_skill("team-wide-helper")
		self._msg(conv, 1, "user", hidden=0, content="run /team-wide-helper please", own=True)
		self.assertNotIn("skills", get_used_features(USER, self.since))

	def test_skills_still_detected_alongside_an_org_wide_mention(self):
		"""A genuine own-skill mention still counts even in the same window as
		an org-wide one - the exclusion is narrow, not a blanket suppression."""
		conv = self._conv()
		self._skill("recon-helper")
		self._org_skill("team-wide-helper")
		self._msg(conv, 1, "user", hidden=0, content="run /team-wide-helper please", own=True)
		self._msg(conv, 2, "user", hidden=0, content="now /recon-helper too", own=True)
		self.assertIn("skills", get_used_features(USER, self.since))

	# ---------------------------------------------------------------- macros
	def test_macros_detected(self):
		conv = self._conv()
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Macro Run",
				"conversation": conv,
				"status": "completed",
				"started_at": frappe.utils.now_datetime(),
			}
		)
		doc.insert(ignore_permissions=True)
		self._own("Jarvis Macro Run", doc.name)
		self.assertIn("macros", get_used_features(USER, self.since))

	def test_macros_detected_when_the_conversation_link_was_nulled(self):
		"""A deleted conversation nulls Jarvis Macro Run.conversation, which is
		exactly why this source reads the run's own owner instead of joining."""
		doc = frappe.get_doc({"doctype": "Jarvis Macro Run", "status": "completed"})
		doc.insert(ignore_permissions=True)
		self._own("Jarvis Macro Run", doc.name)
		self.assertIn("macros", get_used_features(USER, self.since))

	# --------------------------------------------------- triggers/connectors
	def test_trigger_activity_detected(self):
		frappe.get_doc(
			{
				"doctype": "Jarvis Trigger Activity",
				"trigger": "t-feature-usage",
				"status": "Success",
				"event_user": USER,
			}
		).insert(ignore_permissions=True)
		self.assertIn("triggers_connectors", get_used_features(USER, self.since))

	def test_connector_log_detected(self):
		frappe.get_doc(
			{
				"doctype": "Jarvis Connector Log",
				"connector": "c-feature-usage",
				"user": USER,
				"action": "call",
				"status": "Success",
			}
		).insert(ignore_permissions=True)
		self.assertIn("triggers_connectors", get_used_features(USER, self.since))

	# ------------------------------------------------------------ voice chat
	def test_voice_note_detected(self):
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Voice Note",
				"kind": "Voice",
				"transcript": "remember to invoice acme",
				"context_type": "Business",
				"status": "New",
			}
		)
		doc.insert(ignore_permissions=True)
		self._own("Jarvis Voice Note", doc.name)
		self.assertIn("voice_chat", get_used_features(USER, self.since))

	# ------------------------------------------------------------------ wiki
	def test_wiki_detected_from_sources_provenance(self):
		frappe.get_doc(
			{
				"doctype": "Jarvis Wiki Page",
				"slug": "feature-usage-test-page",
				"title": "Feature usage test page",
				"page_type": "Process",
				"scope": "Org",
				"status": "Active",
				"sources": frappe.as_json(
					[{"date": frappe.utils.today(), "kind": "manual", "ref": None, "user": USER}]
				),
			}
		).insert(ignore_permissions=True)
		self.assertIn("wiki", get_used_features(USER, self.since))

	def test_wiki_not_detected_for_another_users_provenance(self):
		frappe.get_doc(
			{
				"doctype": "Jarvis Wiki Page",
				"slug": "feature-usage-other-page",
				"title": "Someone else's page",
				"page_type": "Process",
				"scope": "Org",
				"status": "Active",
				"sources": frappe.as_json(
					[
						{
							"date": frappe.utils.today(),
							"kind": "manual",
							"ref": None,
							"user": "someone@else.invalid",
						}
					]
				),
			}
		).insert(ignore_permissions=True)
		self.assertNotIn("wiki", get_used_features(USER, self.since))

	def test_wiki_tolerates_a_corrupt_sources_value(self):
		page = frappe.get_doc(
			{
				"doctype": "Jarvis Wiki Page",
				"slug": "feature-usage-corrupt-page",
				"title": "Corrupt sources",
				"page_type": "Process",
				"scope": "Org",
				"status": "Active",
			}
		)
		page.insert(ignore_permissions=True)
		frappe.db.set_value("Jarvis Wiki Page", page.name, "sources", '["not-a-dict"]')
		self._conv(file_box=1)
		result = get_used_features(USER, self.since)
		self.assertNotIn("wiki", result)
		self.assertIn("file_box", result)

	# ------------------------------------------------------------ aggregates
	def test_no_features_used_returns_empty_dict(self):
		self._conv()  # a conversation with no matching signals
		self.assertEqual(get_used_features(USER, self.since), {})

	def test_blank_arguments_return_empty_dict(self):
		self._conv(file_box=1)
		self.assertEqual(get_used_features(USER, None), {})
		self.assertEqual(get_used_features("", self.since), {})

	def test_one_source_failing_does_not_blank_others(self):
		self._conv(file_box=1)
		with patch.object(fu, "_macros", side_effect=RuntimeError("boom")):
			result = get_used_features(USER, self.since)
		self.assertIn("file_box", result)
		self.assertNotIn("macros", result)
