"""Buying-domain detector SQL + postprocess (plan section 4.2, Buying).

Line-level detectors (supplier item-group, supplier stockness) aggregate the
Purchase Order Item child rows up to one row per PARENT PO before the executor
ever sees them - the child-row-inflation trap (60 lines from 6 POs) can never
satisfy an n_min gate because n counts distinct POs. Supplier tax-template and
PI update-stock are header-level. Tier-2 adds
buy-supplier-payment-terms-realized (S5: realized PI terms vs the Supplier
master, proposed only on an unset/divergent master).
"""

from __future__ import annotations

# Hard per-detector row cap (plan §4.3 fix 7): the header/unit-grain SQL shapes
# return one row per parent, so a large site could materialize the whole window
# in Python. A fixed LIMIT is a curated OOM backstop (never user input); the
# nightly row budget pauses across detectors. Sized far above any single
# detector's real unit count on a normal site; true antecedent-grain aggregation
# with per-unit spread/burst preservation is a Phase 2 redesign.
HARD_ROW_LIMIT = 200000

# --- S1: supplier -> dominant item group (unit = distinct PO) -----------------
# A PO resolves to its single item group when all its lines share one, else the
# sentinel '__mixed__' (the executor ignores a mixed mode).
SUPPLIER_ITEMGROUP_SQL = f"""
SELECT poi.parent AS unit_id,
       po.supplier AS antecedent,
       CASE WHEN COUNT(DISTINCT poi.item_group) = 1
            THEN MAX(poi.item_group) ELSE '__mixed__' END AS consequent,
       po.company AS company,
       MIN(po.transaction_date) AS day,
       MIN(po.creation) AS created
FROM `tabPurchase Order Item` poi
JOIN `tabPurchase Order` po ON po.name = poi.parent
WHERE po.docstatus = 1
  AND po.company = %(company)s
  AND po.transaction_date >= %(window_start)s
  AND poi.item_group IS NOT NULL AND poi.item_group != ''
GROUP BY poi.parent, po.supplier, po.company
LIMIT {HARD_ROW_LIMIT}
"""

# --- S2: supplier -> non-stock-only (unit = distinct PO) ----------------------
# A PO is 'non_stock' only when EVERY line is non-stock (MAX(is_stock_item)=0).
SUPPLIER_STOCKNESS_SQL = f"""
SELECT poi.parent AS unit_id,
       po.supplier AS antecedent,
       CASE WHEN MAX(it.is_stock_item) = 0 THEN 'non_stock' ELSE 'has_stock' END AS consequent,
       po.company AS company,
       MIN(po.transaction_date) AS day,
       MIN(po.creation) AS created
FROM `tabPurchase Order Item` poi
JOIN `tabPurchase Order` po ON po.name = poi.parent
JOIN `tabItem` it ON it.name = poi.item_code
WHERE po.docstatus = 1
  AND po.company = %(company)s
  AND po.transaction_date >= %(window_start)s
GROUP BY poi.parent, po.supplier, po.company
LIMIT {HARD_ROW_LIMIT}
"""

# --- S1: supplier -> purchase tax template (unit = distinct PO) ---------------
SUPPLIER_TAX_TEMPLATE_SQL = f"""
SELECT po.name AS unit_id,
       po.supplier AS antecedent,
       po.taxes_and_charges AS consequent,
       po.company AS company,
       po.transaction_date AS day,
       po.creation AS created
FROM `tabPurchase Order` po
WHERE po.docstatus = 1
  AND po.company = %(company)s
  AND po.transaction_date >= %(window_start)s
  AND po.taxes_and_charges IS NOT NULL AND po.taxes_and_charges != ''
LIMIT {HARD_ROW_LIMIT}
"""

# --- S2+S5: org books Purchase Invoices with update_stock (unit = distinct PI)-
# antecedent 'org'; the executor suppresses the vanilla update_stock=0 default
# via org_default_consequent, and the go-live-import burst is caught by the
# spread gate (a same-day import batch spans <5 calendar days).
PI_UPDATE_STOCK_SQL = f"""
SELECT pi.name AS unit_id,
       'org' AS antecedent,
       CAST(pi.update_stock AS CHAR) AS consequent,
       pi.company AS company,
       pi.posting_date AS day,
       pi.creation AS created
FROM `tabPurchase Invoice` pi
WHERE pi.docstatus = 1
  AND pi.company = %(company)s
  AND pi.posting_date >= %(window_start)s
LIMIT {HARD_ROW_LIMIT}
"""

# --- S5 (Tier-2): realized supplier terms vs the Supplier master ---------------
# Unit = distinct Purchase Invoice (the realized document); the postprocess
# proposes only on an unset or divergent Supplier.payment_terms master.
SUPPLIER_PAYMENT_TERMS_SQL = f"""
SELECT pi.name AS unit_id,
       pi.supplier AS antecedent,
       pi.payment_terms_template AS consequent,
       pi.company AS company,
       pi.posting_date AS day,
       pi.creation AS created
FROM `tabPurchase Invoice` pi
WHERE pi.docstatus = 1
  AND pi.company = %(company)s
  AND pi.posting_date >= %(window_start)s
  AND pi.supplier IS NOT NULL AND pi.supplier != ''
  AND pi.payment_terms_template IS NOT NULL AND pi.payment_terms_template != ''
LIMIT {HARD_ROW_LIMIT}
"""

SUPPLIER_TERMS_MASTER_SQL = (
	"SELECT s.name AS name, s.payment_terms AS payment_terms FROM `tabSupplier` s WHERE s.name IN %(names)s"
)


def postprocess_supplier_payment_terms(rows, spec, company, patterndb, params):
	"""S5 (Tier-2, plan section 4.2): a supplier's realized terms habit is
	proposed only when the Supplier master is unset or divergent (a matching
	master is config working as intended and stays silent)."""
	from jarvis.learning.executor import reduce_units

	raws = reduce_units(rows, spec, patterndb)
	if not raws:
		return []

	names = sorted({r.get("antecedent_value") for r in raws if r.get("antecedent_value")})
	masters: dict = {}
	if names:
		try:
			for m in patterndb.sql_select(SUPPLIER_TERMS_MASTER_SQL, {"names": names}) or []:
				masters[m.get("name")] = m.get("payment_terms")
		except Exception:
			masters = {}

	out = []
	for raw in raws:
		master = masters.get(raw.get("antecedent_value"))
		if master and str(master) == str(raw.get("consequent_value")):
			continue  # master already encodes the habit - nothing to learn
		clause = (
			f'the Supplier master default is "{master}"'
			if master
			else "the Supplier master has no default payment terms"
		)
		raw["skill_bullet_vars"] = {
			"supplier": raw.get("antecedent_value"),
			"terms": raw.get("consequent_value"),
			"master_clause": clause,
		}
		evidence = raw.get("evidence")
		if isinstance(evidence, dict):
			evidence["master_payment_terms"] = master
		out.append(raw)
	return out
