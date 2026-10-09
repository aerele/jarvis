"""After a guarded structure write was saved: is what was stored what the card
showed? (Owner decisions R2-17 and R2-20.)

The card is built from the sealed call, in stored form, and the same checks run
again at Confirm. That is a prediction of what Frappe's save will store. Twice it
was wrong in ways only tests caught (a cache Frappe reads mid-save; a controller
that changed between Frappe 16.36 and 16.51). This reads the record back, still
under the form's lock and inside the confirm, and compares it with what the
sealed call named. A difference fails the confirm, which then rolls back and
cleans up as it does after any failed guarded write: nothing a person did not see
on the card is kept.

Only what the call named is compared (its values, and each cell of each row of a
table it sent), through the rule the card itself uses to tell whether a value
changes (``same_value``: cast as the save casts). Whatever else the save set
(timestamps, defaults, what a controller derives) is not the card's claim and is
not looked at; nor are the record counts on the card, which may drift. A value is
never put in the message or the log: only which field differed.
"""

from __future__ import annotations

import frappe

from jarvis.chat._record_summary import same_value

_TABLES = ("Table", "Table MultiSelect")


def unlike_the_card(args: dict, undo: dict, tracebacks: list | None = None) -> str | None:
	"""Which field of the saved record is not what the sealed call ``args`` named,
	or None when all of it is. ``undo``: the confirm's clean-up state, whose
	``written`` names the record a create made. A record that cannot be read back
	counts as unlike: the check fails closed, and the traceback goes to
	``tracebacks`` for the caller to log AFTER its rollback (a log written here
	would go with it)."""
	doctype = args.get("doctype")
	update = "changes" in args
	sent = args.get("changes") if update else args.get("values")
	if not isinstance(doctype, str) or not isinstance(sent, dict):
		return "the confirmed call could not be read"
	written = undo.get("written") if isinstance(undo.get("written"), dict) else {}
	name = args.get("name") if update else written.get("name")
	if not name:
		return "the record that was saved could not be found"
	try:
		meta = frappe.get_meta(doctype)
		doc = frappe.get_doc(doctype, name)
		return _unlike(meta, doc, sent)
	except Exception:
		if tracebacks is not None:
			tracebacks.append(frappe.get_traceback())
		return "the record that was saved could not be read back"


def _unlike(meta, doc, sent: dict) -> str | None:
	for key, value in sent.items():
		df = meta.get_field(key)
		if df is None or df.fieldtype == "Password":
			continue  # not a field of the form; a secret is stored encrypted elsewhere
		label = df.label or key
		if df.fieldtype in _TABLES:
			if isinstance(value, list):
				found = _unlike_rows(label, frappe.get_meta(df.options), doc.get(key) or [], value)
				if found:
					return found
		elif not same_value(doc.get(key), value, df):
			return label
	return None


def _unlike_rows(label: str, child, stored: list, sent: list) -> str | None:
	if len(stored) != len(sent):
		return f"{label}: {len(stored)} rows were saved, the card showed {len(sent)}"
	for i, (was, row) in enumerate(zip(stored, sent, strict=True), 1):
		if not isinstance(row, dict):
			continue
		for key, value in row.items():
			df = child.get_field(key)
			if df is None or df.fieldtype == "Password" or df.fieldtype in _TABLES:
				continue
			if not same_value(was.get(key), value, df):
				return f"{label} row {i}, {df.label or key}"
	return None
