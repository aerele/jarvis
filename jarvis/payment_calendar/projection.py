"""Deterministic cash projection; no database, model or payment side effects."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


class ProjectionError(ValueError):
	def __init__(self, code="invalid_projection_source"):
		self.code = code
		super().__init__("A complete Payment Calendar projection is unavailable.")


def _date(value):
	try:
		if not isinstance(value, str) or len(value) != 10:
			raise ValueError
		result = date.fromisoformat(value)
		if result.isoformat() != value:
			raise ValueError
		return result
	except (TypeError, ValueError) as exc:
		raise ProjectionError("invalid_date") from exc


def _amount(value, precision):
	try:
		if value is None or isinstance(value, bool):
			raise InvalidOperation
		amount = Decimal(str(value))
		if not amount.is_finite():
			raise InvalidOperation
		normalized = amount.quantize(Decimal(1).scaleb(-precision))
		if normalized != amount:
			raise InvalidOperation
		return abs(normalized) if normalized.is_zero() else normalized
	except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
		raise ProjectionError("invalid_source_amount") from exc


def _source(source, anchor, end, precision):
	if source is None:
		return None, None
	try:
		if not isinstance(source["rows"], list) or not isinstance(source["complete_for_company"], bool):
			raise ProjectionError()
		groups = defaultdict(list)
		buckets = dict.fromkeys(
			("current_future", "overdue", "undated", "outside_horizon", "adjustments"), Decimal(0)
		)
		for row in source["rows"]:
			remaining, original = _amount(row["remaining"], precision), _amount(row["original"], precision)
			due = _date(row["due_date"]) if row["due_date"] is not None else None
			if remaining < 0:
				buckets["adjustments"] += remaining
			elif remaining > 0:
				bucket = (
					"undated"
					if due is None
					else "overdue"
					if due < anchor
					else "outside_horizon"
					if due > end
					else "current_future"
				)
				buckets[bucket] += remaining
			# Positive accounting adjustments are still amounts owed. Only negative
			# residuals are excluded from daily cash flows. Zero-original placeholders
			# do not make an empty date look settled.
			if due is not None and (
				remaining > 0 or (remaining == 0 and original > 0 and row["kind"] == "obligation")
			):
				groups[due].append((row, remaining, original))
		canonical = _amount(source["canonical_total"], precision)
		if sum(buckets.values(), Decimal(0)) != canonical:
			raise ProjectionError("reconciliation_mismatch")
		return groups, {"canonical_total": canonical, **buckets}
	except (KeyError, TypeError, AttributeError) as exc:
		raise ProjectionError() from exc


def _tallies(rows, prefix, precision):
	invoice_types = {"Purchase Invoice", "Sales Invoice"}
	return {
		f"{prefix}_remaining": str(_amount(sum((r[1] for r in rows), Decimal(0)), precision)),
		f"{prefix}_terms": sum(r[0]["voucher_type"] in invoice_types for r in rows),
		f"{prefix}_invoices": len(
			{
				(r[0]["voucher_type"], r[0]["voucher_no"])
				for r in rows
				if r[0]["voucher_type"] in invoice_types
			}
		),
		f"{prefix}_parties": len({(r[0]["party_type"], r[0]["party"]) for r in rows}),
		f"{prefix}_parties_due": len({(r[0]["party_type"], r[0]["party"]) for r in rows if r[1] > 0}),
		f"{prefix}_rows_due": sum(r[1] > 0 for r in rows),
	}


def _settlement(rows):
	if not rows:
		return "No dues"
	if all(r[1] == 0 for r in rows):
		return "Settled"
	if all(r[1] >= r[2] for r in rows):
		return "Unpaid"
	if any(row[0].get("settlement") == "Partially settled" for row in rows):
		return "Partially settled"
	return "Partially paid"


def _verified_settled_fx(source, precision):
	"""Waive only verified non-cash exclusions; never weaken other scope gates."""
	try:
		rows = source["excluded_rows"]
		note = source["notes"]["currency_residue_excluded"]
		if not isinstance(rows, list) or not rows or not isinstance(note, dict):
			return False
		if type(note.get("count")) is not int or note["count"] != len(rows):
			return False

		def identity(row):
			return row["voucher_type"], row["voucher_no"], row["party"]

		delivered = {identity(row) for row in source["rows"]}
		seen, total = set(), Decimal(0)
		for row in rows:
			key = identity(row)
			if (
				key in delivered
				or key in seen
				or row.get("kind") != "settled_fx"
				or row["voucher_type"] not in ("Purchase Invoice", "Sales Invoice")
				or _amount(row["account_remaining"], precision) != 0
			):
				return False
			remaining = _amount(row["remaining"], precision)
			if remaining == 0:
				return False
			seen.add(key)
			total += remaining
		return _amount(note["amount"], precision) == total
	except (KeyError, TypeError, AttributeError, ProjectionError):
		# Missing/old/unverified proof keeps the original unavailable state.
		return False


def project_snapshot(ap, ar, bank_total, floor, anchor, start, end, *, precision=2):
	"""Aggregate complete residuals; carry from the anchor even before visible start."""
	if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 9:
		raise ProjectionError("invalid_precision")
	anchor, start, end = _date(anchor), _date(start), _date(end)
	if start > end or end < anchor or (end - min(start, anchor)).days > 800:
		raise ProjectionError("invalid_range")
	payables, ap_buckets = _source(ap, anchor, end, precision)
	receivables, ar_buckets = _source(ar, anchor, end, precision)
	opening = _amount(bank_total, precision) if bank_total is not None else None
	minimum = _amount(floor, precision) if floor is not None else None
	if minimum is not None and minimum < 0:
		raise ProjectionError("invalid_floor")
	reasons, complete_sides = set(), {}
	for side, source in (("payables", ap), ("receivables", ar)):
		complete_sides[side] = source is not None and source["complete_for_company"]
		if source is None:
			reasons.add(f"{side}_unavailable")
		elif not source["complete_for_company"]:
			source_reasons = set(source.get("scope_reasons") or ["incomplete_obligations"])
			if source_reasons == {"currency_residue_excluded"} and _verified_settled_fx(source, precision):
				source_reasons.clear()
			reasons.update(source_reasons)
			complete_sides[side] = not source_reasons
	if opening is None:
		reasons.add("bank_unavailable")
	if minimum is None:
		reasons.add("floor_unconfigured")
	available = not reasons
	closing = opening
	days = {}
	day = min(start, anchor)
	while day <= end:
		ap_rows = payables.get(day, []) if payables is not None else []
		ar_rows = receivables.get(day, []) if receivables is not None else []
		outgoing = sum((r[1] for r in ap_rows), Decimal(0))
		incoming = sum((r[1] for r in ar_rows), Decimal(0))
		projected_opening = projected_closing = risk = None
		if available and day >= anchor:
			projected_opening = closing
			if day == anchor:
				outgoing += ap_buckets["overdue"] + ap_buckets["undated"]
			closing += incoming - outgoing
			projected_closing = closing
			risk = "red" if closing < 0 else "orange" if closing < minimum else "green"
		if day >= start:
			days[day.isoformat()] = {
				"date": day.isoformat(),
				**_tallies(ap_rows, "payable", precision),
				**_tallies(ar_rows, "receivable", precision),
				"settlement": _settlement(ap_rows) if ap is not None else "Unavailable",
				"projected_opening": str(_amount(projected_opening, precision))
				if projected_opening is not None
				else None,
				"projected_closing": str(_amount(projected_closing, precision))
				if projected_closing is not None
				else None,
				"risk": risk,
			}
			for prefix, source in (("payable_", ap), ("receivable_", ar)):
				if source is None:
					for field in days[day.isoformat()]:
						if field.startswith(prefix):
							days[day.isoformat()][field] = None
		day += timedelta(days=1)

	def display(value):
		return str(_amount(value, precision)) if value is not None else None

	scheduled = sum((r[1] for r in payables.get(anchor, [])), Decimal(0)) if ap is not None else None
	return {
		"anchor": anchor.isoformat(),
		"start": start.isoformat(),
		"end": end.isoformat(),
		"precision": precision,
		"days": days,
		"forecast_available": available,
		"payable_settlement_complete": complete_sides["payables"],
		"scope_reasons": sorted(reasons),
		"book_cash": display(opening),
		"floor": display(minimum),
		"due_now": {
			"scheduled": display(scheduled),
			"overdue": display(ap_buckets["overdue"]) if ap is not None else None,
			"undated": display(ap_buckets["undated"]) if ap is not None else None,
			"total": display(scheduled + ap_buckets["overdue"] + ap_buckets["undated"])
			if ap is not None
			else None,
		},
		"excluded_collections": {
			key: display(ar_buckets[key]) if ar is not None else None
			for key in ("overdue", "undated", "adjustments")
		},
		"source_notes": {
			side: source.get("notes", {}) if source else {}
			for side, source in (("Payable", ap), ("Receivable", ar))
		},
		"reconciliation": {
			side: {key: display(value) for key, value in buckets.items()} if buckets is not None else None
			for side, buckets in (("Payable", ap_buckets), ("Receivable", ar_buckets))
		},
	}


def upcoming_days(snapshot, selected):
	if snapshot.get("reconciliation", {}).get("Payable") is None:
		raise ProjectionError("payables_unavailable")
	selected, end = _date(selected), _date(snapshot["end"])
	until = selected + timedelta(days=30)
	if selected < _date(snapshot["start"]) or until > end:
		raise ProjectionError("invalid_range")
	rows = []
	day = selected + timedelta(days=1)
	while day <= until:
		data = snapshot["days"][day.isoformat()]
		if data["payable_remaining"] is not None and Decimal(data["payable_remaining"]) > 0:
			rows.append(
				{
					"date": day.isoformat(),
					"remaining": data["payable_remaining"],
					"payees": data["payable_parties_due"],
					"rows": data["payable_rows_due"],
					"overdue": day < _date(snapshot["anchor"]),
				}
			)
		day += timedelta(days=1)
	return rows
