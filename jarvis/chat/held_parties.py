"""Party identity for held File Box writes (the records a held create/update writes).

- ``items_of(tool, args)``: one ``{doctype, values, name, op}`` per record written;
- ``item_key(item)``: the per-item dedup key, scoped by doctype: the normalised GSTIN
  (``gstin``, else ERPNext's ``tax_id``), else the normalised name (its naming field,
  else its title), else a canonical args hash (an update keys on its target + a hash
  of its changes; an Address on its party, type and spot, as a sheet keys it);
- ``find_existing(...)``: records that already look like the party, read as the
  CURRENT user (callers impersonate the row's ``exec_user``)."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

import frappe

CREATE_TOOLS = frozenset({"create_doc"})
MAX_CANDIDATES = 5
_LIKE_SCAN = 50
_NOT_GSTIN = re.compile(r"[^0-9A-Z]")
_COMPANY_SCOPED = frozenset({"Account"})


def company_scoped(doctype: str) -> bool:
	meta = _meta(doctype) if doctype in _COMPANY_SCOPED else None
	return bool(meta and meta.has_field("company") and meta.has_field("account_name"))


def scoped_name(doctype: str, values: dict, name: str) -> str:
	"""Keep unrelated identities unchanged; an Account belongs to one company/number.

	Hash the scope tuple so punctuation in company names or numbers cannot introduce
	ambiguous delimiters. Keep the name prefix used by the existing identity locks.
	"""
	if not name or not company_scoped(doctype):
		return name
	scope = [str(values.get(k) or "").strip().casefold() for k in ("company", "account_number")]
	return f"{name}@{_digest(scope)}"


def missing_scope(items: list[dict]) -> bool:
	return any(
		i["op"] == "create"
		and company_scoped(i["doctype"])
		and (not isinstance(i["values"].get("company"), str) or not i["values"]["company"].strip())
		for i in items
	)


SCOPE_REQUIRED = "Account needs an explicit company. Specify it; do not guess or use a default."


def items_of(tool: str, args) -> list[dict]:
	"""The records ``tool(args)`` writes; ``[]`` for any other tool or a malformed call."""
	if not isinstance(args, dict):
		return []
	if tool in CREATE_TOOLS:
		docs = args.get("docs")
		if isinstance(docs, list):
			return [
				_item(d.get("doctype"), d.get("values"), None, "create")
				for d in docs or []
				if isinstance(d, dict)
			]
		return [_item(args.get("doctype"), args.get("values"), None, "create")]
	if tool == "update_doc":
		updates = args.get("updates")
		if isinstance(updates, list):
			return [
				_item(args.get("doctype"), u.get("changes"), u.get("name"), "update")
				for u in updates
				if isinstance(u, dict)
			]
		return [_item(args.get("doctype"), args.get("changes"), args.get("name"), "update")]
	return []


def _item(doctype, values, name, op) -> dict:
	return {
		"doctype": str(doctype or ""),
		"values": values if isinstance(values, dict) else {},
		"name": str(name or ""),
		"op": op,
	}


def norm_gstin(value) -> str:
	return _NOT_GSTIN.sub("", str(value or "").upper())


def norm_name(value) -> str:
	"""Casefolded words of letters, digits and combining marks (an Indic matra,
	virama, anusvara or nukta is part of a name); anything else separates."""
	text = unicodedata.normalize("NFKC", str(value or "")).casefold()
	return " ".join("".join(c if unicodedata.category(c)[0] in "LNM" else " " for c in text).split())


def _meta(doctype: str):
	try:
		return frappe.get_meta(doctype) if doctype and frappe.db.exists("DocType", doctype) else None
	except Exception:
		return None


def title_field(meta) -> str | None:
	if not meta:
		return None
	if meta.title_field and meta.has_field(meta.title_field):
		return meta.title_field
	guess = f"{frappe.scrub(meta.name)}_name"
	return guess if meta.has_field(guess) else None


def tax_field(meta) -> str | None:
	"""India Compliance's ``gstin``, else ERPNext's ``tax_id``."""
	return next((f for f in ("gstin", "tax_id") if meta and meta.has_field(f)), None)


def is_party(doctype: str) -> bool:
	"""A party master (it carries a tax identity): Supplier, Customer, ... Never an
	Address: India Compliance puts its party's GSTIN on it, and a party has many."""
	return doctype != "Address" and bool(tax_field(_meta(doctype)))


def party_of(item: dict) -> dict:
	"""``{doctype, title, gstin}`` for one item (empty strings when unknown). An
	Address has no gstin here: one GSTIN sits on a party's several addresses."""
	meta = _meta(item["doctype"])
	values = item["values"]
	tax = tax_field(meta) if item["doctype"] != "Address" else None
	gstin = norm_gstin(values.get(tax)) if tax else ""
	field = title_field(meta)
	title = " ".join(str(values.get(field) or "").split()) if field else ""
	return {"doctype": item["doctype"], "title": title or item.get("name") or "", "gstin": gstin}


def _digest(value) -> str:
	canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
	return hashlib.sha256(canonical.encode()).hexdigest()


def item_key(item: dict) -> str:
	from jarvis.chat import held_sheets  # it imports this module

	if item["op"] == "update":
		# Two different changes to one record are two decisions, never one row.
		return f"doc:{item['doctype']}:{item['name']}:{_digest(item['values'])}"
	if item["doctype"] == "Address":
		return held_sheets.identities(item)[0]
	party = party_of(item)
	if party["gstin"]:
		return f"gstin:{item['doctype']}:{party['gstin']}"
	# Its naming field when set (an Item's item_code), else its title.
	named = _meta(item["doctype"]) and held_sheets.deterministic_name(item["doctype"], item["values"])
	name = scoped_name(item["doctype"], item["values"], norm_name(named or party["title"]))
	if name:
		return f"name:{item['doctype']}:{name}"
	return "args:" + _digest({"doctype": item["doctype"], "values": item["values"]})


def is_party_key(key: str) -> bool:
	return key.startswith(("gstin:", "name:"))


def existing_of(item: dict, *, limit=MAX_CANDIDATES, ignore_permissions=False) -> list[dict]:
	"""Readable matches in the proposed scope, not a same-named foreign ledger."""
	if not company_scoped(item["doctype"]):
		party = party_of(item)
		return find_existing(
			item["doctype"],
			title=party["title"],
			gstin=party["gstin"],
			limit=limit,
			ignore_permissions=ignore_permissions,
		)
	values = item["values"]
	if (
		missing_scope([item])
		or not isinstance(values.get("account_name"), str)
		or not values["account_name"].strip()
	):
		return []
	number = str(values.get("account_number") or "").strip()
	try:
		rows = frappe.get_list(
			"Account",
			filters={
				"company": values["company"].strip(),
				"account_name": values["account_name"],
				"account_number": number or ["is", "not set"],
			},
			fields=["name", "account_name", "company", "account_currency"],
			limit_page_length=limit,
			ignore_permissions=ignore_permissions,
		)
	except frappe.PermissionError:
		return []
	return [
		{
			"name": r.name,
			"title": r.account_name,
			"gstin": "",
			"company": r.company,
			"currency": r.account_currency,
		}
		for r in rows
	]


def account_currency_conflict(values: dict, currency: str) -> bool:
	want = str(values.get("account_currency") or "").strip().casefold()
	return bool(want and want != str(currency or "").casefold())


def account_selection_error(values: dict, name: str) -> str:
	"""After the caller's read check, enforce an Account proposal's scope on reuse."""
	if not isinstance(values.get("company"), str) or not values["company"].strip():
		return SCOPE_REQUIRED
	rows = frappe.get_list(
		"Account", filters={"name": name}, fields=["company", "account_currency"], limit_page_length=1
	)
	if not rows:
		return "That Account was not found, or the person who dropped the file can't open it."
	company = str(rows[0].company or "").strip().casefold()
	if company != values["company"].strip().casefold() or account_currency_conflict(
		values, rows[0].account_currency
	):
		return "Choose an Account in the proposed company with the requested currency."
	return ""


def existing_note(match: dict) -> str:
	label = match["title"] or match["name"]
	scope = (
		f" in {match['company']} ({match.get('currency') or 'currency not set'})"
		if match.get("company")
		else ""
	)
	if match.get("company") and account_currency_conflict(
		{"account_currency": match.get("requested_currency")}, match.get("currency")
	):
		return (
			f"Account {label} already exists{scope} (name: {match['name']}) with a different currency. "
			"Review its currency or use a different account name; nothing was created."
		)
	return (
		f"{match['doctype']} {label} already exists{scope} (name: {match['name']}). "
		"Use it: leave it out of this call and do not create it again."
	)


def find_existing(
	doctype: str,
	*,
	title: str = "",
	gstin: str = "",
	limit: int = MAX_CANDIDATES,
	ignore_permissions: bool = False,
) -> list[dict]:
	"""Existing ``doctype`` records matching the GSTIN or the normalised name, as
	``[{name, title, gstin}]``; permission-scoped to the current user unless
	``ignore_permissions``. A name match carrying a different tax id is another
	registration, not this party."""
	meta = _meta(doctype)
	if not meta or meta.istable or meta.issingle:
		return []
	field = title_field(meta)
	tax = tax_field(meta)
	fields = ["name", *([field] if field else []), *([tax] if tax else [])]
	found: dict = {}

	def _scan(filters, cap):
		try:
			return frappe.get_list(
				doctype,
				filters=filters,
				fields=fields,
				limit_page_length=cap,
				ignore_permissions=ignore_permissions,
			)
		except frappe.PermissionError:
			return []

	gstin = norm_gstin(gstin)
	if gstin and tax:
		for r in _scan({tax: gstin}, limit):
			found.setdefault(r.name, r)
	want = norm_name(title)
	if want:
		raw = " ".join(str(title).split())
		for column in [c for c in (field, "name") if c]:
			# The exact value first, then every token (never one token's first page).
			for filters in ({column: raw}, [[column, "like", f"%{t}%"] for t in want.split()]):
				for r in _scan(filters, _LIKE_SCAN):
					if want not in (norm_name(r.get(field) if field else ""), norm_name(r.name)):
						continue
					theirs = norm_gstin(r.get(tax)) if tax else ""
					if gstin and theirs and theirs != gstin:
						continue
					found.setdefault(r.name, r)
	return [
		{
			"name": r.name,
			"title": (r.get(field) if field else "") or "",
			"gstin": (r.get(tax) if tax else "") or "",
		}
		for r in list(found.values())[:limit]
	]
