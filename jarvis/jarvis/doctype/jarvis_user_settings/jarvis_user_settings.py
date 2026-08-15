"""Jarvis User Settings DocType controller.

One row per Frappe user, holding that user's chat preferences
(``notify_enabled`` / ``activity_detail``), an admin-set all-time token cap
(``monthly_token_limit`` - legacy fieldname, the cap is now all-time not
monthly, permlevel 1), and the read-only usage counters the turn handler
increments via atomic SQL (``jarvis.chat.usage``).

Rows are created lazily by ``jarvis.chat.usage.get_or_create_user_settings``
with an explicit ``owner`` so the ``if_owner`` permlevel-0 grant holds even
when an admin triggered creation. The counter fields are ``read_only`` in the
schema and only ever written by server code (never a desk save), so this
controller stays minimal.
"""

import frappe
from frappe.model.document import Document


class JarvisUserSettings(Document):
	# Minimal controller: identity is enforced by field constraints (``user``
	# is a mandatory, unique Link and the autoname source) and the counters
	# are mutated only by jarvis.chat.usage via SQL. No validation hook needed.
	pass
