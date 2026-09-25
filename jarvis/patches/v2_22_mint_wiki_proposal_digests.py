"""Mint ``wiki_digest`` for existing File Box wiki proposals (PR-1, F8).

A proposal made before the digest shipped has none, and approve/retry now
refuse an unverified proposal. Mint over the stored values of every row a
reviewer can still act on: Pending, or Approved but not yet Applied. Idempotent
(only rows without a digest) and batched.
"""

import frappe

_BATCH = 500


def execute():
	if not frappe.db.has_column("Jarvis Approval Request", "wiki_digest"):
		return
	from jarvis.chat.approvals_api import FILE_BOX_WIKI_SOURCE, wiki_digest

	while True:
		rows = frappe.db.sql(
			"""SELECT name, conversation, wiki_payload FROM `tabJarvis Approval Request`
			WHERE source = %(src)s AND COALESCE(wiki_digest, '') = ''
			  AND COALESCE(wiki_payload, '') != ''
			  AND (status = 'Pending'
			       OR (status = 'Approved' AND COALESCE(apply_status, 'Pending') != 'Applied'))
			LIMIT %(n)s""",
			{"src": FILE_BOX_WIKI_SOURCE, "n": _BATCH},
			as_dict=True,
		)
		if not rows:
			break
		for r in rows:
			frappe.db.sql(
				"""UPDATE `tabJarvis Approval Request` SET wiki_digest = %s
				WHERE name = %s AND COALESCE(wiki_digest, '') = ''""",
				(wiki_digest(r.name, r.conversation, r.wiki_payload), r.name),
			)
		frappe.db.commit()
