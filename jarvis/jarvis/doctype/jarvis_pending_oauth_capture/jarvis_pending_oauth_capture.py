# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class JarvisPendingOAuthCapture(Document):
	"""Durable, encrypted, short-lived home for a minted provider OAuth blob
	between the token exchange and the desired-state save that adopts it
	(plan-05 D2, review P0-04 / §8.2).

	The controller logic lives in ``jarvis.oauth.pending_capture`` (create /
	consume / rehydrate / revoke-and-sweep) - this class only names the DocType.
	"""

	pass
