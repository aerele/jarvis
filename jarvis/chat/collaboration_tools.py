"""Closed shared-tool policy: permission-checked reads/artifacts or explicit writes.

New registry tools must be classified before they can run in shared sessions.
Private memory, unattended runs and tenant-wide assistant authoring are excluded.
"""

import json

import frappe

READ_TOOLS = frozenset(
	"""
describe_customizations download_pdf download_vcard export_document export_excel
export_query find_skills get_balance_on get_bank_recon_inputs get_comments
get_creation_context get_customer_outstanding get_doc get_employee_shift
get_engagement_config get_exchange_rate get_file_pages get_fiscal_year
get_gstr1_summary get_holidays_for_employee get_itemised_tax_breakup
get_leave_balance_on get_leave_details get_leaves_for_period get_linked_docs
get_list get_my_access get_naming_series_preview get_party_dashboard_info
get_purchase_match_inputs get_receivables_review_inputs get_report_filters
get_schema get_skill get_stock_balance get_submitted_linked_docs get_valuation_rate
get_workflow_transitions list_app_modules list_connector_actions preview_doc
preview_import preview_landed_cost_voucher query query_file query_report read_app_source read_file read_wiki report_pdf resolve_links
run_report scan_barcode summarize_dataset validate_script
""".split()
)
WRITE_TOOLS = frozenset(
	"""
add_comment add_tag amend_doc apply_workflow_action assign_to attach_to_doc
call_connector cancel_doc create_doc delete_doc follow_document remove_tag
run_import run_method send_email share_doc submit_doc unassign_from
unfollow_document unshare_doc update_comment update_doc
""".split()
)
REFUSED_TOOLS = frozenset(
	"""
create_custom_skill create_dashboard create_dashboard_chart finish_app_learning_run
recall record_agent_run record_app_wiki remember save_agent_dashboard
save_dashboard schedule_run update_wiki
""".split()
)


def tool_kind(name: str) -> str:
	if name in READ_TOOLS:
		return "read"
	if name in WRITE_TOOLS:
		return "write"
	return "refused"


def validate_targets(tool: str, args: dict):
	"""Keep raw private Jarvis state out of generic ERP tools and RPC wrappers.

	Purpose-built business/wiki/skill reads retain their own scope checks. The
	generic document/query/RPC tools must not turn recall's refusal into a direct
	read of the memory store, another private conversation or user preferences.
	Text fields are text: a JSON example in a Note is not a document target.
	"""

	def refuse():
		frappe.throw("Private Jarvis context is unavailable in shared chats.", frappe.PermissionError)

	if tool == "run_method" and isinstance(args, dict) and isinstance(args.get("method"), str):
		from jarvis.tools.run_method import _call_paths

		if any(path.casefold().startswith("jarvis.") for path in _call_paths(args["method"])):
			refuse()
	selectors = {
		"doctype",
		"dt",
		"document_type",
		"reference_doctype",
		"ref_doctype",
		"for_doctype",
		"parenttype",
		"share_doctype",
		"attached_to_doctype",
	}
	work, seen, visited = [(args, "args")], set(), 0
	while work:
		value, key = work.pop()
		visited += 1
		if visited > 16384:
			refuse()
		if isinstance(value, str):
			if key in selectors or (tool == "query" and key == "from"):
				name = value.strip().casefold().replace("_", " ")
				if name.startswith(("jarvis ", "tabjarvis ")):
					refuse()
			elif key in {"method", "cmd", "endpoint"} and value.strip().casefold().startswith("jarvis."):
				refuse()
			elif key in {"doc", "docs", "args", "spec"} and value.lstrip().startswith(("{", "[")):
				try:
					work.append((json.loads(value), key))
				except (ValueError, RecursionError):
					refuse()
		elif isinstance(value, (dict, list)):
			if id(value) in seen:
				continue
			seen.add(id(value))
			if isinstance(value, dict):
				work.extend((child, str(child_key)) for child_key, child in value.items())
			else:
				work.extend((child, "item") for child in value)
