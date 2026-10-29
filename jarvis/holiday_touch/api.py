"""The holiday touch endpoint.

``get_today`` answers for the session user only: it takes no user argument, so it cannot be
asked about someone else. The same payload rides the desktop page boot
(``context.boot["holiday_touch"]`` in ``www/jarvis.py``); this endpoint is for a client that
must refresh it without a reload. A 403 here means "none" to the client.
"""

from __future__ import annotations

import frappe

from jarvis.holiday_touch import resolve
from jarvis.permissions import require_jarvis_access


@frappe.whitelist(methods=["GET"])
def get_today() -> dict:
	"""What the holiday touch shows the session user today. A Guest or a user without Jarvis
	access gets ``frappe.PermissionError`` (403); any other fault becomes a ``none`` payload."""
	require_jarvis_access()  # a Guest has no access: it is refused here too
	return resolve.safe_answer(frappe.session.user)
