"""Give an agent_token with no issue date one (#16b).

Only rotate_agent_token used to stamp ``agent_token_issued_at``, so on most sites
it was empty and the token's age was unknown. The site now stamps it whenever it
receives a token; this starts the clock for the token already in place, so the
daily ``oauth.cron.check_agent_token_age`` can tell System Managers to rotate it.
The age is advisory (nothing is refused for being old). Idempotent.
"""

import frappe
from frappe.utils.password import get_decrypted_password


def execute():
	if frappe.db.get_single_value("Jarvis Settings", "agent_token_issued_at"):
		return
	if not get_decrypted_password("Jarvis Settings", "Jarvis Settings", "agent_token", raise_exception=False):
		return
	frappe.db.set_single_value("Jarvis Settings", "agent_token_issued_at", frappe.utils.now_datetime())
