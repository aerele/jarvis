"""Shared operator evidence custody; private bundles retain all assessment predicates."""

from html import escape

import frappe

from jarvis.chat.purchase_match import digest

AGENTS = frozenset({"ap-3way-match-operator", "ar-collections-operator"})


def backend(agent):
	if agent == "ap-3way-match-operator":
		from jarvis.chat import purchase_match

		return purchase_match
	if agent == "ar-collections-operator":
		from jarvis.chat import receivables_review

		return receivables_review
	return None


def get_inputs(run, adapter):
	state = frappe.db.get_value(
		"Jarvis Agent Run",
		run.name,
		["agent", "status", "input_snapshot_json"],
		as_dict=True,
		for_update=True,
	)
	if not state or state.agent != adapter.AGENT or state.status != "running":
		frappe.throw(
			"Review evidence is available only to the active review delegate.", frappe.PermissionError
		)
	config = frappe.parse_json(run.assessment_config_json or "{}")
	scope = frappe.parse_json(run.scope_json or "{}")
	if not config or scope != adapter.scope_from_config(config):
		frappe.throw("This run has no valid launch-time review scope and policy; launch a new run.")
	current = adapter.collect(scope, config)
	stored = frappe.parse_json(state.input_snapshot_json or "null")
	if stored:
		if stored["source_digest"] != current["source_digest"]:
			frappe.throw("Review evidence or permissions changed since capture; launch a new run.")
		return stored
	current["captured_at"] = frappe.utils.now()
	current["captured_by"] = frappe.session.user
	frappe.db.set_value(
		"Jarvis Agent Run", run.name, "input_snapshot_json", frappe.as_json(current), update_modified=False
	)
	run.input_snapshot_json = frappe.as_json(current)
	return current


def stage_reviews(run, inst, findings, adapter):
	from jarvis._session import impersonate

	if not findings:
		return []
	if not frappe.has_permission("Jarvis Approval Request", "create"):
		frappe.throw("Live review requires permission to create Approval Requests.", frappe.PermissionError)
	stored = frappe.parse_json(run.input_snapshot_json)
	reviewer = inst.reviewer
	if not reviewer or not frappe.db.get_value("User", reviewer, "enabled"):
		frappe.throw("An enabled, named reviewer is required.")
	with impersonate(reviewer):
		if adapter.collect(stored["scope"], stored["config"])["source_digest"] != stored["source_digest"]:
			frappe.throw(
				"The named reviewer cannot read the complete assessment evidence.", frappe.PermissionError
			)
	requests = []
	for finding in findings:
		evidence = {
			"run": run.name,
			"input_digest": stored["source_digest"],
			"scope": stored["scope"],
			"policy": stored["config"],
			"reviewer": reviewer,
			"finding": finding,
		}
		key = digest(
			{
				"installation": inst.name,
				**{field: value for field, value in evidence.items() if field != "run"},
			}
		)
		name = frappe.db.get_value("Jarvis Approval Request", {"assessment_key": key}, "name")
		if name:
			requests.append(name)
			continue
		context = adapter.review_context(stored, finding)
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Approval Request",
				"title": f"{adapter.LABEL}: {finding['ref_name']}",
				"source": "Agent Review",
				"status": "Pending",
				"document_type": finding["ref_doctype"],
				"conversation": run.conversation,
				"ref_doctype": finding["ref_doctype"],
				"ref_name": finding["ref_name"],
				"agent": run.agent,
				"run": run.name,
				"preparation_mode": run.preparation_mode,
				"result_class": "derived_candidate",
				"assessment_key": key,
				"assessment_evidence": frappe.as_json(evidence),
				"question": adapter.REVIEW_QUESTION,
				"context_md": "<pre>"
				+ escape(context)
				+ "</pre>\n\nEvidence digest: "
				+ stored["source_digest"],
				"options": "[]",
			}
		)
		doc.flags.jarvis_server_write = True
		doc.insert()
		frappe.share.add(
			"Jarvis Approval Request",
			doc.name,
			user=reviewer,
			read=1,
			flags={"ignore_share_permission": True},
		)
		requests.append(doc.name)
	return requests


def may_review(doc):
	evidence = frappe.parse_json(doc.get("assessment_evidence") or "{}")
	return evidence.get("reviewer") == frappe.session.user


def check_review(doc, approve, adapter=None):
	adapter = adapter or backend(doc.agent)
	if adapter is None:
		frappe.throw("Unknown review authority.", frappe.PermissionError)
	if not may_review(doc):
		frappe.throw("Only the named reviewer may dispose of this assessment.", frappe.PermissionError)
	if approve:
		evidence = frappe.parse_json(doc.assessment_evidence)
		current = adapter.collect(evidence["scope"], evidence["policy"])
		if current["source_digest"] != evidence["input_digest"]:
			frappe.throw(
				"This review is stale or no longer fully readable. Re-run the assessment; no posting is authorised."
			)
