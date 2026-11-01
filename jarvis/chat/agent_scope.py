"""Generic, zero-IP scope / period resolution for agent runs (amendment A6).

Resolves an explicit
``{company, fiscal_year, from_date, to_date, prior_fy_start, prior_fy_end}``
from an installation's ``config`` JSON plus the site date, so the container
bundle NEVER infers "the current period" itself (a UTC container clock vs an IST
site picks the wrong fiscal year for ~5.5h every day — catastrophic at the
Mar-31 / Apr-1 boundary). ``_launch_audit`` stamps the result on the Run
(``scope_json``) and injects it verbatim into the run message; prior-FY
selection stays versioned bench code, never LLM prose.

It is DELIBERATELY generic for normal delegates: it carries only ordinary
accounting dimensions (company / fiscal year / period boundaries). A narrow
delegate can add an explicit record root only through a server-side validation
hook. Delegate bundles resolve their run scope from these bench-injected,
versioned values — never LLM prose.

The helper runs under the caller's identity (the run-as user in
``_launch_audit``), so ``frappe.defaults`` / erpnext's fiscal-year lookup are
resolved for that user.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError

INSTALLATION = "Jarvis Agent Installation"
NEGATIVE_STOCK_AGENT = "negative-stock-valuation-auditor"
_NEGATIVE_STOCK_OUTPUT_SCOPE_KEYS = ("company", "from_date", "to_date", "report_date")


def _agent_slug(installation) -> str:
	return str(installation.get("agent") or installation.get("agent_slug") or "").strip()


def requires_bound_scope(installation) -> bool:
	"""Return true when a bad record root must refuse a launch.

	The negative-stock evaluator needs one Warehouse root to prove that its
	configured leaf set is complete. A missing or invalid root must never become
	a company-wide Warehouse scan.
	"""
	return _agent_slug(installation) == NEGATIVE_STOCK_AGENT


def negative_stock_record_scope(value) -> dict | None:
	"""Return a canonical Warehouse root, or ``None`` for untrusted input.

	This is only a shape check. Launch resolution also checks existence, company,
	enabled state, and the active run-as user's read permissions. Warehouse groups
	are valid roots: the evaluator may use a group to prove its leaf set.
	"""
	if (
		not isinstance(value, dict)
		or set(value) != {"doctype", "name"}
		or value.get("doctype") != "Warehouse"
		or not isinstance(value.get("name"), str)
		or not value["name"].strip()
		or value["name"] != value["name"].strip()
	):
		return None
	return {"doctype": "Warehouse", "name": value["name"]}


def negative_stock_output_scope(value) -> dict | None:
	"""Return the five safe negative-stock writeback keys, or ``None``.

	The evaluator may return this small projection at writeback. The server
	compares it with the launch-stamped projection, so no model field can change
	the Warehouse, company, period, or report-date cutoff during global merge.
	"""
	if not isinstance(value, dict):
		return None
	boundary = negative_stock_record_scope(value.get("record_scope"))
	if boundary is None:
		return None
	output = {}
	for key in _NEGATIVE_STOCK_OUTPUT_SCOPE_KEYS:
		item = value.get(key)
		if not isinstance(item, str) or not item.strip() or item != item.strip():
			return None
		output[key] = item
	output["record_scope"] = boundary
	return output


def _negative_stock_warehouse_scope(config: dict, company: str) -> dict:
	"""Validate the configured Warehouse root as the active run-as user.

	Only the canonical two-field value may enter ``Run.scope_json``. A Warehouse
	group is allowed, but a disabled, foreign, unreadable, or missing root is not.
	"""
	namespace = config.get("negative_stock")
	declared = namespace.get("record_scope") if isinstance(namespace, dict) else None
	boundary = negative_stock_record_scope(declared)
	if boundary is None:
		raise InvalidArgumentError("Negative stock record_scope must name one Warehouse")

	name = boundary["name"]
	try:
		warehouse = frappe.db.get_value("Warehouse", name, ["company", "disabled"], as_dict=True)
		warehouse_readable = frappe.has_permission("Warehouse", "read", doc=name)
	except Exception as exc:
		raise InvalidArgumentError("Negative stock Warehouse scope could not be verified") from exc
	if not warehouse or not warehouse_readable:
		raise InvalidArgumentError(
			"Negative stock record_scope must be a readable enabled Warehouse in the company"
		)
	try:
		company_readable = frappe.has_permission("Company", "read", doc=company)
	except Exception as exc:
		raise InvalidArgumentError("Negative stock Company scope could not be verified") from exc
	if (
		not company_readable
		or warehouse.get("company") != company
		or warehouse.get("disabled") is None
		or frappe.utils.cint(warehouse.get("disabled"))
	):
		raise InvalidArgumentError(
			"Negative stock record_scope must be a readable enabled Warehouse in the company"
		)
	return boundary


def resolve_scope(installation) -> dict:
	"""Resolve the explicit run scope for an installation.

	``installation`` may be a ``Jarvis Agent Installation`` doc or its name.
	Reads ``company`` / ``fiscal_year`` / ``from_date`` / ``to_date`` from the
	install's ``config`` JSON; falls back to the caller's default Company (or the
	site's single Company) and erpnext's current fiscal year for the missing
	pieces. The negative-stock auditor also requires a server-validated Warehouse
	root. A missing, malformed, foreign, disabled, or unreadable root raises
	instead of broadening the review.
	"""
	if isinstance(installation, str):
		installation = frappe.get_doc(INSTALLATION, installation)

	cfg: dict = {}
	if installation.get("config"):
		try:
			parsed = frappe.parse_json(installation.config)
			if isinstance(parsed, dict):
				cfg = parsed
		except Exception:
			cfg = {}

	def _s(key):
		val = cfg.get(key)
		return str(val).strip() if val is not None and str(val).strip() else None

	scope = _resolve(_s("company"), _s("fiscal_year"), _s("from_date"), _s("to_date"))
	if requires_bound_scope(installation):
		scope["record_scope"] = _negative_stock_warehouse_scope(cfg, scope["company"])
	return scope


def _resolve(company, fiscal_year, from_date, to_date) -> dict:
	if not company:
		company = frappe.defaults.get_user_default("Company") or frappe.db.get_default("company")
	if not company:
		companies = frappe.get_all("Company", pluck="name", limit=2)
		if len(companies) == 1:
			company = companies[0]
	if not company:
		raise InvalidArgumentError("company is required (could not infer a single default Company)")
	if not frappe.db.exists("Company", company):
		raise InvalidArgumentError(f"unknown Company: {company}")

	if fiscal_year:
		fy = frappe.db.get_value(
			"Fiscal Year", fiscal_year, ["year_start_date", "year_end_date"], as_dict=True
		)
		if not fy:
			raise InvalidArgumentError(f"unknown Fiscal Year: {fiscal_year}")
		from_date, to_date = str(fy.year_start_date), str(fy.year_end_date)

	if not (from_date and to_date):
		# Default to the company's CURRENT fiscal year via erpnext (site date,
		# never a container clock). erpnext is a hard dependency of every agent
		# (min_apps=["erpnext"]); if it is somehow absent the caller degrades.
		from erpnext.accounts.utils import get_fiscal_year as _gfy

		try:
			fy = _gfy(frappe.utils.today(), company=company, as_dict=True)
			from_date, to_date = str(fy.year_start_date), str(fy.year_end_date)
			fiscal_year = fy.name
		except Exception:
			raise InvalidArgumentError("from_date and to_date (or fiscal_year) are required")

	prior = _prior_fiscal_year(from_date)
	return {
		"company": company,
		"fiscal_year": fiscal_year,
		"from_date": from_date,
		"to_date": to_date,
		"prior_fy_start": prior["py_start"] if prior else None,
		"prior_fy_end": prior["py_end"] if prior else None,
		# The run's as-of date (site date, never a container clock). to_date bounds
		# the analysis window and MAY be in the future (a user-set or fiscal-year
		# end), but a period-end finding must not assess data that has not happened
		# yet: an evaluator caps its "due" cutoff at report_date so future-dated
		# schedule rows (e.g. the rest of the year's depreciation) are never flagged.
		"report_date": frappe.utils.today(),
	}


def _prior_fiscal_year(from_date) -> dict | None:
	"""The enabled Fiscal Year with the greatest ``year_end_date`` strictly
	before ``from_date`` (the period immediately preceding the scope). Returns
	``{name, py_start, py_end}`` or ``None`` when no such FY exists. Generic
	prior-period accounting logic."""
	rows = frappe.db.sql(
		"""select name, year_start_date, year_end_date from `tabFiscal Year`
           where year_end_date < %(from_date)s and disabled = 0
           order by year_end_date desc limit 1""",
		{"from_date": from_date},
		as_dict=True,
	)
	if not rows:
		return None
	fy = rows[0]
	return {"name": fy.name, "py_start": str(fy.year_start_date), "py_end": str(fy.year_end_date)}
