"""Run the ERP's OWN ``make_*`` mapper for a (source doc -> target doctype) pair.

When a record derives from another - a Sales Invoice from a Sales Order, a
Purchase Receipt from a Purchase Order - ERPNext already knows how to build it:
the form's "Create >" buttons call whitelisted mappers
(``make_sales_invoice(source_name)``) that return a fully-populated, UNSAVED
target doc. Those mappers carry things inference reliably misses - the
``so_detail`` / ``sales_order`` row links that keep the order's billed status
correct, billed-qty arithmetic, unit-price rows, advance allocation, payment
schedules, SO-level tax templates.

This module resolves the mapper and runs it, so the agent can draft from the
ERP's own answer instead of reconstructing one. It never inserts: the caller gets
values, and the normal create path (with its confirmation card) still applies.

TWO FACTS THAT SHAPE EVERYTHING HERE:

1. **Mappers are NOT side-effect free.** ``make_sales_invoice``'s postprocess
   reaches ``bundle.save()`` (stock_reservation_entry.py) and inserts a Serial
   and Batch Bundle when the order has serial/batch reservations;
   ``make_purchase_order`` calls ``doc.insert()`` outright. Frappe commits any
   normally-returning request, so a mapper run OUTSIDE a sandbox would persist
   those. Every call here runs inside ``preview_sandbox`` and is rolled back.
   This is the module's central invariant - do not "optimize" it away.
2. **Passing the three resolution filters does not mean a mapper is usable.**
   Several return None or a list rather than one doc (see ``mapped_values``). We
   therefore validate the RESULT rather than curating a static allowlist that
   would rot: anything that is not a single doc of the requested doctype is
   treated as "no mapper", and the caller falls back to its normal path.
"""

from __future__ import annotations

import frappe

from jarvis.tools._doc_actions import get_doc_actions
from jarvis.tools._preview_sandbox import preview_sandbox

# Mapper naming convention on the "Create >" buttons: make_* / create_*.
_MAPPER_PREFIXES = ("make_", "create_")

# Resolution is cached because get_doc_actions assembles the DocType's form JS
# and imports every candidate module - far too heavy to redo for each context
# key on every call. Same TTL as the schema cache, and busted by the same
# doc_events (get_schema.clear_cache_for), since a Client Script edit can change
# which methods the form references.
_MAPPER_TTL = 300


def mapper_cache_key(source_doctype: str) -> str:
	return f"jarvis_mappers:{source_doctype}"


def _mapper_map(source_doctype: str) -> dict:
	"""``{scrubbed_target: dotted_method}`` for ``source_doctype``'s OWN mappers.

	Three filters, each load-bearing and each validated against live meta:

	1. OWN mappers only - the dotted path must contain ``scrub(source_doctype)``
	   as a segment. ``get_doc_actions`` scrapes every method the form JS
	   references, including foreign ones: without this, Sales Order resolves
	   target "Sales Order" via ``quotation.mapper.make_sales_order``, which
	   expects a QUOTATION. Feeding it a Sales Order would be silently wrong.
	   (Mirrors ``_doc_actions._sort_key``'s own/foreign split.)
	2. Signature shape - the first parameter must be ``source_name``. Drops the
	   non-mappers sharing the prefix: ``make_raw_material_request(items, ...)``,
	   ``make_work_orders(items, ...)``, ``update_status(status, name)``.
	3. Naming convention - ``make_``/``create_`` prefix. The remainder is
	   compared SCRUBBED (not title-cased) so odd casing like ``BOM`` matches.

	Keyed by scrubbed target because that is what the method name yields; the
	lookup in ``resolve_mapper`` scrubs the caller's real doctype to match."""
	cache = frappe.cache()
	key = mapper_cache_key(source_doctype)
	hit = cache.get_value(key)
	if hit is not None:
		return hit
	out: dict = {}
	own = frappe.scrub(source_doctype)
	try:
		actions = get_doc_actions(source_doctype)
	except Exception:
		actions = []
	for action in actions:
		method = action.get("method") or ""
		if own not in method.split("."):
			continue
		args = action.get("args") or []
		if not args or args[0] != "source_name":
			continue
		tail = method.rsplit(".", 1)[-1]
		for prefix in _MAPPER_PREFIXES:
			if tail.startswith(prefix):
				# setdefault: first wins, and _doc_actions already sorts the
				# DocType's own methods ahead of foreign ones.
				out.setdefault(tail[len(prefix) :], method)
				break
	cache.set_value(key, out, expires_in_sec=_MAPPER_TTL)
	return out


def resolve_mapper(source_doctype: str, target_doctype: str) -> str | None:
	"""Dotted path of the mapper that builds ``target_doctype`` from
	``source_doctype``, or None when there isn't one."""
	if not source_doctype or not target_doctype:
		return None
	try:
		return _mapper_map(source_doctype).get(frappe.scrub(target_doctype))
	except Exception:
		# Resolution is an optimisation, never a correctness gate: a failure
		# means "no mapper", and the caller keeps its normal path.
		return None


def mapped_values(
	source_doctype: str,
	source_name: str,
	target_doctype: str,
) -> tuple[dict | None, str | None]:
	"""``(values, note)`` for ``target_doctype`` mapped from ``source_name``.

	``values`` is the mapped doc as a plain dict (child rows included), or None
	when there is no mapper, the caller may not read the source, or the mapper
	declined. ``note`` carries the ERP's own refusal when there is one - "Sales
	Order is not submitted", "nothing left to bill" - which is far more useful to
	the agent than silence, so it is surfaced rather than swallowed.

	Runs inside ``preview_sandbox``: mappers write (see module docstring).
	"""
	method = resolve_mapper(source_doctype, target_doctype)
	if not method:
		return None, None
	# The mapper runs as the chat user; check read on the SOURCE explicitly so a
	# refusal is a clean note rather than whatever the mapper throws mid-build.
	if not frappe.has_permission(source_doctype, ptype="read", doc=source_name):
		return None, f"no read permission on {source_doctype} {source_name}"
	try:
		fn = frappe.get_attr(method)
	except Exception:
		return None, None
	try:
		with preview_sandbox():
			doc = frappe.call(fn, source_name=source_name)
			# Passing the filters does not make a mapper usable. Verified
			# counter-examples: make_purchase_order returns None without
			# `selected_items` and a LIST when it works; make_maintenance_schedule
			# / make_maintenance_visit guard their whole body and implicitly
			# return None when one already exists. Validate the RESULT - anything
			# that is not one doc of the requested type is "no mapper", and the
			# caller falls back rather than crashing on a None.
			if getattr(doc, "doctype", None) != target_doctype:
				return None, None
			# Materialise INSIDE the sandbox; the rollback only touches the DB,
			# but the doc must be read before we leave the block.
			doc.apply_fieldlevel_read_permissions()
			values = doc.as_dict(no_default_fields=True)
	except frappe.PermissionError as e:
		return None, str(e) or f"no permission to map {target_doctype}"
	except Exception as e:
		# The ERP's own refusal (draft source, nothing to bill, ...). Best-effort
		# by contract: the caller must keep working without a mapping.
		return None, f"could not map {target_doctype} from {source_name}: {e}"
	return _strip(values), None


# Framework bookkeeping the agent must never copy into a draft.
_DROP_KEYS = {
	"name",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"idx",
	"parent",
	"parentfield",
	"parenttype",
	"doctype",
}


def _strip(values: dict) -> dict:
	"""Drop framework bookkeeping, recursively through child rows. ``as_dict``
	keeps them even with ``no_default_fields``, and a draft must not carry a
	``name``/``docstatus`` copied from a mapping."""
	if not isinstance(values, dict):
		return {}
	out = {}
	for k, v in values.items():
		if k in _DROP_KEYS:
			continue
		if isinstance(v, list):
			rows = [_strip(r) for r in v if isinstance(r, dict)]
			if rows:
				out[k] = rows
			continue
		if v in (None, ""):
			continue
		out[k] = v
	return out
