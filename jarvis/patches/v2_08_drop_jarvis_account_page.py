"""Remove the retired ``jarvis-account`` Desk page.

Billing now lives in the SPA at ``/jarvis/billing``. The desk page's files
(``jarvis/page/jarvis_account/*``) are deleted in this same change, which stops
a FRESH install from ever creating the record - but Frappe does not remove an
already-installed standard Page when its JSON disappears, so every existing site
would keep a ``Page`` row named ``jarvis-account`` and keep serving a dead
billing UI at ``/app/jarvis-account``.

Deleting the row is what makes the ``website_redirects`` entry in hooks.py the
only thing left at that URL, which matters beyond tidiness: admin-v2 still mails
renewal links pointing there (``jarvis_admin_v2/billing/notifications.py``), so
the path has to resolve to the new page rather than to a stale one.

Harmless as a no-op, which is the property this patch needs most: a fresh
install runs ``install_app(set_as_patched=True)``, which marks every patch done
WITHOUT running it, so this must be correct both when it runs and when it never
does. A delete-if-exists is that by construction - on a fresh site the record
was never created, and re-running finds nothing.
"""

import frappe


def execute():
	# force=True: the Page is "standard", and a standard record refuses a plain
	# delete on a site where the developer flag is off.
	# ignore_missing=True covers the fresh-install case where it never existed.
	try:
		if frappe.db.exists("Page", "jarvis-account"):
			frappe.delete_doc(
				"Page",
				"jarvis-account",
				force=True,
				ignore_missing=True,
				ignore_permissions=True,
				delete_permanently=True,
			)
	except Exception:
		# Never fail a migrate over a retired Desk page. Worst case the row
		# survives and the redirect below it still takes the customer to the SPA.
		frappe.log_error(title="v2_08_drop_jarvis_account_page")

	frappe.db.commit()
