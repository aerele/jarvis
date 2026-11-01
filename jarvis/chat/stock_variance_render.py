"""Render the trusted server stock-count worklist."""

from html import escape
from urllib.parse import quote

MAX_DASHBOARD_CHARS = 950_000
INTEGRITY_DIGEST_PLACEHOLDER = "0" * 64


def render(sheet):
	def e(v):
		return escape(str(v if v is not None else ""))

	def qty(v):
		return e(format(__import__("decimal").Decimal(str(v)).normalize(), "f"))

	def link(name):
		return "/app/jarvis-physical-count/" + quote(str(name), safe="")

	def money(v):
		return e(sheet.get("currency") or "") + " " + f"{v:,.2f}"

	complete = sheet.get("run_state") == "evaluated_clean"
	partial = sheet.get("run_state") == "partial"
	parts = [
		'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:20px;color:#17212c;overflow-wrap:anywhere}main{max-width:1050px;margin:auto}h1{font-size:24px}h2{font-size:19px;margin-top:26px}.notice{padding:12px;background:#eef5ff;border-radius:8px}.card{border:1px solid #d7dfe8;border-radius:8px;padding:14px;margin:12px 0}.muted{color:#536275}.metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}a{color:#0759aa}p{margin:8px 0}ul{padding-left:20px}@media(max-width:500px){body{padding:12px}h1{font-size:21px}.metrics{grid-template-columns:1fr}}</style></head><body><main><h1>Stock count review</h1>',
		"<p><strong>"
		+ (
			"Checks complete for selected counts"
			if complete
			else "Some selected counts could not be completed"
			if partial
			else "The stock count check could not be completed"
		)
		+ "</strong></p>",
	]
	if sheet.get("test_declaration"):
		parts.append('<p class="notice">' + e(sheet["test_declaration"]) + "</p>")
	scope = sheet.get("scope") if isinstance(sheet.get("scope"), dict) else {}
	record = scope.get("record_scope")
	root_name = record.get("name") if isinstance(record, dict) else None
	parts += [
		"<p>Company: " + e(scope.get("company") or "Not set") + "</p>",
		"<p>Review: "
		+ e(scope.get("from_date") or "Not set")
		+ " to "
		+ e(scope.get("to_date") or "Not set")
		+ ". Report cutoff: "
		+ e(scope.get("effective_cutoff") or "Not set")
		+ ".</p>",
		"<p>Selected warehouse scope: " + e(root_name or "Not set") + ".</p>",
		"<p>Reviewer: configured</p>",
		'<p class="muted">Book stock is checked at each saved count time, using current retained ledger records. A later correction can change an earlier balance. Current live stock can differ. This report does not prove a cause, loss or required stock adjustment.</p>',
	]
	if sheet.get("integrity_digest"):
		parts.append(
			'<p class="muted">Evaluator version: '
			+ e(sheet.get("evaluator_version") or "Not set")
			+ ". Integrity digest: "
			+ e(sheet["integrity_digest"])
			+ ".</p>"
		)
	if not complete:
		cov = next(iter(sheet.get("coverage", {}).values()), {})
		marker = sheet.get("incomplete") if isinstance(sheet.get("incomplete"), dict) else {}
		detail = (
			marker.get("detail")
			if partial and marker
			else cov.get("detail") or "A required source is missing or incomplete."
		)
		if cov.get("reason_code") == "configuration_missing":
			detail = (
				"Name the stock reviewer."
				if detail == "the stock reviewer"
				else "Complete these review settings: " + detail.rstrip(".") + "."
			)
		parts.append('<p class="notice">' + e(detail) + "</p>")
		if partial and (sheet.get("findings") or sheet.get("count_results")):
			parts.append(
				"<p>Some selected count documents could not be evaluated. "
				"The findings and controls below cover only the completed count documents. "
				"Review the excluded documents and run the check again.</p>"
			)
		else:
			parts.append(
				"<p>Complete the missing source or review settings, then run the check again. "
				"No numeric findings were saved from this incomplete check.</p>"
			)
	if complete or sheet.get("findings"):
		parts.append("<h2>Count differences to review</h2>")
	if complete and not sheet["findings"]:
		parts.append("<p>No material quantity differences were found in the completed selected counts.</p>")
	for finding in sheet.get("findings", []):
		parts += [
			'<section class="card"><h2>Submitted physical count ' + e(finding["ref_name"]) + "</h2>",
			"<p>Count time: " + e(finding["count_time"]) + "</p>",
			"<p>Sum of absolute material gaps at recorded book rates: <strong>"
			+ money(finding["amount"])
			+ "</strong>. Opposite signs do not cancel.</p>",
			'<p><a target="_blank" rel="noopener" href="'
			+ e(link(finding["ref_name"]))
			+ '">Open submitted physical count: '
			+ e(finding["ref_name"])
			+ "</a></p>",
		]
		if finding.get("lines_sha256"):
			parts.append(
				'<p class="muted">Evidence: '
				+ e(finding.get("line_count", len(finding["lines"])))
				+ " material lines; line digest: "
				+ e(finding["lines_sha256"])
				+ ".</p>"
			)
		for row in finding["lines"]:
			parts += [
				'<section class="card"><strong>Item: '
				+ e(row["item_code"])
				+ " · "
				+ e(row["direction"].capitalize())
				+ "</strong>",
				"<p>Warehouse: " + e(row["warehouse"]) + "</p>",
				'<div class="metrics"><div>Counted: '
				+ qty(row["counted_qty"])
				+ " "
				+ e(row["stock_uom"])
				+ "</div><div>Book: "
				+ qty(row["system_qty"])
				+ " "
				+ e(row["stock_uom"])
				+ "</div><div>Quantity gap: "
				+ qty(row["variance_qty"])
				+ " "
				+ e(row["stock_uom"])
				+ "</div><div>Recorded book rate: "
				+ money(row["book_rate"])
				+ "</div><div>Signed value: "
				+ money(row["signed_value"])
				+ "</div><div>Absolute value: "
				+ money(row["amount"])
				+ "</div></div></section>",
			]
		parts.append(
			'<p class="muted">Check timing differences, counting errors and valid stock movements before taking action. The report does not choose a cause.</p></section>'
		)
	if complete or sheet.get("count_results") or sheet.get("excluded_counts"):
		parts.append(
			"<h2>All reviewed count documents</h2>"
			if complete
			else "<h2>Completed and excluded count documents</h2>"
		)
		for count in sheet["count_results"]:
			parts += [
				'<section class="card"><strong>' + e(count["name"]) + "</strong>",
				"<p>Count time: " + e(count["count_time"]) + "</p>",
				"<p>Material lines: "
				+ e(count["material_lines"])
				+ ". Matched quantities: "
				+ e(count["matched_lines"])
				+ ". Other quantity gaps below the review threshold: "
				+ e(count["below_threshold_lines"])
				+ ".</p>",
				'<p><a target="_blank" rel="noopener" href="'
				+ e(link(count["name"]))
				+ '">Open submitted physical count: '
				+ e(count["name"])
				+ "</a></p>",
			]
			controls = [row for row in count["lines"] if not row["material"]]
			if controls:
				parts.append("<details><summary>Other count lines</summary>")
				for row in controls:
					parts += [
						'<section class="card"><strong>Item: ' + e(row["item_code"]) + "</strong>",
						"<p>Warehouse: " + e(row["warehouse"]) + "</p>",
						"<p>Counted: "
						+ qty(row["counted_qty"])
						+ " "
						+ e(row["stock_uom"])
						+ "; book: "
						+ qty(row["system_qty"])
						+ " "
						+ e(row["stock_uom"])
						+ "; quantity gap: "
						+ qty(row["variance_qty"])
						+ " "
						+ e(row["stock_uom"])
						+ ".</p>",
						"<p>Absolute value at the recorded book rate: "
						+ money(row["amount"])
						+ ". "
						+ (
							"The quantities match."
							if row["variance_qty"] == 0
							else "This gap is below the review threshold."
						)
						+ "</p></section>",
					]
				parts.append("</details>")
			parts.append("</section>")
		parts.append("<h2>Excluded count documents</h2>")
		if not sheet["excluded_counts"]:
			parts.append("<p>No submitted physical count was excluded.</p>")
		for row in sheet["excluded_counts"]:
			parts.append(
				"<p>" + e(row["name"]) + " · " + e(row["counted_at"]) + " · " + e(row["reason"]) + ".</p>"
			)
		if sheet["cross_references"]:
			parts.append(
				'<p class="notice">Some book quantities are below zero. Review stock valuation separately. The quantity differences above remain part of this count review.</p>'
			)
			for row in sheet["cross_references"]:
				parts.append(
					"<p>Item: " + e(row["item_code"]) + "; warehouse: " + e(row["warehouse"]) + ".</p>"
				)
	parts += [
		"<h2>Scope and limits</h2><p>This review discovers submitted physical counts in the selected dates for the selected warehouse scope. Counts outside this scope are not read. It uses the saved count time and retained ledger history, not a live stock balance.</p>",
		'<p class="muted">Supported sizing: INR books and ordinary stock Items. Batch, serial and inventory-dimension counts need a separate adapter. This report does not prove that another agent or operator is active.</p></main></body></html>',
	]
	return "".join(parts)


def writeback_payload(sheet):
	finding_fields = [
		"check_id",
		"token",
		"ref_doctype",
		"ref_name",
		"result_class",
		"severity",
		"amount",
		"title",
		"note",
		"count_time",
		"line_count",
		"lines_sha256",
	]
	payload = {
		k: sheet[k]
		for k in [
			"coverage",
			"scope",
			"truncated",
			"rows_consumed",
			"integrity_digest",
		]
	}
	# The detailed incomplete marker stays in the fixed server-rendered dashboard.
	# The generic writeback only needs this boolean to keep the run partial and to
	# prevent auto-resolution from a source set that excluded one or more counts.
	payload["partial"] = bool(sheet.get("incomplete"))
	payload["findings"] = [{key: finding[key] for key in finding_fields} for finding in sheet["findings"]]
	payload["scope"] = {
		k: v for k, v in sheet["scope"].items() if k not in {"effective_cutoff", "warehouse_set"}
	}
	return payload
