"""Native, manual replenishment review from customer-owned ERPNext policies.

No model-supplied source, grade, quantity, or finding is accepted. Source rows
stay inside a read-only server transaction. Only the sealed named reviewer can
read the derived review rows or record a decision. A decision never posts ERP.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import ROUND_CEILING, Decimal, InvalidOperation

import frappe
from frappe.utils.password import get_encryption_key

from jarvis._session import impersonate
from jarvis.chat import negative_stock_receipt as trust
from jarvis.permissions import has_jarvis_access, refuse_in_tool_dispatch, require_jarvis_user

AGENT = "reorder-replenishment-operator"
VERSION = "0.2.0"
POLICY = "erpnext-reorder-v1"
TOOL = "prepare_reorder_review"
STATE = "reorder-review-v1"
MAX_ROWS = 20_000
MAX_PROPOSALS = 100
EPS = Decimal("0.000001")
MONEY_EPS = Decimal("0.01")
RUN = "Jarvis Agent Run"
_INVALID = "Reorder settings are invalid. Select a current Negative Stock run and save the settings."
_SOURCE = "Stock evidence is incomplete, changed, or needs manual review. Run Negative Stock again after checking the source."
_STALE = "Stock or purchasing records changed. Start a new Reorder review before you approve a suggestion."
_ACCESS = "You cannot read the complete source for this Reorder review. Check your access and run again."
_STATE = "This Reorder review is unavailable. Start a new review."
_REVIEW_ACCESS = (
	"Select an enabled reviewer who can use Jarvis and read the complete stock and purchasing source."
)
_UNSUPPORTED = "This source needs manual stock review. This release cannot prepare suggestions for changing receipt costs, landed costs, or stock adjustments."


def refuse(message=_SOURCE):
	frappe.throw(message, frappe.ValidationError)


def canonical(value):
	return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def seal(value):
	return hmac.new(
		get_encryption_key().encode(), (STATE + canonical(value)).encode(), hashlib.sha256
	).hexdigest()


def number(value):
	if value is None or isinstance(value, bool):
		refuse()
	try:
		result = Decimal(str(value))
	except (InvalidOperation, TypeError, ValueError):
		refuse()
	if not result.is_finite() or abs(result) > Decimal("1e12"):
		refuse()
	return result


def configuration(inst):
	try:
		cfg = json.loads(inst.get("config") or "{}")
		if inst.get("installed_version") != VERSION:
			refuse(_INVALID)
		if set(cfg) != {"policy_version", "negative_stock_run", "max_proposals"}:
			refuse(_INVALID)
		if (
			cfg["policy_version"] != POLICY
			or type(cfg["max_proposals"]) is not int
			or not 1 <= cfg["max_proposals"] <= MAX_PROPOSALS
		):
			refuse(_INVALID)
		name = cfg["negative_stock_run"]
		if not isinstance(name, str) or not 1 <= len(name) <= 140:
			refuse(_INVALID)
		negative = frappe.get_doc(RUN, name)
		scope = trust._scope(negative.get("scope_json"))
		if negative.agent != trust.AGENT or scope["effective_as_of"] != frappe.utils.today():
			refuse(_INVALID)
		reviewer = inst.get("reviewer")
		if not reviewer or not frappe.db.get_value("User", reviewer, "enabled"):
			refuse(_INVALID)
		if not has_jarvis_access(reviewer):
			refuse(_REVIEW_ACCESS)
		return {**cfg, "scope": json.loads(negative.scope_json), "reviewer": reviewer}
	except (KeyError, TypeError, ValueError, frappe.DoesNotExistError, trust.ReceiptRefused):
		refuse(_INVALID)


def configuration_issues(raw):
	try:
		cfg = json.loads(raw or "{}")
		return (
			{}
			if set(cfg) == {"policy_version", "negative_stock_run", "max_proposals"}
			and cfg.get("policy_version") == POLICY
			and type(cfg.get("max_proposals")) is int
			and 1 <= cfg["max_proposals"] <= MAX_PROPOSALS
			and isinstance(cfg.get("negative_stock_run"), str)
			and cfg["negative_stock_run"].strip()
			else {"reorder": _INVALID}
		)
	except (TypeError, ValueError):
		return {"reorder": _INVALID}


def scope_from_config(config):
	return config["scope"]


def release_available():
	from jarvis.chat.agent_catalog import registry_tools_allow

	return registry_tools_allow(AGENT) == ["jarvis__" + TOOL]


def modern_run(run):
	try:
		return bool(
			run
			and run.get("agent") == AGENT
			and run.get("bundle_version") == VERSION
			and run.get("capability_contract") == "snapshot"
			and json.loads(run.get("tools_allow_json") or "[]") == ["jarvis__" + TOOL]
			and json.loads(run.get("capability_writes_json") or "null") == []
			and json.loads(run.get("assessment_config_json") or "{}").get("policy_version") == POLICY
		)
	except (TypeError, ValueError):
		return False


def require_fields(doctype, fields, *, parenttype=None):
	from frappe.model import get_permitted_fields

	meta = frappe.get_meta(doctype)
	if set(fields) - set(
		get_permitted_fields(doctype, parenttype=parenttype, permission_type="read", ignore_virtual=True)
	) or any(meta.get_field(f) and meta.get_field(f).get("mask") for f in fields):
		refuse(_ACCESS)


def readable_rows(doctype, fields, filters=None):
	"""Compare native permissions with the bounded full source, never skip a slice."""
	require_fields(doctype, fields)
	rows = frappe.get_all(
		doctype, fields=fields, filters=filters or {}, order_by="name", limit_page_length=MAX_ROWS + 1
	)
	if len(rows) > MAX_ROWS:
		refuse()
	if not frappe.has_permission(doctype, "read"):
		refuse(_ACCESS)
	visible = set(
		frappe.get_list(doctype, filters=filters or {}, pluck="name", limit_page_length=MAX_ROWS + 1)
	)
	if visible != {r.name for r in rows}:
		refuse(_ACCESS)
	return rows


def children(doctype, parents, fields):
	if not parents:
		return []
	from frappe.model import get_permitted_fields

	parenttype = {
		"Item Reorder": "Item",
		"Material Request Item": "Material Request",
		"Purchase Order Item": "Purchase Order",
	}[doctype]
	meta = frappe.get_meta(doctype)
	if set(fields) - set(
		get_permitted_fields(doctype, parenttype=parenttype, permission_type="read", ignore_virtual=True)
	) or any(meta.get_field(f) and meta.get_field(f).get("mask") for f in fields):
		refuse(_ACCESS)
	rows = frappe.get_all(
		doctype,
		filters={"parent": ["in", parents]},
		fields=fields,
		order_by="name",
		limit_page_length=MAX_ROWS + 1,
	)
	if len(rows) > MAX_ROWS:
		refuse()
	return rows


def native_stock_guard(data, scope):
	"""Conservative public accounting invariants, not a copy of private grade rules.

	A changing receipt unit cost, reconciliation, landed cost or unsupported stock
	shape is held for manual review. This deliberately admits a smaller cohort than
	the general auditor. Model flags cannot waive any of these checks.
	"""
	as_of = min(scope.get("to_date", scope["report_date"]), scope["report_date"])
	company = data["company"][0]
	if not company.enable_perpetual_inventory or not company.default_inventory_account:
		refuse()
	warehouses = {r.name: r for r in data["warehouses"]}
	root = warehouses.get(scope["record_scope"]["name"])
	if not root or not root.is_group:
		refuse()
	leaf = {n: w for n, w in warehouses.items() if not w.is_group}
	if any(w.disabled or not root.lft <= w.lft <= w.rgt <= root.rgt for w in leaf.values()):
		refuse()
	accounts = {r.name: r for r in data["accounts"] if r.account_type == "Stock" and not r.is_group}
	if any(
		a.disabled or (a.account_currency or company.default_currency) != company.default_currency
		for a in accounts.values()
	):
		refuse()
	wh_account = {n: w.account or company.default_inventory_account for n, w in leaf.items()}
	if not leaf or set(wh_account.values()) - accounts.keys():
		refuse()
	if any(int(r.docstatus) == 1 for r in data["landed_costs"]):
		refuse(_UNSUPPORTED)
	if any(r.status not in {"Completed", "Cancelled", "Skipped"} for r in data["reposts"]):
		refuse()
	items = {r.name: r for r in data["items"]}
	positions, rates = {}, {}
	entries = sorted(
		data["ledger"],
		key=lambda r: (
			frappe.utils.getdate(r.posting_date),
			frappe.utils.get_timedelta(str(r.posting_time)),
			frappe.utils.get_datetime(r.creation),
			r.name,
		),
	)
	for row in entries:
		if row.is_cancelled:
			continue
		if str(row.posting_date) > as_of:
			refuse()
		key = row.item_code, row.warehouse
		if row.is_adjustment_entry or row.voucher_type == "Stock Reconciliation":
			refuse(_UNSUPPORTED)
		if (
			row.warehouse not in leaf
			or row.item_code not in items
			or items[row.item_code].allow_negative_stock
		):
			refuse()
		old_qty, old_value = positions.get(key, (Decimal(0), Decimal(0)))
		qty, balance = number(row.actual_qty), number(row.qty_after_transaction)
		value, delta, rate = (
			number(row.stock_value),
			number(row.stock_value_difference),
			number(row.valuation_rate),
		)
		if balance < 0 or abs(old_qty + qty - balance) > EPS or abs(value - old_value - delta) > MONEY_EPS:
			refuse()
		if (balance == 0 and abs(value) > MONEY_EPS) or (
			balance > 0 and (rate <= 0 or abs(value - balance * rate) > MONEY_EPS)
		):
			refuse()
		if qty > 0:
			incoming = number(row.incoming_rate)
			if incoming <= 0:
				refuse()
			if key in rates and abs(rates[key] - incoming) > EPS:
				refuse(_UNSUPPORTED)
			rates[key] = incoming
		if (qty == 0 and abs(delta) > MONEY_EPS) or (
			balance > 0 and key in rates and abs(rate - rates[key]) > EPS
		):
			refuse()
		positions[key] = balance, value
	bin_keys = set()
	from erpnext.stock.doctype.bin.bin import PROJECTED_QTY_ADDITIONS, PROJECTED_QTY_DEDUCTIONS

	for row in data["bins"]:
		key = row.item_code, row.warehouse
		if key in bin_keys or row.warehouse not in leaf:
			refuse()
		bin_keys.add(key)
		qty, value = positions.get(key, (Decimal(0), Decimal(0)))
		if abs(number(row.actual_qty) - qty) > EPS or abs(number(row.stock_value) - value) > MONEY_EPS:
			refuse()
		if qty > 0 and abs(number(row.valuation_rate) * qty - value) > MONEY_EPS:
			refuse()
		projected = sum((number(row.get(f)) for f in PROJECTED_QTY_ADDITIONS), Decimal(0)) - sum(
			(number(row.get(f)) for f in PROJECTED_QTY_DEDUCTIONS), Decimal(0)
		)
		if abs(number(row.projected_qty) - projected) > EPS:
			refuse()
	if any((q != 0 or v != 0) and key not in bin_keys for key, (q, v) in positions.items()):
		refuse()
	stock_totals = {account: Decimal(0) for account in accounts}
	for (_item, wh), (_qty, value) in positions.items():
		stock_totals[wh_account[wh]] += value
	gl_totals = {account: Decimal(0) for account in accounts}
	for row in data["gl"]:
		if not row.is_cancelled and row.account in gl_totals:
			if str(row.posting_date) > as_of:
				refuse()
			gl_totals[row.account] += number(row.debit) - number(row.credit)
	if any(abs(stock_totals[a] - gl_totals[a]) > MONEY_EPS for a in accounts):
		refuse()
	return leaf


def collect(run, config, *, review_execution_user=None):
	"""One complete fresh source; no source rows enter a tool result or transcript."""
	scope = config["scope"]
	if min(scope["to_date"], scope["report_date"]) != frappe.utils.today():
		refuse(_STALE)
	with trust.verified_source(
		config["negative_stock_run"], run, review_execution_user=review_execution_user
	):
		company = scope["company"]
		if int(frappe.db.get_single_value("Stock Settings", "allow_negative_stock") or 0):
			refuse()
		data = {
			"company": readable_rows(
				"Company",
				["name", "default_currency", "enable_perpetual_inventory", "default_inventory_account"],
				{"name": company},
			),
			"warehouses": readable_rows(
				"Warehouse",
				["name", "company", "account", "is_group", "disabled", "lft", "rgt"],
				{"company": company},
			),
			"accounts": readable_rows(
				"Account",
				["name", "account_type", "is_group", "disabled", "account_currency"],
				{"company": company},
			),
			"items": readable_rows(
				"Item",
				[
					"name",
					"disabled",
					"end_of_life",
					"is_stock_item",
					"has_variants",
					"variant_of",
					"allow_negative_stock",
					"stock_uom",
					"min_order_qty",
				],
			),
			"ledger": readable_rows(
				"Stock Ledger Entry",
				[
					"name",
					"item_code",
					"warehouse",
					"actual_qty",
					"qty_after_transaction",
					"stock_value",
					"stock_value_difference",
					"valuation_rate",
					"incoming_rate",
					"posting_date",
					"posting_time",
					"creation",
					"is_cancelled",
					"is_adjustment_entry",
					"voucher_type",
				],
				{"company": company},
			),
			"gl": readable_rows(
				"GL Entry",
				["name", "account", "posting_date", "debit", "credit", "is_cancelled"],
				{"company": company},
			),
			"reposts": readable_rows("Repost Item Valuation", ["name", "status"], {"company": company}),
			"landed_costs": readable_rows("Landed Cost Voucher", ["name", "docstatus"], {"company": company}),
			"uoms": readable_rows("UOM", ["name", "must_be_whole_number"]),
			"requests": readable_rows(
				"Material Request",
				["name", "docstatus", "status", "material_request_type"],
				{"company": company, "docstatus": ["<", 2]},
			),
			"orders": readable_rows(
				"Purchase Order", ["name", "docstatus", "status"], {"company": company, "docstatus": ["<", 2]}
			),
		}
		if len(data["company"]) != 1:
			refuse()
		from erpnext.stock.doctype.bin.bin import RECALCULATED_FIELDS

		data["bins"] = readable_rows(
			"Bin",
			["name", "item_code", "warehouse", "projected_qty", *RECALCULATED_FIELDS],
			{"warehouse": ["in", [w.name for w in data["warehouses"]]]},
		)
		data["policies"] = children(
			"Item Reorder",
			[i.name for i in data["items"]],
			[
				"name",
				"parent",
				"warehouse",
				"warehouse_group",
				"warehouse_reorder_level",
				"warehouse_reorder_qty",
				"material_request_type",
			],
		)
		data["request_items"] = children(
			"Material Request Item",
			[r.name for r in data["requests"]],
			["name", "parent", "item_code", "warehouse", "stock_qty", "ordered_qty"],
		)
		data["order_items"] = children(
			"Purchase Order Item",
			[r.name for r in data["orders"]],
			["name", "parent", "item_code", "warehouse", "qty", "received_qty", "conversion_factor"],
		)
		if sum(len(rows) for rows in data.values()) > MAX_ROWS:
			refuse()
		leaf = native_stock_guard(data, scope)
		rows = derive(data, leaf, config)
		from erpnext.stock.stock_balance import get_indented_qty, get_ordered_qty

		bins = {(r.item_code, r.warehouse): r for r in data["bins"]}
		for row in rows:
			key = row["item"], row["warehouse"]
			ordered, indented = number(get_ordered_qty(*key)), number(get_indented_qty(*key))
			bin_row = bins.get(key)
			if (
				abs(ordered - number(bin_row.ordered_qty if bin_row else 0)) > EPS
				or abs(indented - number(bin_row.indented_qty if bin_row else 0)) > EPS
			):
				refuse()
		return {"source_digest": seal({"config": config, "source": data}), "rows": rows}


def derive(data, leaf, config):
	items = {r.name: r for r in data["items"]}
	bins = {(r.item_code, r.warehouse): r for r in data["bins"]}
	uoms = {r.name: r.must_be_whole_number for r in data["uoms"]}
	drafts = {r.name for r in data["requests"] if r.docstatus == 0} | {
		r.name for r in data["orders"] if r.docstatus == 0
	}
	draft_keys = {
		(r.item_code, r.warehouse)
		for r in [*data["request_items"], *data["order_items"]]
		if r.parent in drafts
	}
	counts = {}
	for p in data["policies"]:
		key = p.parent, p.warehouse
		counts[key] = counts.get(key, 0) + 1
	rows = []
	seen = set()
	warehouse_names = {w.name for w in data["warehouses"]}
	for policy in data["policies"]:
		item = items.get(policy.parent)
		if (
			not item
			or policy.warehouse not in warehouse_names
			or not item.is_stock_item
			or item.disabled
			or (
				item.end_of_life
				and str(item.end_of_life) != "0000-00-00"
				and str(item.end_of_life) < frappe.utils.today()
			)
		):
			continue
		key = item.name, policy.warehouse
		projected = number(bins[key].projected_qty) if key in bins else Decimal(0)
		level, qty = number(policy.warehouse_reorder_level), number(policy.warehouse_reorder_qty)
		if level < 0 or qty < 0:
			refuse()
		if projected > level or (level == 0 and qty == 0):
			continue
		reason = ""
		if counts[key] != 1:
			reason = "conflicting_policy"
		elif (
			(policy.warehouse_group and policy.warehouse_group != policy.warehouse)
			or item.variant_of
			or item.has_variants
			or policy.warehouse not in leaf
		):
			reason = "group_or_variant_policy"
		elif policy.material_request_type != "Purchase":
			reason = "non_purchase_policy"
		elif key in draft_keys:
			reason = "draft_supply_exists"
		proposed = max(qty, level - projected, number(item.min_order_qty or 0))
		if item.stock_uom not in uoms:
			refuse()
		if uoms[item.stock_uom]:
			proposed = proposed.to_integral_value(rounding=ROUND_CEILING)
		if proposed <= 0:
			continue
		row = {
			"id": seal({"item": item.name, "warehouse": policy.warehouse})[:24],
			"item": item.name,
			"warehouse": policy.warehouse,
			"stock_uom": item.stock_uom,
			"projected_qty": None
			if policy.warehouse not in leaf or item.has_variants
			else format(projected.normalize(), "f"),
			"reorder_level": format(level.normalize(), "f"),
			"reorder_qty": format(qty.normalize(), "f"),
			"proposed_qty": None if reason else format(proposed.normalize(), "f"),
			"status": "Held" if reason else "Pending",
			"reason": reason,
		}
		if row["id"] not in seen:
			seen.add(row["id"])
			rows.append(row)
			if len(rows) > config["max_proposals"]:
				refuse(
					"The review exceeds the saved row limit. Check the policies or raise the limit and run again."
				)
	rows.sort(key=lambda r: (r["warehouse"], r["item"]))
	if len(rows) > config["max_proposals"]:
		refuse("The review exceeds the saved row limit. Check the policies or raise the limit and run again.")
	return rows


def load_state(run):
	try:
		envelope = json.loads(run.get("assessment_output_json") or "{}")
		state = envelope["state"]
		if (
			state["kind"] != STATE
			or state["run"] != run.name
			or not hmac.compare_digest(envelope["seal"], seal(state))
		):
			refuse(_STATE)
		return state
	except (KeyError, TypeError, ValueError):
		refuse(_STATE)


def store_state(run, state):
	frappe.db.set_value(
		RUN,
		run.name,
		"assessment_output_json",
		canonical({"state": state, "seal": seal(state)}),
		update_modified=False,
	)


def locked_run(name):
	# A locking read must supply the state itself. A later ordinary SELECT can
	# otherwise re-use a request's older repeatable-read snapshot after this lock.
	rows = frappe.db.sql(
		"select * from `tabJarvis Agent Run` where name=%s for update", (name,), as_dict=True
	)
	if not rows:
		refuse(_STATE)
	return frappe.get_doc({"doctype": RUN, **rows[0]})


def summary(state):
	return {
		"run": state["run"],
		"state": "preview" if state["mode"] == "shadow" else "prepared",
		"proposal_count": sum(r["status"] != "Held" for r in state["rows"]),
		"held_count": sum(r["status"] == "Held" for r in state["rows"]),
		"review_page": "/jarvis/reorder-reviews",
	}


def prepare(run):
	run = locked_run(run.name)
	if (
		not modern_run(run)
		or run.get("preparation_mode") not in {"shadow", "live"}
		or trust._session_user(run) != frappe.session.user
	):
		refuse(_STATE)
	if run.get("assessment_output_json"):
		return summary(load_state(run))
	if run.status != "running":
		refuse(_STATE)
	config = json.loads(run.assessment_config_json)
	if not has_jarvis_access(config["reviewer"]):
		refuse(_REVIEW_ACCESS)
	if run.scope_json is None or json.loads(run.scope_json) != config["scope"]:
		refuse(_INVALID)
	current = collect(run, config)
	with impersonate(config["reviewer"]):
		if collect(run, config)["source_digest"] != current["source_digest"]:
			refuse(_ACCESS)
	state = {
		"kind": STATE,
		"run": run.name,
		"mode": run.preparation_mode,
		"reviewer": config["reviewer"],
		"execution_user": frappe.session.user,
		"reviewer_profile": trust._live_profile_fingerprint(config["reviewer"]),
		"config": config,
		**current,
	}
	store_state(run, state)
	frappe.db.set_value(
		RUN,
		run.name,
		{
			"status": "completed",
			"finished_at": frappe.utils.now_datetime(),
			"findings_count": 0,
			"result_state": "partial"
			if any(r["status"] == "Held" for r in current["rows"])
			else "evaluated_clean",
			"coverage_note": f"{sum(r['status'] == 'Held' for r in current['rows'])} saved policies need manual action. Open Reorder reviews."
			if any(r["status"] == "Held" for r in current["rows"])
			else "",
		},
		update_modified=False,
	)
	from jarvis.chat.agent_run_steps import record_step
	from jarvis.chat.agent_runs import teardown_run_session

	record_step(
		run.name,
		kind="writeback",
		label="Reorder review prepared",
		detail=f"{summary(state)['proposal_count']} suggestions; {summary(state)['held_count']} policies need manual action. No ERP request was created.",
		owner=run.owner,
	)

	frappe.db.after_commit.add(lambda: teardown_run_session(run.get("session_key")))
	return summary(state)


def dispatch(session_key, tool, args, *, cap, result_mode=None):
	"""Refuse model text before the generic dispatcher can retain arguments."""
	run = None
	try:
		run = locked_run(cap.get("run"))
		if run.agent == AGENT and not modern_run(run):
			from jarvis.chat.reorder_replenishment_gate import RUN_UNAVAILABLE

			if run.status == "running":
				frappe.db.set_value(
					RUN,
					run.name,
					{
						"status": "failed",
						"finished_at": frappe.utils.now_datetime(),
						"error": RUN_UNAVAILABLE,
					},
					update_modified=False,
				)
				from jarvis.chat.agent_runs import teardown_run_session

				frappe.db.after_commit.add(lambda: teardown_run_session(run.get("session_key")))
			return {"ok": False, "error": {"code": "CapabilityDeniedError", "message": RUN_UNAVAILABLE}}
		if (
			not modern_run(run)
			or run.session_key != session_key
			or not frappe.db.get_value("User", frappe.session.user, "enabled")
		):
			refuse(_STATE)
		if tool != TOOL or result_mode is not None:
			return {
				"ok": False,
				"error": {
					"code": "PermissionError",
					"message": "Only native Reorder preparation is allowed.",
				},
			}
		if isinstance(args, str):
			if len(args) >= 4096:
				refuse(_STATE)
			args = json.loads(args)
		if args not in ({}, None):
			refuse(_STATE)
		frappe.db.savepoint("reorder_prepare")
		try:
			return {"ok": True, "data": prepare(run)}
		except Exception:
			frappe.db.rollback(save_point="reorder_prepare")
			raise
	except Exception as exc:
		message = (
			str(exc)
			if isinstance(exc, frappe.ValidationError)
			and str(exc) in {_INVALID, _SOURCE, _STALE, _ACCESS, _STATE, _UNSUPPORTED, _REVIEW_ACCESS}
			else _SOURCE
		)
		try:
			if run and run.agent == AGENT and run.status == "running":
				frappe.db.set_value(
					RUN,
					run.name,
					{"status": "failed", "finished_at": frappe.utils.now_datetime(), "error": message},
					update_modified=False,
				)
				from jarvis.chat.agent_runs import teardown_run_session

				frappe.db.after_commit.add(lambda: teardown_run_session(run.get("session_key")))
		except Exception:
			frappe.logger("jarvis.reorder_review").warning({"event": "reorder_terminal_state_unavailable"})
		return {"ok": False, "error": {"code": "ReorderUnavailable", "message": message}}
	finally:
		# Frappe sends message_log beside the returned envelope. Source errors
		# must not escape through that second response channel.
		frappe.local.message_log = []
		for key in ("exc", "exception", "exc_type", "_server_messages"):
			frappe.local.response.pop(key, None)


def reviewer_state(name, *, lock=False):
	run = locked_run(name) if lock else frappe.get_doc(RUN, name)
	if run.agent != AGENT:
		refuse(_STATE)
	state = load_state(run)
	if state["reviewer"] != frappe.session.user:
		frappe.throw("Only the named Reorder reviewer can use this control.", frappe.PermissionError)
	if state.get("reviewer_profile") != trust._live_profile_fingerprint(frappe.session.user):
		frappe.throw(_ACCESS, frappe.PermissionError)
	if not frappe.has_permission("Bin", "read"):
		frappe.throw(_ACCESS, frappe.PermissionError)
	require_fields("Bin", ["projected_qty"])
	require_fields("Item", ["stock_uom", "min_order_qty"])
	require_fields("Item Reorder", ["warehouse_reorder_level", "warehouse_reorder_qty"], parenttype="Item")
	for doctype, names in (
		("Company", {state["config"]["scope"]["company"]}),
		("Warehouse", {r["warehouse"] for r in state["rows"]}),
		("Item", {r["item"] for r in state["rows"]}),
	):
		if any(not frappe.has_permission(doctype, "read", doc=n) for n in names):
			frappe.throw(_ACCESS, frappe.PermissionError)
	for row in state["rows"]:
		bin_name = frappe.db.get_value(
			"Bin", {"item_code": row["item"], "warehouse": row["warehouse"]}, "name"
		)
		if bin_name and not frappe.has_permission("Bin", "read", doc=bin_name):
			frappe.throw(_ACCESS, frappe.PermissionError)
	return run, state


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def get_review(name: str):
	refuse_in_tool_dispatch()
	_run, state = reviewer_state(name)
	scope = state["config"]["scope"]
	as_of = min(scope["to_date"], scope["report_date"])
	return {
		**{k: state[k] for k in ("run", "mode", "reviewer", "execution_user", "rows")},
		"company": scope["company"],
		"as_of": as_of,
		"current": as_of == frappe.utils.today(),
	}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def list_reviews(start: int = 0):
	refuse_in_tool_dispatch()
	if type(start) is not int or not 0 <= start <= 100_000:
		refuse(_STATE)
	names = frappe.get_all(
		RUN,
		filters={"agent": AGENT, "status": "completed", "assessment_output_json": ["is", "set"]},
		pluck="name",
		order_by="creation desc, name desc",
		start=start,
		page_length=26,
	)
	rows = []
	for name in names[:25]:
		try:
			rows.append(get_review(name))
		except (frappe.PermissionError, frappe.ValidationError, frappe.DoesNotExistError):
			continue
	return {"rows": rows, "has_more": len(names) > 25, "next_start": start + min(25, len(names))}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def decide(name: str, row_id: str, approve: int):
	refuse_in_tool_dispatch()
	if type(approve) is not int or approve not in (0, 1):
		refuse(_STATE)
	run, state = reviewer_state(name, lock=True)
	row = next((r for r in state["rows"] if r["id"] == row_id), None)
	decision = "Approved" if approve else "Rejected"
	if state["mode"] != "live" or not row or row["status"] == "Held":
		refuse(_STATE)
	if row["status"] == decision:
		return {"ok": True, "status": decision, "idempotent": True}
	if row["status"] != "Pending":
		refuse(_STATE)
	if approve:
		# The run bearer is removed after preparation. The recorded execution user
		# remains authoritative; the reviewer never becomes a replacement run-as.
		if (
			collect(run, state["config"], review_execution_user=state["execution_user"])["source_digest"]
			!= state["source_digest"]
		):
			refuse(_STALE)
	row.update(status=decision, decided_by=frappe.session.user, decided_at=frappe.utils.now())
	store_state(run, state)
	return {"ok": True, "status": decision, "idempotent": False}
