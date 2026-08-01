"""Grandfather existing chat users past the first-chat introduction.

The chat home now opens with a one-time, assistant-styled introduction, gated
on ``Jarvis User Settings.home_intro_seen_version`` (0 = never shown). That
column arrives at 0 for EVERY row, so without this patch the release would
introduce Jarvis to people who have been using it for months - the moment they
next opened an empty chat home they would be told what File Box is.

The signal for "this user already knows" is deliberately behavioural rather
than temporal: they own at least one conversation that has at least one
message. A conversation with no messages proves nothing (an abandoned New Chat,
or a File Box drop that never sent), so it does not count.

Bulk SQL in two statements, both idempotent:

1. UPDATE every existing settings row belonging to such a user. The
   ``home_intro_seen_version < 1`` predicate makes a re-run a no-op and, more
   importantly, means the patch can never LOWER a version - if a later release
   bumps HOME_INTRO_VERSION and this patch is somehow replayed, a user who has
   already acknowledged version 2 keeps it.
2. INSERT a row for the rare veteran who has none. Settings rows are created
   lazily (usage counters, the greeting cadence counter, a persona change), so
   nearly every veteran already has one - but "nearly" is not "all", and a
   missing row reads as seen=0, which is exactly the case this patch exists to
   prevent. Omitted columns take their schema defaults (notify_enabled 1,
   activity_detail 1, preferred_persona 'Jarvis'), the same values
   ``get_or_create_user_settings`` would have produced. ``owner`` is the
   settings user, not Administrator, because the permlevel-0 grant is
   ``if_owner`` (see get_or_create_user_settings for the same rule).

Both statements join ``tabUser`` so a conversation left behind by a deleted
user cannot mint an orphan settings row pointing at a User that is gone.

Genuinely new users - no conversations, or only empty ones - are untouched and
see the introduction, which is the whole point of the feature.
"""

import frappe

SETTINGS = "Jarvis User Settings"

# Users with real chat history: at least one conversation carrying at least one
# message. Shared by both statements below so the two can never disagree on who
# counts as a veteran.
_VETERANS = """
	SELECT DISTINCT c.owner AS user
	FROM `tabJarvis Conversation` c
	JOIN `tabUser` u ON u.name = c.owner
	WHERE EXISTS (
		SELECT 1 FROM `tabJarvis Chat Message` m WHERE m.conversation = c.name
	)
"""


def execute() -> None:
	# The column lands in post_model_sync, before patches; guard anyway so a
	# partial or --skip-failing deploy state no-ops instead of 500-ing.
	if not frappe.db.has_column(SETTINGS, "home_intro_seen_version"):
		return

	now = frappe.utils.now_datetime()

	frappe.db.sql(
		f"""
		UPDATE `tabJarvis User Settings` s
		JOIN ({_VETERANS}) v ON v.user = s.user
		SET s.home_intro_seen_version = 1, s.home_intro_seen_at = %(now)s
		WHERE s.home_intro_seen_version < 1
		""",
		{"now": now},
	)

	frappe.db.sql(
		f"""
		INSERT INTO `tabJarvis User Settings`
			(name, creation, modified, modified_by, owner, docstatus, idx,
			 user, home_intro_seen_version, home_intro_seen_at)
		SELECT v.user, %(now)s, %(now)s, 'Administrator', v.user, 0, 0,
		       v.user, 1, %(now)s
		FROM ({_VETERANS}) v
		LEFT JOIN `tabJarvis User Settings` s ON s.user = v.user
		WHERE s.name IS NULL
		""",
		{"now": now},
	)

	frappe.db.commit()
