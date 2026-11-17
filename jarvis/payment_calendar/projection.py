"""Deterministic cash projection; no database, model or payment side effects."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation


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


def listed_row(remaining, original, kind):
	"""Whether a dated row is listed on its day: dues, and settled terms that once owed something.

	Negative residuals are excluded; zero-original placeholders do not make a date look settled.
	Day detail, the window bucket and the month row counts all use this one rule.
	"""
	return remaining > 0 or (remaining == 0 and original > 0 and kind == "obligation")


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
			# Positive accounting adjustments are still amounts owed.
			if due is not None and listed_row(remaining, original, row["kind"]):
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
		f"{prefix}_rows": len(rows),
		f"{prefix}_top_invoice": _top_invoice(rows, precision),
	}


def _top_invoice(rows, precision):
	"""Largest invoice by summed remaining; ties go to the smallest voucher, then party."""
	invoices = {}
	for row, remaining, _ in rows:
		if remaining <= 0:
			continue
		key = (row["voucher_type"], row["voucher_no"], row["party_type"], row["party"])
		name = row.get("party_name") or row["party"]
		invoice = invoices.setdefault(key, {"party_name": name, "remaining": Decimal(0)})
		invoice["remaining"] += remaining
	if not invoices:
		return None
	key = min(invoices, key=lambda k: (-invoices[k]["remaining"], *k))
	return {
		"voucher_type": key[0],
		"voucher_no": key[1],
		"party_name": invoices[key]["party_name"],
		"remaining": str(_amount(invoices[key]["remaining"], precision)),
	}


def _concentration(top, dues):
	"""(star flag, share of the day's dues); the star needs strictly more than half."""
	if top is None or Decimal(dues) <= 0:
		return False, None
	remaining, total = Decimal(top["remaining"]), Decimal(dues)
	share = (remaining * 100 / total).quantize(Decimal("0.1"), rounding=ROUND_DOWN)
	return remaining * 2 > total, str(share)


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


def project_snapshot(ap, ar, bank_total, floor, anchor, start, end, *, precision=2, month=None):
	"""Aggregate complete residuals; carry from the anchor even before visible start."""
	if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 9:
		raise ProjectionError("invalid_precision")
	anchor, start, end = _date(anchor), _date(start), _date(end)
	if start > end or (max(end, anchor) - min(start, anchor)).days > 800:
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

	def display(value):
		return str(_amount(value, precision)) if value is not None else None

	closing = opening
	cumulative_in = cumulative_out = Decimal(0)
	days = {}
	day = min(start, anchor)
	while day <= end:
		ap_rows = payables.get(day, []) if payables is not None else []
		ar_rows = receivables.get(day, []) if receivables is not None else []
		outgoing = sum((r[1] for r in ap_rows), Decimal(0))
		incoming = sum((r[1] for r in ar_rows), Decimal(0))
		cash = dict.fromkeys(
			(
				"projected_opening",
				"projected_closing",
				"risk",
				"cumulative_receivable",
				"cumulative_payable",
				"margin",
			)
		)
		if available and day >= anchor:
			cash["projected_opening"] = display(closing)
			# Running sums count scheduled rows only; overdue and undated dues are
			# charged once on the anchor day through due_now.
			cumulative_in += incoming
			cumulative_out += outgoing
			if day == anchor:
				outgoing += ap_buckets["overdue"] + ap_buckets["undated"]
			closing += incoming - outgoing
			cash.update(
				projected_closing=display(closing),
				risk="red" if closing < 0 else "orange" if closing < minimum else "green",
				cumulative_receivable=display(cumulative_in),
				cumulative_payable=display(cumulative_out),
				margin=display(closing - minimum),
			)
		if day >= start:
			days[day.isoformat()] = _day(day, ap, ar, ap_rows, ar_rows, cash, precision, complete_sides)
		day += timedelta(days=1)

	scheduled = sum((r[1] for r in payables.get(anchor, [])), Decimal(0)) if ap is not None else None
	immediate = ap_buckets["overdue"] + ap_buckets["undated"] if ap is not None else None
	result = {
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
			"immediate": display(immediate),
			"total": display(scheduled + immediate) if ap is not None else None,
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
		"month_totals": None,
	}
	if month:
		result["month"] = month
		result["month_totals"] = _month_totals(payables, receivables, complete_sides, start, end, precision)
	return result


def _day(day, ap, ar, ap_rows, ar_rows, cash, precision, complete_sides):
	"""One calendar cell; side fields are null for an unavailable side."""
	payable = _tallies(ap_rows, "payable", precision)
	concentrated, share = _concentration(payable["payable_top_invoice"], payable["payable_remaining"])
	entry = {
		"date": day.isoformat(),
		**payable,
		**_tallies(ar_rows, "receivable", precision),
		"settlement": _settlement(ap_rows) if ap is not None else "Unavailable",
		"payable_concentration_pct": share,
		**cash,
	}
	for prefix, source in (("payable_", ap), ("receivable_", ar)):
		if source is None:
			for field in entry:
				if field.startswith(prefix):
					entry[field] = None
	# Set after the null loop: both stay meaningful when their side is unavailable.
	entry["net"] = _net(entry, complete_sides, precision)
	entry["receivable_settlement"] = _settlement(ar_rows) if ar is not None else "Unavailable"
	entry["payable_concentrated"] = concentrated
	return entry


def _net(entry, complete_sides, precision):
	"""Receivable minus payable remaining for the day; null unless both sides are complete."""
	if not (complete_sides["payables"] and complete_sides["receivables"]):
		return None
	net = Decimal(entry["receivable_remaining"]) - Decimal(entry["payable_remaining"])
	return str(_amount(net, precision))


def _month_totals(payables, receivables, complete_sides, start, end, precision):
	"""Positive dated remaining inside the window; null unless both sides are complete.

	The row counts are the rows the window bucket lists (settled-zero terms included).
	"""
	complete = complete_sides["payables"] and complete_sides["receivables"]
	totals = dict.fromkeys(("receivable", "payable", "net", "receivable_rows", "payable_rows"))
	if not complete:
		return {**totals, "complete": False}
	sums = {}
	for side, groups in (("payable", payables), ("receivable", receivables)):
		rows = [r for due, day_rows in groups.items() if start <= due <= end for r in day_rows]
		sums[side] = sum((r[1] for r in rows if r[1] > 0), Decimal(0))
		totals[f"{side}_rows"] = len(rows)
	totals.update(
		receivable=str(_amount(sums["receivable"], precision)),
		payable=str(_amount(sums["payable"], precision)),
		net=str(_amount(sums["receivable"] - sums["payable"], precision)),
	)
	return {**totals, "complete": True}


def horizon_summary(horizon):
	"""The stored 30-day horizon from a projection that starts on the anchor.

	Days keep only closing and margin. `lowest` needs a closing on every day.
	"""
	days = {
		iso: {"projected_closing": day["projected_closing"], "margin": day["margin"]}
		for iso, day in horizon["days"].items()
	}
	closings = [
		(iso, Decimal(day["projected_closing"]))
		for iso, day in days.items()
		if day["projected_closing"] is not None
	]
	lowest = first_below = None
	if closings:
		floor = Decimal(horizon["floor"])
		first_below = next((iso for iso, value in closings if value < floor), None)
		if len(closings) == len(days):
			lowest = _lowest(closings, floor, horizon["precision"])
	return {
		"start": horizon["start"],
		"end": horizon["end"],
		"forecast_available": horizon["forecast_available"],
		"scope_reasons": horizon["scope_reasons"],
		"days": days,
		"lowest": lowest,
		"first_below_floor": first_below,
	}


def _lowest(closings, floor, precision):
	"""The minimum closing; a tie keeps the first date."""
	day, closing = closings[0]
	for iso, value in closings:
		if value < closing:
			day, closing = iso, value
	cushion = closing - floor
	state = "below" if cushion < 0 else "tight" if cushion <= floor * Decimal("0.10") else "safe"
	return {
		"date": day,
		"closing": str(_amount(closing, precision)),
		"cushion": str(_amount(cushion, precision)),
		"state": state,
	}


def upcoming_days(snapshot, selected):
	if snapshot.get("reconciliation", {}).get("Payable") is None:
		raise ProjectionError("payables_unavailable")
	selected, end = _date(selected), _date(snapshot["end"])
	until = (
		min(selected + timedelta(days=30), end) if snapshot.get("month") else selected + timedelta(days=30)
	)
	if selected < _date(snapshot["start"]) or selected > end or until > end:
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
