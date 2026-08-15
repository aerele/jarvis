"""PP-1: backfill ``result_class`` on pre-existing Jarvis Agent Finding rows.

``result_class`` is a NEW ``reqd`` Select. Schema-sync adds the column nullable
(so ``migrate`` itself passes), but the moment any code path ``doc.save()``s a
legacy finding, Frappe would ``MandatoryError`` — so every existing row must be
stamped BEFORE enforcement matters. Same trap the A13 ``run_as_user`` backfill
handled.

Conservative classification (never silently upgrade a legacy row to a fact):

  * default ``observed_fact`` — a direct record fact;
  * ``derived_candidate`` (+ ``confirmation_status = unconfirmed``) when the row's
    ``title``/``detail_md`` carries a match/estimate/exposure marker OR a strong
    verb (saved/recovered/prevented/...) — the value may be a candidate, not a
    realised fact, so it is held below "fact".

No legacy row is backfilled to ``confirmed_outcome`` (reserved for the PP-5
ledger). Raw ``db.set_value`` (never ``doc.save()``) so the new set-once / reqd
controller guard is not tripped mid-migrate. Idempotent: only rows whose
``result_class`` is still NULL/empty are touched, so a re-run is a no-op.
"""

import frappe

FINDING = "Jarvis Agent Finding"

# Lower-cased substrings that mark a value as a match/estimate/exposure (a
# candidate) rather than a direct record read.
_CANDIDATE_MARKERS = (
	"match",
	"estimate",
	"estimated",
	"exposure",
	"likely",
	"probable",
	"candidate",
	"fuzzy",
	"approx",
	"potential",
	"suspected",
)

# Strong verbs (PP-1) — their presence means the row asserts an outcome it has
# no provenance for, so it is held at derived_candidate, never observed_fact.
_STRONG_VERBS = (
	"saved",
	"recovered",
	"prevented",
	"actually paid",
	"replaces",
)


def _looks_like_candidate(text: str) -> bool:
	t = (text or "").lower()
	return any(m in t for m in _CANDIDATE_MARKERS) or any(v in t for v in _STRONG_VERBS)


def execute():
	if not frappe.db.has_column(FINDING, "result_class"):
		return

	rows = frappe.get_all(
		FINDING,
		filters={"result_class": ["in", [None, ""]]},
		fields=["name", "title", "detail_md", "result_class"],
	)
	facts = candidates = 0
	for r in rows:
		if (r.result_class or "").strip():
			continue  # already classed (belt-and-braces against a partial deploy)
		blob = f"{r.title or ''}\n{r.detail_md or ''}"
		if _looks_like_candidate(blob):
			frappe.db.set_value(
				FINDING,
				r.name,
				{"result_class": "derived_candidate", "confirmation_status": "unconfirmed"},
				update_modified=False,
			)
			candidates += 1
		else:
			frappe.db.set_value(FINDING, r.name, "result_class", "observed_fact", update_modified=False)
			facts += 1

	frappe.db.commit()
	if rows:
		frappe.logger("jarvis").info(
			f"PP-1 result_class backfill: {facts} observed_fact, {candidates} derived_candidate "
			f"(of {len(rows)} legacy findings)"
		)
