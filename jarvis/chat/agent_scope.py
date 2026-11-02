"""Resolve explicit scope and FEFO run boundaries for agent runs."""

import frappe
from frappe import _

from jarvis.exceptions import InvalidArgumentError

INSTALLATION = "Jarvis Agent Installation"
FEFO_AGENT = "fefo-expiry-compliance-auditor"
_FEFO_OUTPUT_SCOPE_KEYS = ("company", "from_date", "to_date", "report_date")
_FEFO_SCOPE_CONTRACT_KEY = "fefo_scope_contract"
_FEFO_SCOPE_CONTRACT = "fefo-expiry-compliance-v0.4.1"


def fefo_record_scope(value) -> dict | None:
	"""Return the exact optional FEFO Warehouse leaf declaration."""
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


def _fefo_warehouse_boundary(value, company: str) -> dict:
	"""Validate one FEFO Warehouse leaf as the active run-as user."""
	boundary = fefo_record_scope(value)
	if boundary is None:
		raise InvalidArgumentError("FEFO record_scope must name one readable enabled Warehouse leaf")
	name = boundary["name"]
	try:
		warehouse = frappe.db.get_value("Warehouse", name, ["company", "is_group", "disabled"], as_dict=True)
		warehouse_readable = frappe.has_permission("Warehouse", "read", doc=name)
		company_readable = frappe.has_permission("Company", "read", doc=company)
	except Exception as exc:
		raise InvalidArgumentError("FEFO Warehouse scope could not be verified") from exc
	if (
		not warehouse
		or not warehouse_readable
		or not company_readable
		or warehouse.get("company") != company
		or warehouse.get("is_group") is None
		or warehouse.get("disabled") is None
		or frappe.utils.cint(warehouse.get("is_group"))
		or frappe.utils.cint(warehouse.get("disabled"))
	):
		raise InvalidArgumentError("FEFO record_scope must name one readable enabled Warehouse leaf")
	return boundary


def _fefo_warehouse_scope(config: dict, company: str) -> dict | None:
	"""Return an optional FEFO leaf and preserve absent as company-wide."""
	if not record_scope_requested(config):
		return None
	return _fefo_warehouse_boundary(config.get("record_scope"), company)


def fefo_output_scope(value) -> dict | None:
	"""Project the immutable FEFO writeback boundary, or reject it."""
	if not isinstance(value, dict):
		return None
	output = {}
	for key in _FEFO_OUTPUT_SCOPE_KEYS:
		item = value.get(key)
		if not isinstance(item, str) or not item.strip() or item != item.strip():
			return None
		output[key] = item
	if "record_scope" in value:
		boundary = fefo_record_scope(value.get("record_scope"))
		if boundary is None:
			return None
		output["record_scope"] = boundary
	try:
		dates = {key: frappe.utils.getdate(output[key]) for key in ("from_date", "to_date", "report_date")}
	except Exception:
		return None
	if any(str(dates[key]) != output[key] for key in dates) or dates["from_date"] > min(
		dates["to_date"], dates["report_date"]
	):
		return None
	return output


def fefo_stamped_scope(value) -> dict | None:
	"""Return a current server-stamped FEFO projection, never a legacy scope."""
	if not isinstance(value, dict) or value.get(_FEFO_SCOPE_CONTRACT_KEY) != _FEFO_SCOPE_CONTRACT:
		return None
	return fefo_output_scope(value)


def fefo_stamped_scope_as_user(value, run_as_user: str) -> dict | None:
	"""Revalidate an immutable FEFO boundary as the bound run-as user."""
	scope = fefo_stamped_scope(value)
	if scope is None:
		return None
	if "record_scope" not in scope:
		return scope
	user = str(run_as_user or "").strip()
	if not user or user == "Guest":
		return None
	from jarvis._session import impersonate

	try:
		with impersonate(user):
			if _fefo_warehouse_boundary(scope["record_scope"], scope["company"]) != scope["record_scope"]:
				return None
	except Exception:
		return None
	return scope


def active_fefo_scope() -> dict | None:
	"""Return the active FEFO Run scope after server-side revalidation."""
	from jarvis.tools._agent_run_ctx import get_session_key

	session_key = get_session_key()
	if not session_key:
		return None
	run = frappe.db.get_value(
		"Jarvis Agent Run",
		{"session_key": session_key, "status": "running"},
		["agent", "scope_json"],
		as_dict=True,
	)
	if not run or run.get("agent") != FEFO_AGENT:
		return None
	run_as_user = frappe.db.get_value("Jarvis Chat Session", {"session_key": session_key}, "user")
	try:
		scope = frappe.parse_json(run.get("scope_json") or "{}")
	except Exception as exc:
		raise InvalidArgumentError("FEFO run scope is missing or malformed") from exc
	trusted = fefo_stamped_scope_as_user(scope, run_as_user)
	if trusted is None:
		raise InvalidArgumentError("FEFO record_scope is stale or unavailable")
	return trusted


_FEFO_SOURCE_DOCTYPES = frozenset(
	{
		"Company",
		"Warehouse",
		"Item",
		"Batch",
		"Stock Ledger Entry",
		"Serial and Batch Bundle",
		"Serial and Batch Entry",
		"Stock Entry",
		"Delivery Note",
		"Sales Invoice",
		"Repost Item Valuation",
		"Stock Closing Entry",
		"Inventory Dimension",
	}
)
_FEFO_LEAF_UNSCOPABLE_DOCTYPES = frozenset({"Inventory Dimension"})


def _fefo_equal(where, field: str, value) -> bool:
	return _fefo_predicate(where, field, "=", value)


def _fefo_predicate(where, field: str, op: str, value) -> bool:
	return any(
		isinstance(predicate, dict)
		and predicate.get("op") == op
		and predicate.get("field") == field
		and predicate.get("value") == value
		for predicate in (where or [])
	)


def _fefo_cutoff(scope: dict) -> str:
	return min(scope["to_date"], scope["report_date"])


def _fefo_main_equal(spec: dict, suffix: str, value) -> bool:
	alias = spec.get("alias")
	return isinstance(alias, str) and bool(alias) and _fefo_equal(spec.get("where"), f"{alias}.{suffix}", value)


def _fefo_sle_exists(
	spec: dict, field: str, reference: str, scope: dict, *, voucher_type: str | None = None
) -> bool:
	for predicate in spec.get("where") or []:
		if not isinstance(predicate, dict) or predicate.get("op") != "exists":
			continue
		child = predicate.get("value")
		if (
			not isinstance(child, dict)
			or child.get("from") != "Stock Ledger Entry"
			or child.get("alias") != "s"
		):
			continue
		where = child.get("where")
		if not _fefo_equal(where, "s.company", scope["company"]):
			continue
		if not _fefo_predicate(where, "s.posting_date", "<=", _fefo_cutoff(scope)):
			continue
		if not _fefo_predicate(where, "s.docstatus", "<", 2) or not _fefo_equal(where, "s.is_cancelled", 0):
			continue
		if not _fefo_equal(where, f"s.{field}", {"$field": reference}):
			continue
		if voucher_type and not _fefo_equal(where, "s.voucher_type", voucher_type):
			continue
		boundary = scope.get("record_scope")
		if boundary and not _fefo_equal(where, "s.warehouse", boundary["name"]):
			continue
		return True
	return False


def _fefo_closing_balance_exists(spec: dict, scope: dict) -> bool:
	for predicate in spec.get("where") or []:
		if not isinstance(predicate, dict) or predicate.get("op") != "exists":
			continue
		child = predicate.get("value")
		if (
			not isinstance(child, dict)
			or child.get("from") != "Stock Closing Balance"
			or child.get("alias") != "b"
		):
			continue
		where = child.get("where")
		if (
			_fefo_equal(where, "b.stock_closing_entry", {"$field": "r.name"})
			and _fefo_equal(where, "b.company", scope["company"])
			and _fefo_equal(where, "b.warehouse", scope["record_scope"]["name"])
		):
			return True
	return False


def _fefo_only_expected_joins(spec: dict, doctype: str) -> bool:
	joins = spec.get("joins") or []
	if doctype != "Serial and Batch Entry":
		return not joins
	return [
		{
			"doctype": "Serial and Batch Bundle",
			"alias": "b",
			"type": "inner",
			"on": {"r.parent": "b.name"},
		}
	] == joins


def _fefo_native_source_is_fenced(spec: dict, doctype: str, scope: dict) -> bool:
	if not _fefo_only_expected_joins(spec, doctype):
		return False
	boundary = scope.get("record_scope")
	if doctype == "Company":
		return _fefo_main_equal(spec, "name", scope["company"])
	if doctype == "Inventory Dimension":
		return boundary is None
	if doctype == "Warehouse":
		return _fefo_main_equal(spec, "company", scope["company"]) and (
			boundary is None or _fefo_main_equal(spec, "name", boundary["name"])
		)
	if doctype == "Stock Ledger Entry":
		return (
			_fefo_main_equal(spec, "company", scope["company"])
			and _fefo_predicate(spec.get("where"), f"{spec.get('alias')}.posting_date", "<=", _fefo_cutoff(scope))
			and _fefo_predicate(spec.get("where"), f"{spec.get('alias')}.docstatus", "<", 2)
			and _fefo_equal(spec.get("where"), f"{spec.get('alias')}.is_cancelled", 0)
			and (boundary is None or _fefo_main_equal(spec, "warehouse", boundary["name"]))
		)
	if doctype == "Repost Item Valuation":
		return _fefo_main_equal(spec, "company", scope["company"]) and (
			boundary is None or _fefo_main_equal(spec, "warehouse", boundary["name"])
		)
	if doctype == "Stock Closing Entry":
		return (
			_fefo_main_equal(spec, "company", scope["company"])
			and _fefo_equal(spec.get("where"), f"{spec.get('alias')}.docstatus", 1)
			and _fefo_predicate(spec.get("where"), f"{spec.get('alias')}.to_date", "<=", _fefo_cutoff(scope))
			and (boundary is None or _fefo_closing_balance_exists(spec, scope))
		)
	if doctype == "Item":
		return _fefo_sle_exists(spec, "item_code", "r.name", scope)
	if doctype == "Batch":
		return _fefo_sle_exists(spec, "item_code", "r.item", scope)
	if doctype == "Serial and Batch Bundle":
		return _fefo_sle_exists(spec, "serial_and_batch_bundle", "r.name", scope)
	if doctype == "Serial and Batch Entry":
		return _fefo_sle_exists(spec, "serial_and_batch_bundle", "b.name", scope)
	if doctype in {"Stock Entry", "Delivery Note", "Sales Invoice"}:
		return _fefo_sle_exists(spec, "voucher_no", "r.name", scope, voucher_type=doctype)
	return False


def assert_active_fefo_query_scope(spec) -> None:
	"""Fence native FEFO query requests to the immutable launch scope."""
	scope = active_fefo_scope()
	if scope is None:
		return
	if not isinstance(spec, dict) or not isinstance(spec.get("from"), str):
		raise InvalidArgumentError("FEFO native source request is malformed")
	from jarvis.tools._doctype_name import canonical_doctype

	doctype = canonical_doctype(spec["from"])
	if doctype not in _FEFO_SOURCE_DOCTYPES:
		raise InvalidArgumentError("FEFO native source request is outside the approved source set")
	boundary = scope.get("record_scope")
	if boundary and doctype in _FEFO_LEAF_UNSCOPABLE_DOCTYPES:
		raise InvalidArgumentError("FEFO source is unavailable for a Warehouse-scoped review")
	if not _fefo_native_source_is_fenced(spec, doctype, scope):
		if boundary:
			raise InvalidArgumentError("FEFO native source request is outside the stamped leaf")
		raise InvalidArgumentError("FEFO native source request is outside the stamped scope")


def assert_active_fefo_report_scope(report_name: str, filters: dict) -> None:
	"""Fence the FEFO native report request to the immutable launch scope."""
	scope = active_fefo_scope()
	if scope is None:
		return
	if report_name != "Batch-Wise Balance History" or not isinstance(filters, dict):
		raise InvalidArgumentError("FEFO report request is outside the approved source set")
	expected = {
		"company": scope["company"],
		"from_date": scope["from_date"],
		"to_date": min(scope["to_date"], scope["report_date"]),
	}
	if scope.get("record_scope"):
		expected["warehouse"] = scope["record_scope"]["name"]
	if filters != expected:
		raise InvalidArgumentError("FEFO native report request is outside the stamped scope")


def _configuration(config) -> dict:
	"""Return a configuration object and preserve the legacy malformed fallback."""
	if isinstance(config, dict):
		return config
	if config:
		try:
			parsed = frappe.parse_json(config)
			if isinstance(parsed, dict):
				return parsed
		except Exception:
			pass
	return {}


def record_scope_requested(config) -> bool:
	"""True when a tenant supplied the record-scope namespace explicitly."""
	return "record_scope" in _configuration(config)


def assert_record_scope_supported(supports_record_scope: bool, config) -> None:
	"""Refuse an unsupported boundary before configuration or launch has effects."""
	if record_scope_requested(config) and supports_record_scope is not True:
		frappe.throw(
			_("This agent does not support record-scoped runs. Remove record_scope from its configuration.")
		)


def resolve_scope(installation, *, allow_record_scope: bool = False) -> dict:
	"""Resolve the explicit run scope for an installation."""
	if isinstance(installation, str):
		installation = frappe.get_doc(INSTALLATION, installation)

	cfg = _configuration(installation.get("config"))
	agent_slug = str(installation.get("agent") or installation.get("agent_slug") or "").strip()
	if record_scope_requested(cfg) and not allow_record_scope:
		raise InvalidArgumentError("record_scope is not supported by this agent")

	def _s(key):
		val = cfg.get(key)
		return str(val).strip() if val is not None and str(val).strip() else None

	scope = _resolve(_s("company"), _s("fiscal_year"), _s("from_date"), _s("to_date"))
	if agent_slug == FEFO_AGENT:
		boundary = _fefo_warehouse_scope(cfg, scope["company"])
		if boundary is not None:
			scope["record_scope"] = boundary
		scope[_FEFO_SCOPE_CONTRACT_KEY] = _FEFO_SCOPE_CONTRACT
		return scope
	if not record_scope_requested(cfg):
		return scope

	record = cfg["record_scope"]
	if not record:
		return scope
	if not isinstance(record, dict) or not all(record.get(k) for k in ("doctype", "name")):
		raise InvalidArgumentError("record_scope requires a doctype and name")
	doc = frappe.get_doc(record["doctype"], record["name"])
	doc.check_permission("read")
	if doc.get("company") and doc.company != scope["company"]:
		raise InvalidArgumentError("record_scope must belong to the selected company")
	scope["record_scope"] = {key: str(record[key]) for key in ("doctype", "name")}
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
		"report_date": frappe.utils.today(),
	}


def _prior_fiscal_year(from_date) -> dict | None:
	"""Return the enabled fiscal year immediately before ``from_date``."""
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
