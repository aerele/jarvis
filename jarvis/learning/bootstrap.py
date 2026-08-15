"""Pattern-learning bootstrap: after_migrate seeding + enablement preflight.

``after_migrate`` seeds ``Jarvis Pattern Detector State`` from the detector
registry (idempotent, best-effort: a seeding hiccup must never fail a migrate).
``enablement_preflight`` is the synchronous readiness check run at enablement
(plan section 3). It READS freely (get_all / db.sql) and writes ONLY
``Jarvis Pattern Detector State`` readiness flags: ``data_starved`` per
detector, plus ``not_applicable`` for the print-format detector when the
Access Log signal shows printing bypasses Frappe's print system (zero Print
rows despite submitted selling documents - custom print engines never write
Access Log, so more data will NOT fix it). It is NOT wired to any endpoint in
this wave (Wave C surfaces it).
"""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

DETECTOR_STATE = "Jarvis Pattern Detector State"
SETTINGS = "Jarvis Settings"

# Frappe does not backfill Single field defaults on migrate, so a pre-existing
# Jarvis Settings row reads null for the pattern_* config (blank window / 0 max /
# 0 budget on the card). after_migrate seeds these when currently null (plan
# sections 5.1 / 6.4). Kept in sync with learned_api._SETTINGS_DEFAULTS.
_SETTINGS_DEFAULTS = {
	"pattern_window_start": "01:00:00",
	"pattern_window_end": "05:00:00",
	"pattern_max_proposals_per_run": 10,
	"pattern_row_budget_per_night": 500000,
}

# information_schema.table_rows sample for the size estimate. table_rows is an
# unreliable InnoDB estimate (plan section 5.3), so it is reported as a HINT.
_SIZE_TABLES = (
	"Sales Invoice",
	"Sales Order",
	"Purchase Invoice",
	"Purchase Order",
	"Delivery Note",
	"Purchase Receipt",
	"Stock Entry",
	"Payment Entry",
	"Journal Entry",
	"GL Entry",
	"Stock Ledger Entry",
	"Access Log",
)

# Very rough throughput floor for unindexed GROUP-BY range scans across the
# Tier-1 detectors. Deliberately conservative; the coverage note tells the real
# story after the first night.
_ROWS_PER_MINUTE = 1_000_000

_CATCHALL_ITEM_GROUPS = (
	"general",
	"misc",
	"miscellaneous",
	"services",
	"default",
	"products",
	"all item groups",
)


# --------------------------------------------------------------------------- #
# after_migrate seeding
# --------------------------------------------------------------------------- #
def after_migrate() -> None:
	"""hooks.after_migrate entry. Best-effort seed of detector state rows + the
	pattern_* Single defaults (both idempotent; a seeding hiccup must never fail a
	migrate)."""
	try:
		_seed_detector_state()
	except Exception:
		frappe.log_error(
			title="jarvis pattern learning: after_migrate seeding failed",
			message=frappe.get_traceback(),
		)
	try:
		_seed_settings_defaults()
	except Exception:
		frappe.log_error(
			title="jarvis pattern learning: settings-default seeding failed",
			message=frappe.get_traceback(),
		)


def _seed_settings_defaults() -> None:
	"""Persist the pattern_* config defaults on the Jarvis Settings Single when
	they are currently null. Idempotent (only fills nulls, never clobbers an
	operator's value) and side-effect free (db.set_value/update_modified=False, so
	Jarvis Settings.on_update never fires).

	Reads via ``frappe.get_single().get`` (NOT ``db.get_single_value``): the latter
	casts an unset Time field to ``timedelta(0)`` and an unset Int to ``0`` - both
	miss the null test - whereas the loaded doc returns ``None`` for a genuinely
	unset field and the stored string for a real ``00:00:00`` midnight window (so
	a configured midnight window is never clobbered)."""
	s = frappe.get_single(SETTINGS)
	updates = {}
	for field, default in _SETTINGS_DEFAULTS.items():
		if s.get(field) in (None, ""):
			updates[field] = default
	if updates:
		# set_single_value (not the deprecated set_value on a Single); direct
		# write, no on_update.
		frappe.db.set_single_value(SETTINGS, updates, update_modified=False)
		frappe.db.commit()


def _seed_detector_state() -> None:
	specs = _tier1_specs()
	if not specs:
		return
	created = 0
	for spec in specs:
		detector_id = _spec_id(spec)
		if not detector_id:
			continue
		# Idempotent: only insert missing rows; never clobber an operator's
		# enabled flag or a stream watermark.
		if not frappe.db.exists(DETECTOR_STATE, detector_id):
			doc = frappe.get_doc(
				{
					"doctype": DETECTOR_STATE,
					"detector_id": detector_id,
					"enabled": 1,
				}
			)
			doc.insert(ignore_permissions=True)
			created += 1
	if created:
		frappe.db.commit()


# --------------------------------------------------------------------------- #
# enablement preflight (plan section 3) - read-only except data_starved
# --------------------------------------------------------------------------- #
def enablement_preflight() -> dict:
	"""Synchronous readiness preflight. Returns a dict of signals; never raises
	(each probe degrades to a note on failure). NOT wired to an endpoint here."""
	access_log_signal = _safe(_access_log_signal, "access_log_signal")
	print_signal = _safe(lambda: _apply_print_signal(access_log_signal), "print_signal")
	return {
		"site_size": _safe(_site_size_estimate, "site_size"),
		"access_log_signal": access_log_signal,
		"print_signal": print_signal,
		"detector_readiness": _safe(_detector_readiness, "detector_readiness"),
		"schema_coverage": _safe(_custom_schema_coverage, "schema_coverage"),
		"item_group_hygiene": _safe(_item_group_hygiene, "item_group_hygiene"),
	}


def _site_size_estimate() -> dict:
	from jarvis.learning.compat import db_backend

	if db_backend() != "mariadb":
		return {"supported": False, "note": "size estimate is MariaDB-only"}
	tables = tuple("tab" + d for d in _SIZE_TABLES)
	rows = frappe.db.sql(
		"""
		select table_name, table_rows
		from information_schema.tables
		where table_schema = %(db)s and table_name in %(tables)s
		""",
		{"db": frappe.conf.db_name, "tables": tables},
		as_dict=True,
	)
	per_table = {r.table_name: int(r.table_rows or 0) for r in rows}
	total = sum(per_table.values())
	projected_minutes = max(1, round(total / _ROWS_PER_MINUTE))
	return {
		"supported": True,
		"estimated_rows": total,
		"per_table": per_table,
		"projected_first_night_minutes": projected_minutes,
		"note": "table_rows is an unreliable InnoDB estimate; a hint, never a budget.",
	}


def _access_log_signal() -> dict:
	retention_days = None
	try:
		ls = frappe.get_single("Log Settings")
		for row in ls.get("logs_to_clear") or []:
			if row.get("ref_doctype") == "Access Log":
				retention_days = row.get("days")
				break
	except Exception:
		retention_days = None

	if not frappe.db.exists("DocType", "Access Log"):
		return {
			"supported": False,
			"retention_days": retention_days,
			"note": "Access Log doctype not present.",
		}
	total = frappe.db.count("Access Log")
	print_rows = frappe.db.count("Access Log", {"method": "Print"})
	no_print_signal = total > 0 and print_rows == 0
	return {
		"supported": True,
		"retention_days": retention_days,  # None = not auto-cleared (unbounded history)
		"access_log_rows": total,
		"print_rows": print_rows,
		"no_print_signal": no_print_signal,
		"note": (
			"Zero Print rows despite activity: printing likely bypasses Frappe's print "
			"system (print-format detectors would be not_applicable, not data_starved)."
			if no_print_signal
			else None
		),
	}


_NOT_APPLICABLE_PREFIX = "not_applicable:"


def _apply_print_signal(signal: dict) -> dict:
	"""Wire the Access-Log Print signal into the print-format detector's state
	(plan section 3): zero Print rows while submitted selling documents exist
	means printing bypasses Frappe's print system entirely (Access Log Print
	rows are written ONLY by frappe printview), so the detector can NEVER
	produce - Detector State.not_applicable, distinct from data_starved. A
	positive Print signal clears a previously set flag. No submitted documents
	yet is a data question, not an applicability one: leave the flag alone."""
	from jarvis.learning.snapshots import PRINT_FORMAT_DETECTOR_ID

	if not isinstance(signal, dict) or not signal.get("supported"):
		return {"applied": False, "note": "no Access Log signal to act on"}
	if int(signal.get("print_rows") or 0) > 0:
		_set_not_applicable(PRINT_FORMAT_DETECTOR_ID, False, None)
		return {"applied": True, "detector_id": PRINT_FORMAT_DETECTOR_ID, "not_applicable": False}
	if not _has_submitted_print_targets():
		return {
			"applied": False,
			"note": "no submitted selling documents yet - data_starved territory, not applicability",
		}
	reason = (
		f"{_NOT_APPLICABLE_PREFIX} zero Print rows in Access Log despite submitted selling "
		"documents - printing bypasses Frappe's print system (custom print engine), so "
		"print-format patterns can never be observed here."
	)
	_set_not_applicable(PRINT_FORMAT_DETECTOR_ID, True, reason)
	return {
		"applied": True,
		"detector_id": PRINT_FORMAT_DETECTOR_ID,
		"not_applicable": True,
		"reason": reason,
	}


def _has_submitted_print_targets() -> bool:
	"""Any submitted row in the selling doctypes the print detector resolves
	against (bounded exists probes, never counts)."""
	try:
		from jarvis.learning.detectors.selling import PRINT_PARTY_SQL

		doctypes = list(PRINT_PARTY_SQL)
	except Exception:
		doctypes = ["Sales Invoice", "Sales Order", "Delivery Note", "Quotation"]
	for doctype in doctypes:
		try:
			if frappe.db.exists(doctype, {"docstatus": 1}):
				return True
		except Exception:
			continue
	return False


def _set_not_applicable(detector_id: str, flag: bool, reason: str | None) -> None:
	"""Persist the applicability verdict on Detector State (creates the row if
	the after_migrate seed has not run). The reason lands in last_error with a
	distinct prefix (the doctype has no dedicated reason field); clearing only
	wipes a reason THIS preflight wrote, never a real engine error."""
	try:
		if frappe.db.exists(DETECTOR_STATE, detector_id):
			update = {"not_applicable": 1 if flag else 0}
			if flag:
				update["last_error"] = reason
			else:
				last_error = frappe.db.get_value(DETECTOR_STATE, detector_id, "last_error") or ""
				if last_error.startswith(_NOT_APPLICABLE_PREFIX):
					update["last_error"] = None
			frappe.db.set_value(DETECTOR_STATE, detector_id, update, update_modified=False)
		else:
			frappe.get_doc(
				{
					"doctype": DETECTOR_STATE,
					"detector_id": detector_id,
					"enabled": 1,
					"not_applicable": 1 if flag else 0,
					"last_error": reason if flag else None,
				}
			).insert(ignore_permissions=True)
	except Exception:
		pass


def _detector_readiness() -> dict:
	specs = _tier1_specs()
	if not specs:
		return {"supported": False, "note": "detector registry not available yet"}
	ready, starved = [], []
	for spec in specs:
		detector_id = _spec_id(spec)
		doctype = spec.get("doctype") if isinstance(spec, dict) else None
		if not (detector_id and doctype):
			continue
		gates = (spec.get("gates") or {}) if isinstance(spec, dict) else {}
		n_min = int(gates.get("n_min", 20) or 20)
		window_months = int(spec.get("window_months", 18) or 18)
		count = _readiness_count(spec, doctype, window_months)
		is_starved = count is not None and count < n_min
		_set_data_starved(detector_id, is_starved)
		entry = {"detector_id": detector_id, "doctype": doctype, "count": count, "n_min": n_min}
		(starved if is_starved else ready).append(entry)
	return {
		"supported": True,
		"total": len(ready) + len(starved),
		"ready_count": len(ready),
		"ready": ready,
		"data_starved": starved,
		"note": (
			f"{len(ready)} of {len(ready) + len(starved)} detectors have enough data; "
			"data-starved detectors will clear as the site accumulates transactions."
		),
	}


def _custom_schema_coverage() -> dict:
	read_doctypes = set()
	for spec in _tier1_specs():
		if not isinstance(spec, dict):
			continue
		if spec.get("doctype"):
			read_doctypes.add(spec["doctype"])
		for guard in spec.get("field_guards") or []:
			if isinstance(guard, (list, tuple)) and guard:
				read_doctypes.add(guard[0])
	try:
		custom_doctypes = frappe.get_all("DocType", filters={"custom": 1, "istable": 0}, pluck="name")
	except Exception:
		custom_doctypes = []
	invisible = [d for d in custom_doctypes if d not in read_doctypes]
	try:
		custom_fields = frappe.db.count("Custom Field")
	except Exception:
		custom_fields = 0
	return {
		"custom_doctypes": len(custom_doctypes),
		"invisible_custom_doctypes": len(invisible),
		"invisible_sample": invisible[:50],
		"custom_fields": custom_fields,
		"note": (
			f"{len(invisible)} custom doctypes are currently invisible to pattern learning "
			"(honest ceiling for heavily-customized sites)."
			if invisible
			else None
		),
	}


def _item_group_hygiene() -> dict:
	if not frappe.db.exists("DocType", "Item"):
		return {"supported": False, "note": "Item doctype not present."}
	total = frappe.db.count("Item")
	if not total:
		return {"supported": True, "warn": False, "total_items": 0}
	rows = frappe.db.sql(
		"""
		select item_group, count(*) c
		from `tabItem`
		where ifnull(item_group, '') != ''
		group by item_group
		order by c desc
		limit 5
		""",
		as_dict=True,
	)
	top = rows[0] if rows else None
	warn = False
	note = None
	if top:
		share = int(top.c) / total
		if (top.item_group or "").strip().lower() in _CATCHALL_ITEM_GROUPS and share >= 0.4:
			warn = True
			note = (
				f"Catch-all item group '{top.item_group}' holds {share * 100:.0f}% of items; "
				"item-group-keyed patterns may be unreliable."
			)
	return {
		"supported": True,
		"warn": warn,
		"note": note,
		"total_items": total,
		"top_groups": [{"item_group": r.item_group, "count": int(r.c)} for r in rows],
	}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _readiness_count(spec, doctype: str, window_months: int):
	"""Rows available to a detector, for the data-starved floor. Most detectors
	count recent rows in their own ``doctype``; the config naming-series detector
	is special: its ``doctype`` is the meta ``DocType`` (the antecedent is a
	DocType name, ``antecedent_kind == 'doctype'``), so counting recent DocType
	creations measures the wrong table (~0 on a stable site) and permanently
	misreports data_starved. It reads how many documents the site has actually
	NUMBERED via naming series instead (``tabSeries``)."""
	kind = spec.get("antecedent_kind") if isinstance(spec, dict) else None
	if kind == "doctype":
		return _naming_series_doc_count()
	return _count_recent(doctype, window_months)


def _naming_series_doc_count():
	"""Total documents numbered through naming series (``tabSeries`` holds one row
	per series prefix with its current counter; the sum is the numbered-document
	count). Window-independent: naming series accrue over the life of the site.
	Returns None on failure so readiness degrades to "not starved"."""
	try:
		row = frappe.db.sql("select coalesce(sum(`current`), 0) from `tabSeries`")
		return int(row[0][0] or 0) if row else 0
	except Exception:
		return None


def _count_recent(doctype: str, window_months: int):
	"""Cheap COUNT of rows created within the window (submitted only when the
	doctype is submittable). A readiness floor, not a unit count."""
	try:
		if not frappe.db.exists("DocType", doctype):
			return None
		cutoff = str(get_datetime(add_to_date(now_datetime(), months=-window_months)).date())
		filters = {"creation": [">=", cutoff]}
		try:
			if frappe.get_meta(doctype).is_submittable:
				filters["docstatus"] = 1
		except Exception:
			pass
		return frappe.db.count(doctype, filters)
	except Exception:
		return None


def _set_data_starved(detector_id: str, is_starved: bool) -> None:
	"""The ONLY write the preflight makes. Idempotent; creates the state row if
	the after_migrate seed has not run."""
	if not detector_id:
		return
	try:
		if frappe.db.exists(DETECTOR_STATE, detector_id):
			frappe.db.set_value(
				DETECTOR_STATE,
				detector_id,
				{"data_starved": 1 if is_starved else 0},
				update_modified=False,
			)
		else:
			frappe.get_doc(
				{
					"doctype": DETECTOR_STATE,
					"detector_id": detector_id,
					"enabled": 1,
					"data_starved": 1 if is_starved else 0,
				}
			).insert(ignore_permissions=True)
	except Exception:
		pass


def _tier1_specs() -> list:
	try:
		from jarvis.learning import registry
	except Exception:
		return []
	return list(getattr(registry, "TIER1_DETECTORS", []) or [])


def _spec_id(spec):
	if isinstance(spec, dict):
		return spec.get("id")
	return getattr(spec, "id", None)


def _safe(fn, label: str) -> dict:
	try:
		return fn()
	except Exception:
		frappe.log_error(
			title=f"jarvis pattern learning: preflight {label} failed",
			message=frappe.get_traceback(),
		)
		return {"supported": False, "error": True}
