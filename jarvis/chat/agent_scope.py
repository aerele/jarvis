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


def _declared_record_scope(installation, config: dict, company: str) -> dict | None:
	"""Return the validated, agent-owned narrow scope, never a broad fallback.

	Most delegates use only the generic company/date scope.  The negative-stock
	auditor also needs one Warehouse root so it can prove that its configured leaf
	set is complete.  Keep that declaration in its own explicit config namespace,
	validate it on the server, and put only the resulting two-field value in the
	run scope.  A missing, malformed, foreign-company, or unknown Warehouse is
	not replaced with all Warehouses: the bundle receives no ``record_scope`` and
	fails its preflight before it can query a broad slice.
	"""
	agent = str(installation.get("agent") or installation.get("agent_slug") or "").strip()
	if agent != "negative-stock-valuation-auditor":
		return None

	namespace = config.get("negative_stock")
	declared = namespace.get("record_scope") if isinstance(namespace, dict) else None
	if (
		not isinstance(declared, dict)
		or set(declared) != {"doctype", "name"}
		or declared.get("doctype") != "Warehouse"
		or not isinstance(declared.get("name"), str)
		or not declared["name"].strip()
	):
		return None

	name = declared["name"].strip()
	try:
		warehouse_company = frappe.db.get_value("Warehouse", name, "company")
		# Scope resolution runs as ``run_as_user``.  A valid owner declaration
		# must still be readable by that identity before its name can reach the
		# delegate.  This applies Warehouse User Permissions as well as DocPerms.
		warehouse_readable = frappe.has_permission("Warehouse", "read", doc=name)
	except Exception:
		return None
	if not warehouse_readable or warehouse_company != company:
		return None
	return {"doctype": "Warehouse", "name": name}


def resolve_scope(installation) -> dict:
	"""Resolve the explicit run scope for an installation.

	``installation`` may be a ``Jarvis Agent Installation`` doc or its name.
	Reads ``company`` / ``fiscal_year`` / ``from_date`` / ``to_date`` from the
	install's ``config`` JSON; falls back to the caller's default Company (or the
	site's single Company) and erpnext's current fiscal year for the missing
	pieces. Raises :class:`InvalidArgumentError` when no company / period can be
	determined — the caller (``_launch_audit``) treats that as a soft failure and
	degrades to an unscoped run rather than aborting the launch.
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
	record_scope = _declared_record_scope(installation, cfg, scope["company"])
	if record_scope is not None:
		scope["record_scope"] = record_scope
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
