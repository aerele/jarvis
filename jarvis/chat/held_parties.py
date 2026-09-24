"""Party identity for held File Box writes (the records a held create/update writes).

- ``items_of(tool, args)``: one ``{doctype, values, name, op}`` per record written;
- ``item_key(item)``: the per-item dedup key, scoped by doctype: the normalised GSTIN
  (``gstin``, else ERPNext's ``tax_id``), else the normalised party name, else a
  canonical args hash (an update keys on its target + a hash of its changes; an
  Address on its party, type and spot, as a sheet keys it);
- ``find_existing(...)``: records that already look like the party, read as the
  CURRENT user (callers impersonate the row's ``exec_user``)."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

import frappe

CREATE_TOOLS = frozenset({"create_doc", "create_docs"})
MAX_CANDIDATES = 5
_LIKE_SCAN = 50
_NOT_GSTIN = re.compile(r"[^0-9A-Z]")
_NOT_WORD = re.compile(r"[\W_]+")


def items_of(tool: str, args) -> list[dict]:
	"""The records ``tool(args)`` writes; ``[]`` for any other tool or a malformed call."""
	if not isinstance(args, dict):
		return []
	if tool in CREATE_TOOLS:
		docs = args.get("docs")
		if tool == "create_docs" or isinstance(docs, list):
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
	text = unicodedata.normalize("NFKC", str(value or "")).casefold()
	return " ".join(_NOT_WORD.sub(" ", text).split())


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
	if item["op"] == "update":
		# Two different changes to one record are two decisions, never one row.
		return f"doc:{item['doctype']}:{item['name']}:{_digest(item['values'])}"
	if item["doctype"] == "Address":
		from jarvis.chat import held_sheets  # it imports this module

		return held_sheets.identities(item)[0]
	party = party_of(item)
	if party["gstin"]:
		return f"gstin:{item['doctype']}:{party['gstin']}"
	if norm_name(party["title"]):
		return f"name:{item['doctype']}:{norm_name(party['title'])}"
	return "args:" + _digest({"doctype": item["doctype"], "values": item["values"]})


def is_party_key(key: str) -> bool:
	return key.startswith(("gstin:", "name:"))


def find_existing(
	doctype: str, *, title: str = "", gstin: str = "", limit: int = MAX_CANDIDATES
) -> list[dict]:
	"""Existing ``doctype`` records matching the GSTIN or the normalised name, as
	``[{name, title, gstin}]``; permission-scoped to the current user. A name match
	carrying a different tax id is another registration, not this party."""
	meta = _meta(doctype)
	if not meta or meta.istable or meta.issingle:
		return []
	field = title_field(meta)
	tax = tax_field(meta)
	fields = ["name", *([field] if field else []), *([tax] if tax else [])]
	found: dict = {}

	def _scan(filters, cap):
		try:
			return frappe.get_list(doctype, filters=filters, fields=fields, limit_page_length=cap)
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
