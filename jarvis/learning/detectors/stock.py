"""Stock-domain detector SQL + postprocess (plan section 4.2, Stock)."""

from __future__ import annotations

# Hard per-detector row cap (plan §4.3 fix 7): a curated OOM backstop for the
# unit-grain SQL shapes; the nightly row budget pauses across detectors.
HARD_ROW_LIMIT = 200000

# --- single-plant guard: distinct shipping warehouses used by the company -----
WAREHOUSE_COUNT_SQL = """
SELECT COUNT(DISTINCT sii.warehouse) AS wh
FROM `tabSales Invoice Item` sii
JOIN `tabSales Invoice` si ON si.name = sii.parent
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
  AND sii.warehouse IS NOT NULL AND sii.warehouse != ''
"""

# --- S5: item group -> warehouse (unit = distinct (invoice, item group)) ------
ITEMGROUP_WAREHOUSE_SQL = f"""
SELECT CONCAT(sii.parent, '::', sii.item_group) AS unit_id,
       sii.item_group AS antecedent,
       CASE WHEN COUNT(DISTINCT sii.warehouse) = 1
            THEN MAX(sii.warehouse) ELSE '__mixed__' END AS consequent,
       si.company AS company,
       MIN(si.posting_date) AS day,
       MIN(si.creation) AS created
FROM `tabSales Invoice Item` sii
JOIN `tabSales Invoice` si ON si.name = sii.parent
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
  AND sii.item_group IS NOT NULL AND sii.item_group != ''
  AND sii.warehouse IS NOT NULL AND sii.warehouse != ''
GROUP BY sii.parent, sii.item_group, si.company
LIMIT {HARD_ROW_LIMIT}
"""

# --- S1: stock-entry purpose -> dominant warehouse route (unit = distinct SE) -
STOCK_ENTRY_ROUTE_SQL = f"""
SELECT se.name AS unit_id,
       se.purpose AS antecedent,
       CASE WHEN COUNT(DISTINCT CONCAT(COALESCE(sed.s_warehouse, ''), '>', COALESCE(sed.t_warehouse, ''))) = 1
            THEN MAX(CONCAT(COALESCE(sed.s_warehouse, ''), ' to ', COALESCE(sed.t_warehouse, '')))
            ELSE '__mixed__' END AS consequent,
       MIN(se.company) AS company,
       MIN(se.posting_date) AS day,
       MIN(se.creation) AS created
FROM `tabStock Entry Detail` sed
JOIN `tabStock Entry` se ON se.name = sed.parent
WHERE se.docstatus = 1
  AND se.company = %(company)s
  AND se.posting_date >= %(window_start)s
  AND se.purpose IS NOT NULL AND se.purpose != ''
GROUP BY se.name, se.purpose
LIMIT {HARD_ROW_LIMIT}
"""


# --- Tier-2: (item_group, cost_center) -> warehouse for multi-plant companies -
# Unit = distinct (invoice, item group, cost center); the antecedent is the
# composite "group :: cost center" pair, so the generic reduce gates each pair
# independently. The postprocess splits the pair back into template vars.
_PAIR_SEP = " :: "

ITEMGROUP_WAREHOUSE_DIMENSIONED_SQL = f"""
SELECT CONCAT(sii.parent, '::', sii.item_group, '::', sii.cost_center) AS unit_id,
       CONCAT(sii.item_group, '{_PAIR_SEP}', sii.cost_center) AS antecedent,
       CASE WHEN COUNT(DISTINCT sii.warehouse) = 1
            THEN MAX(sii.warehouse) ELSE '__mixed__' END AS consequent,
       si.company AS company,
       MIN(si.posting_date) AS day,
       MIN(si.creation) AS created
FROM `tabSales Invoice Item` sii
JOIN `tabSales Invoice` si ON si.name = sii.parent
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
  AND sii.item_group IS NOT NULL AND sii.item_group != ''
  AND sii.warehouse IS NOT NULL AND sii.warehouse != ''
  AND sii.cost_center IS NOT NULL AND sii.cost_center != ''
GROUP BY sii.parent, sii.item_group, sii.cost_center, si.company
LIMIT {HARD_ROW_LIMIT}
"""

# Delivery Note rows carry the same habit (plan section 4.2: SI/DN item rows);
# merged by the postprocess when the DN columns exist on this site.
ITEMGROUP_WAREHOUSE_DIMENSIONED_DN_SQL = f"""
SELECT CONCAT(dni.parent, '::', dni.item_group, '::', dni.cost_center) AS unit_id,
       CONCAT(dni.item_group, '{_PAIR_SEP}', dni.cost_center) AS antecedent,
       CASE WHEN COUNT(DISTINCT dni.warehouse) = 1
            THEN MAX(dni.warehouse) ELSE '__mixed__' END AS consequent,
       dn.company AS company,
       MIN(dn.posting_date) AS day,
       MIN(dn.creation) AS created
FROM `tabDelivery Note Item` dni
JOIN `tabDelivery Note` dn ON dn.name = dni.parent
WHERE dn.docstatus = 1
  AND dn.company = %(company)s
  AND dn.posting_date >= %(window_start)s
  AND dni.item_group IS NOT NULL AND dni.item_group != ''
  AND dni.warehouse IS NOT NULL AND dni.warehouse != ''
  AND dni.cost_center IS NOT NULL AND dni.cost_center != ''
GROUP BY dni.parent, dni.item_group, dni.cost_center, dn.company
LIMIT {HARD_ROW_LIMIT}
"""

# --- S2: item group -> transacted UOM != stock UOM (unit = (invoice, group)) --
# '__stock_uom__' marks units transacting in the item's own stock UOM (the
# vanilla default, suppressed via org_default_consequent on the spec);
# '__mixed__' units never become a mode.
UOM_CONVERSION_SQL = f"""
SELECT CONCAT(sii.parent, '::', sii.item_group) AS unit_id,
       sii.item_group AS antecedent,
       CASE WHEN COUNT(DISTINCT sii.uom) > 1 THEN '__mixed__'
            WHEN COUNT(DISTINCT it.stock_uom) = 1 AND MAX(sii.uom) = MAX(it.stock_uom)
            THEN '__stock_uom__'
            ELSE MAX(sii.uom) END AS consequent,
       si.company AS company,
       MIN(si.posting_date) AS day,
       MIN(si.creation) AS created
FROM `tabSales Invoice Item` sii
JOIN `tabSales Invoice` si ON si.name = sii.parent
JOIN `tabItem` it ON it.name = sii.item_code
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
  AND sii.item_group IS NOT NULL AND sii.item_group != ''
  AND sii.uom IS NOT NULL AND sii.uom != ''
GROUP BY sii.parent, sii.item_group, si.company
LIMIT {HARD_ROW_LIMIT}
"""

# --- S2: item group -> batch/serial tracking in practice (unit = Item) --------
# Org-wide (Item has no company); the postprocess attaches realized fill rates.
BATCH_SERIAL_USAGE_SQL = f"""
SELECT it.name AS unit_id,
       it.item_group AS antecedent,
       CASE WHEN it.has_batch_no = 1 AND it.has_serial_no = 1 THEN 'batch-and-serial-tracked'
            WHEN it.has_batch_no = 1 THEN 'batch-tracked'
            WHEN it.has_serial_no = 1 THEN 'serial-tracked'
            ELSE '__untracked__' END AS consequent,
       DATE(it.creation) AS day,
       it.creation AS created
FROM `tabItem` it
WHERE it.disabled = 0
  AND it.is_stock_item = 1
  AND it.item_group IS NOT NULL AND it.item_group != ''
LIMIT {HARD_ROW_LIMIT}
"""

# Realized fill rates per group from the stock ledger (evidence only; cancelled
# rows are a tolerable rounding error for an evidence line, and is_cancelled is
# not probed so the statement stays static).
BATCH_SERIAL_FILL_SQL = """
SELECT it.item_group AS item_group,
       SUM(CASE WHEN sle.batch_no IS NOT NULL AND sle.batch_no != '' THEN 1 ELSE 0 END) AS batch_filled,
       SUM(CASE WHEN sle.serial_no IS NOT NULL AND sle.serial_no != '' THEN 1 ELSE 0 END) AS serial_filled,
       COUNT(*) AS total
FROM `tabStock Ledger Entry` sle
JOIN `tabItem` it ON it.name = sle.item_code
WHERE sle.creation >= %(window_start)s
GROUP BY it.item_group
LIMIT 10000
"""

# --- S5: realized Work Order BOM vs the flagged default BOM (unit = WO) -------
DEFAULT_BOM_USAGE_SQL = f"""
SELECT wo.name AS unit_id,
       wo.production_item AS antecedent,
       wo.bom_no AS consequent,
       wo.company AS company,
       DATE(wo.creation) AS day,
       wo.creation AS created
FROM `tabWork Order` wo
WHERE wo.docstatus = 1
  AND wo.company = %(company)s
  AND wo.creation >= %(window_start)s
  AND wo.bom_no IS NOT NULL AND wo.bom_no != ''
  AND wo.production_item IS NOT NULL AND wo.production_item != ''
LIMIT {HARD_ROW_LIMIT}
"""

DEFAULT_BOM_SQL = (
	"SELECT b.item AS item, b.name AS bom FROM `tabBOM` b "
	"WHERE b.is_default = 1 AND b.is_active = 1 AND b.docstatus = 1 "
	"AND b.item IN %(items)s"
)


def postprocess_itemgroup_warehouse(rows, spec, company, patterndb, params):
	"""Single-plant guard (plan section 4.2): item-group -> warehouse is only
	trustworthy for a company with ONE warehouse cluster. A company shipping
	from more than ``single_plant_max_warehouses`` distinct warehouses is
	multi-plant and is skipped with a coverage note (Tier-2 dimensioned
	variant). Otherwise fall through to the generic reduce, where the variance
	gate additionally kills a volume-skewed 'everything ships from one
	warehouse' distribution."""
	from jarvis.learning.executor import DetectorSkip, reduce_units, single_value

	max_wh = int(spec.get("single_plant_max_warehouses", 3))
	n_wh = single_value(patterndb, WAREHOUSE_COUNT_SQL, params) or 0
	if int(n_wh) > max_wh:
		raise DetectorSkip(
			f"multi-plant: {int(n_wh)} shipping warehouses; needs cost-center dimensioning (Tier-2 variant)"
		)
	return reduce_units(rows, spec, patterndb)


def postprocess_itemgroup_warehouse_dimensioned(rows, spec, company, patterndb, params):
	"""Tier-2 multi-plant variant (plan sections 4.2, 4.4): (item_group,
	cost_center) -> warehouse, ONLY for companies the Tier-1 single-plant guard
	skips. The guard here is the exact mirror-inverse over the SAME warehouse
	count, so for any company exactly one of the two variants runs and
	iter_work_units can safely schedule both."""
	from jarvis.learning import compat
	from jarvis.learning.executor import DetectorSkip, reduce_units, single_value

	max_wh = int(spec.get("single_plant_max_warehouses", 3))
	n_wh = int(single_value(patterndb, WAREHOUSE_COUNT_SQL, params) or 0)
	if n_wh <= max_wh:
		raise DetectorSkip(
			f"single-plant: {n_wh} shipping warehouses; covered by the Tier-1 "
			"stock-itemgroup-warehouse detector"
		)

	rows = list(rows or [])
	if all(compat.has_field("Delivery Note Item", f) for f in ("item_group", "warehouse", "cost_center")):
		try:
			rows += patterndb.timed_select(ITEMGROUP_WAREHOUSE_DIMENSIONED_DN_SQL, params) or []
		except Exception:
			pass  # DN rows are additive evidence; SI rows carry the pattern

	raws = reduce_units(rows, spec, patterndb)
	for raw in raws:
		parts = str(raw.get("antecedent_value") or "").split(_PAIR_SEP, 1)
		if len(parts) != 2:
			continue
		raw["skill_bullet_vars"] = {
			"item_group": parts[0],
			"cost_center": parts[1],
			"warehouse": raw.get("consequent_value"),
		}
		evidence = raw.get("evidence")
		if isinstance(evidence, dict):
			evidence["cost_center"] = parts[1]
	return raws


def postprocess_batch_serial_usage(rows, spec, company, patterndb, params):
	"""S2 (plan section 4.2): group tracked in practice via Item flags, with
	realized batch/serial fill rates from the stock ledger attached as
	evidence (best-effort; a site without the legacy ledger columns still
	proposes off the flags alone)."""
	from jarvis.learning import compat
	from jarvis.learning.executor import reduce_units

	raws = reduce_units(rows, spec, patterndb)
	if not raws:
		return []

	fill: dict = {}
	if compat.has_field("Stock Ledger Entry", "batch_no") and compat.has_field(
		"Stock Ledger Entry", "serial_no"
	):
		try:
			for r in patterndb.timed_select(BATCH_SERIAL_FILL_SQL, params) or []:
				total = int(r.get("total") or 0)
				if not total:
					continue
				fill[r.get("item_group")] = {
					"batch_fill_rate": round(int(r.get("batch_filled") or 0) / total, 4),
					"serial_fill_rate": round(int(r.get("serial_filled") or 0) / total, 4),
					"ledger_rows": total,
				}
		except Exception:
			fill = {}

	for raw in raws:
		group = raw.get("antecedent_value")
		raw["skill_bullet_vars"] = {
			"item_group": group,
			"tracking": str(raw.get("consequent_value") or "").replace("-", " "),
		}
		evidence = raw.get("evidence")
		if isinstance(evidence, dict) and group in fill:
			evidence.update(fill[group])
	return raws


def postprocess_default_bom_usage(rows, spec, company, patterndb, params):
	"""S5 (plan section 4.2): propose the realized Work Order BOM habit ONLY
	when it diverges from the item's flagged default BOM (a matching default is
	config working as intended, nothing to learn)."""
	from jarvis.learning.executor import reduce_units

	raws = reduce_units(rows, spec, patterndb)
	if not raws:
		return []

	items = sorted({r.get("antecedent_value") for r in raws if r.get("antecedent_value")})
	defaults: dict = {}
	if items:
		try:
			for r in patterndb.sql_select(DEFAULT_BOM_SQL, {"items": items}) or []:
				if r.get("item"):
					defaults[r["item"]] = r.get("bom")
		except Exception:
			defaults = {}

	out = []
	for raw in raws:
		item = raw.get("antecedent_value")
		default_bom = defaults.get(item)
		if default_bom and str(default_bom) == str(raw.get("consequent_value")):
			continue  # the flagged default already encodes the habit
		clause = (
			f'the flagged default BOM is "{default_bom}"'
			if default_bom
			else "no active default BOM is flagged"
		)
		raw["skill_bullet_vars"] = {
			"item": item,
			"bom": raw.get("consequent_value"),
			"master_clause": clause,
		}
		evidence = raw.get("evidence")
		if isinstance(evidence, dict):
			evidence["default_bom"] = default_bom
		out.append(raw)
	return out
