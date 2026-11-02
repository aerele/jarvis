"""Generic, zero-IP scope / period resolution for agent runs (amendment A6).

Resolves an explicit
``{company, fiscal_year, from_date, to_date, prior_fy_start, prior_fy_end}``
from an installation's ``config`` JSON plus the site date, so the container
bundle NEVER infers "the current period" itself (a UTC container clock vs an IST
site picks the wrong fiscal year for ~5.5h every day — catastrophic at the
Mar-31 / Apr-1 boundary). ``_launch_audit`` stamps the result on the Run
(``scope_json``) and injects it verbatim into the run message; prior-FY
selection stays versioned bench code, never LLM prose.

It is DELIBERATELY generic: it carries no rule shape, only ordinary accounting
dimensions (company / fiscal year / period boundaries), so the delegate bundles
resolve their run scope from bench-injected, versioned values — never LLM prose.

The helper runs under the caller's identity (the run-as user in
``_launch_audit``), so ``frappe.defaults`` / erpnext's fiscal-year lookup are
resolved for that user.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError

INSTALLATION = "Jarvis Agent Installation"


def authoritative_writeback_scope(run_doc, submitted_scope) -> dict:
	"""Return the launch-stamped scope after checking the evaluator echo.

	The evaluator receives the scope in its run message and returns the scope it
	used with its findings. That return value is useful as a consistency check,
	but it is not an authority to change a run. In particular, it must never
	select the company used for finding persistence or auto-resolution.

	An evaluator may return only the dimensions that apply to its data model. For
	example, a CRM evaluator does not bind a Company dimension. Each supplied key
	must therefore exist in, and exactly match, the server-stamped launch scope.
	A scoped run also requires a non-empty evaluator scope, so a missing result
	cannot silently bypass the check. An unscoped legacy run may return an empty
	scope, but it may not introduce a scope of its own.
	"""
	# Older unscoped launches leave scope_json unset. Treat that as an empty server
	# scope, never as permission for the evaluator to add a replacement scope.
	stamped = _scope_object(run_doc.get("scope_json"), label="server-stamped run scope", allow_missing=True)
	submitted = _scope_object(submitted_scope, label="evaluator scope", allow_missing=True)

	if not stamped:
		if submitted:
			raise InvalidArgumentError("evaluator scope cannot add dimensions to an unscoped run")
		return {}
	if not submitted:
		raise InvalidArgumentError("evaluator scope is required for a scoped run")

	for key, value in submitted.items():
		if key not in stamped:
			raise InvalidArgumentError(f"evaluator scope has unstamped dimension: {key}")
		if stamped[key] != value:
			raise InvalidArgumentError(f"evaluator scope does not match server-stamped {key}")

	# A fresh dict prevents later code from mutating the run's trusted stamp.
	return dict(stamped)


def _scope_object(value, *, label: str, allow_missing: bool = False) -> dict:
	"""Parse one scope object without the old silent fallback-to-empty behavior."""
	if value is None and allow_missing:
		return {}
	if isinstance(value, str):
		try:
			value = frappe.parse_json(value)
		except Exception:
			raise InvalidArgumentError(f"{label} must be a JSON object") from None
	if not isinstance(value, dict):
		raise InvalidArgumentError(f"{label} must be a JSON object")
	return value


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

	return _resolve(_s("company"), _s("fiscal_year"), _s("from_date"), _s("to_date"))


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
