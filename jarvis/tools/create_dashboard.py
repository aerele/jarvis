"""Create a Frappe Dashboard from existing Dashboard Charts (+ Number Cards).

Assembles charts (create them first with ``create_dashboard_chart``) into a
saved Dashboard the customer can open. Runs under the calling user:
``doc.insert()`` enforces create permission on Dashboard, and Frappe validates
the chart/card links.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError

_WIDTHS = {"Half", "Full"}


def create_dashboard(
	dashboard_name: str,
	charts: list | None = None,
	cards: list | None = None,
) -> dict:
	"""Create a Dashboard; return {name, dashboard_name, url}.

	``charts``: list of Dashboard Chart names, or dicts {chart, width: Half|Full}.
	``cards``: optional list of Number Card names (or {card} dicts).
	"""
	if not dashboard_name:
		raise InvalidArgumentError("dashboard_name is required")
	if not charts:
		raise InvalidArgumentError("charts must be a non-empty list of Dashboard Chart names")

	doc = frappe.new_doc("Dashboard")
	doc.dashboard_name = dashboard_name
	for ch in charts:
		if isinstance(ch, dict):
			name, width = ch.get("chart"), (ch.get("width") or "Half")
		else:
			name, width = ch, "Half"
		if not name:
			raise InvalidArgumentError("each chart needs a 'chart' name")
		doc.append("charts", {"chart": name, "width": width if width in _WIDTHS else "Half"})
	for cd in cards or []:
		card = cd.get("card") if isinstance(cd, dict) else cd
		if card:
			doc.append("cards", {"card": card})

	doc.insert()
	return {
		"name": doc.name,
		"dashboard_name": doc.dashboard_name,
		"url": f"/app/dashboard-view/{doc.name}",
	}
