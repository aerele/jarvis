"""send_email's ``recipients_from``: the recipients of a mail-merge come from a query the
bench runs, never from addresses the model typed.

Why (6 Nov 2026, a Diwali greeting to 171 customers): the model copied 171 addresses out
of a query result into ``bcc`` and 3 came out misspelled, on a card that showed only the
first few. A reference cannot be mistyped: the model names the query (the ``result_id``
of a partial ``query`` answer, or the query spec itself, which holds no addresses) and
which columns hold the email and the record; the bench runs it as the user, with the
query tool's own gates, and writes one message per row.

The expanded ``messages`` are what the card shows and what Confirm sends. At send time
the query is run again and must give the same messages (``assert_unchanged``), so the
send is exactly what the card showed, and a hand-made ``messages`` list can never claim
the larger merge cap.
"""

from __future__ import annotations

import re

import frappe

from jarvis.exceptions import InvalidArgumentError

# One call's ceiling for a bench-resolved merge. A model-typed ``messages`` list keeps
# the shared 20 (jarvis.tools._bulk._MAX_BATCH), and so does a merge that runs without a
# card (UNCARDED_MAX).
MAX_MERGE = 500
UNCARDED_MAX = 20

_KEYS = frozenset({"result_id", "spec", "email_field", "name_field", "doctype"})
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# Fields the merge copies onto every message from the call itself.
_SHARED = ("cc", "bcc", "print_format", "send_me_a_copy", "attachments")


def expand(args: dict) -> tuple[dict, dict]:
	"""``(stored, note)``: the model's call with ``recipients_from`` expanded into
	``messages`` (a new dict; the caller's is left as it was), and what the merge left
	out, for the card. Only the model's own call comes here, so ``messages`` beside
	``recipients_from`` is refused: those are written only by this function."""
	if args.get("recipients"):
		raise InvalidArgumentError("pass recipients or recipients_from, not both")
	if args.get("messages") is not None:
		raise InvalidArgumentError("pass messages or recipients_from, not both")
	messages, note = build_messages(
		args.get("recipients_from"),
		subject=args.get("subject"),
		content=args.get("content"),
		shared={k: args[k] for k in _SHARED if args.get(k) is not None},
	)
	return {**args, "messages": messages}, note


def assert_unchanged(args: dict) -> list:
	"""Re-run the query at send time and return the messages, refusing when they differ
	from the ones the card showed (``args["messages"]``, set by ``expand``)."""
	fresh, _note = build_messages(
		args.get("recipients_from"),
		subject=args.get("subject"),
		content=args.get("content"),
		shared={k: args[k] for k in _SHARED if args.get(k) is not None},
	)
	if args.get("messages") is None:
		# No card: an armed macro or an approved skill run sends without a person reading
		# the list, so it keeps the typed batch's ceiling. Only a reviewed card goes past it.
		if len(fresh) > UNCARDED_MAX:
			raise InvalidArgumentError(
				f"this merge has {len(fresh)} recipients; without a confirmation card at most "
				f"{UNCARDED_MAX} can be emailed in one call. Narrow the query."
			)
	elif _comparable(fresh) != _comparable(args["messages"]):
		raise InvalidArgumentError(
			"the recipient list changed since the confirmation card was shown (a contact was "
			"added, removed or edited). Nothing was sent. Run the send again to show a new card."
		)
	return fresh


def build_messages(src, *, subject, content, shared: dict) -> tuple[list, dict]:
	"""``(messages, note)``: one message per row with a valid, not yet seen address.
	``note`` counts what was left out, for the card."""
	spec, email_field, name_field, doctype = _source(src)
	if not subject:
		raise InvalidArgumentError("subject is required")
	if content is None or content == "":
		raise InvalidArgumentError("content is required")

	from jarvis.tools.query import query_rows

	rows, masked, limited = query_rows(spec, max_rows=MAX_MERGE)
	if any(email_field in fields for fields in masked.values()):
		raise InvalidArgumentError(f"the {email_field} column is masked for you; it cannot be emailed")
	if not rows:
		raise InvalidArgumentError("the query matches no rows, so there is no one to email")
	columns = set(rows[0])
	for needed in (email_field, name_field):
		if needed not in columns:
			raise InvalidArgumentError(
				f"the query does not select {needed!r}; its columns are {sorted(columns)}"
			)
	used = set(_PLACEHOLDER.findall(subject)) | set(_PLACEHOLDER.findall(content))
	unknown = sorted(used - columns)
	if unknown:
		raise InvalidArgumentError(
			f"placeholders {unknown} are not columns of the query; use one of {sorted(columns)}"
		)

	messages = []
	seen = set()
	no_email = duplicate = 0
	blank_fill = {}
	for row in rows:
		address = str(row.get(email_field) or "").strip()
		if not address or not frappe.utils.validate_email_address(address):
			no_email += 1
			continue
		if address.lower() in seen:
			duplicate += 1
			continue
		name = row.get(name_field)
		if not name:
			raise InvalidArgumentError(f"a row has no {name_field}; every email needs its {doctype}")
		for field in used:
			if row.get(field) in (None, ""):
				blank_fill[field] = blank_fill.get(field, 0) + 1
		seen.add(address.lower())
		messages.append(
			{
				**shared,
				"doctype": doctype,
				"name": name,
				"recipients": address,
				"subject": _fill(subject, row),
				"content": _fill(content, row),
			}
		)
	if blank_fill:
		field, n = next(iter(sorted(blank_fill.items())))
		raise InvalidArgumentError(
			f"{n} recipient(s) have no {field}, so {{{{ {field} }}}} would be blank; add a filter "
			f"that {field} is set, or remove the placeholder"
		)
	if not messages:
		raise InvalidArgumentError("no row has a valid email address")
	_assert_references(doctype, [m["name"] for m in messages], name_field)
	note = {"rows": len(rows), "no_email": no_email, "duplicate": duplicate, "limited": limited}
	return messages, note


def _source(src) -> tuple[dict, str, str, str]:
	if not isinstance(src, dict):
		raise InvalidArgumentError(
			"recipients_from must be {result_id or spec, email_field, name_field?, doctype?}"
		)
	extra = sorted(set(src) - _KEYS)
	if extra:
		raise InvalidArgumentError(f"recipients_from does not take {extra}")
	if (src.get("result_id") is None) == (src.get("spec") is None):
		raise InvalidArgumentError("recipients_from takes exactly one of result_id or spec")
	if src.get("result_id") is not None:
		from jarvis.tools._export.saved_queries import resolve

		spec = resolve(src["result_id"])
	else:
		spec = src["spec"]
		if not isinstance(spec, dict):
			raise InvalidArgumentError("recipients_from.spec must be a query spec (the query tool's)")
	email_field = src.get("email_field")
	if not isinstance(email_field, str) or not email_field:
		raise InvalidArgumentError("recipients_from.email_field is required: the column holding the address")
	name_field = src.get("name_field") or "name"
	doctype = src.get("doctype") or spec.get("from")
	if not isinstance(doctype, str) or not doctype:
		raise InvalidArgumentError("recipients_from.doctype is required when the spec has no from")
	return spec, email_field, name_field, doctype


def _assert_references(doctype: str, names: list, name_field: str) -> None:
	"""Every email is logged against its record, and send_email refuses one the user cannot
	read. Check them all now, so a wrong ``name_field`` is refused at the card, not after
	Confirm. A query that selects two tables' ``name`` keeps only the last one (the
	usual cause): select only the reference table's name, or name the column with
	``name_field``."""
	if not frappe.has_permission(doctype, "read"):
		raise InvalidArgumentError(f"you cannot read {doctype}, so no email can be logged against it")
	readable = set(
		frappe.get_list(doctype, filters={"name": ["in", list(set(names))]}, pluck="name", limit=0)
	)
	missing = [n for n in names if n not in readable]
	if missing:
		raise InvalidArgumentError(
			f"{len(missing)} of {len(names)} {name_field!r} values are not {doctype} records you can read "
			f"(e.g. {missing[0]!r}). Select the {doctype}'s own name for {name_field!r}: when two joined "
			f"tables both select name, only the last is kept."
		)


def _fill(text: str, row: dict) -> str:
	return _PLACEHOLDER.sub(lambda m: str(row.get(m.group(1)) or ""), text)


def _comparable(messages) -> list:
	keys = ("doctype", "name", "recipients", "subject", "content")
	return [tuple(m.get(k) for k in keys) for m in messages or [] if isinstance(m, dict)]
