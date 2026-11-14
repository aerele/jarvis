"""Document Intake: the one place that reads how many rows an UPDATE matched."""

import frappe


def update_rowcount(query) -> int:
	"""Run the pypika UPDATE ``query`` and return how many rows it matched.

	Claims are one conditional UPDATE (``... WHERE status = 'Queued'``): the count says
	whether this caller won the row. Frappe has no public accessor for it, so it reads the
	DB cursor of the current connection, directly after the UPDATE ran."""
	query.run()
	return frappe.db._cursor.rowcount
