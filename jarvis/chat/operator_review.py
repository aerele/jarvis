"""Shared operator evidence custody; private bundles retain all assessment predicates."""

from html import escape

import frappe

from jarvis.chat import chat_permissions
from jarvis.chat.purchase_match import digest

_BACKENDS = {
	"ap-3way-match-operator": "jarvis.chat.purchase_match",
	"ar-collections-operator": "jarvis.chat.receivables_review",
	"bank-recon-operator": "jarvis.chat.bank_reconciliation_review",
	"cycle-count-planner-operator": "jarvis.chat.cycle_count_review",
}
AGENTS = frozenset(_BACKENDS)
_FRESH_SNAPSHOT_AGENT = "cycle-count-planner-operator"


def backend(agent):
	path = _BACKENDS.get(agent)
	return frappe.get_module(path) if path else None


def _collect_current(adapter, scope, config):
	"""Collect review evidence from the right freshness boundary for an adapter.

	Cycle Count compares live stock, movement, and submitted-count evidence to a
	sealed result.  Its source read must start after any earlier permission or
	row read in this request, otherwise MariaDB repeatable-read can quietly reuse
	an old snapshot.  Other review adapters keep their established collection
	contract.
	"""
	if adapter.AGENT == _FRESH_SNAPSHOT_AGENT:
		with adapter.fresh_source_snapshot():
			return adapter.collect(scope, config)
	return adapter.collect(scope, config)


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
	stored = frappe.parse_json(state.input_snapshot_json or "null")
	# An incomplete source is the terminal, typed result for this run. In
	# particular, a byte-boundary failure intentionally has a scrubbed digest,
	# so recollecting its original complete source could never match it.
	if isinstance(stored, dict) and stored.get("complete") is False:
		return stored
	current = _collect_current(adapter, scope, config)
	if stored:
		if stored["source_digest"] != current["source_digest"]:
			frappe.throw("Review evidence or permissions changed since capture; launch a new run.")
		return stored
	current["captured_at"] = frappe.utils.now()
	current["captured_by"] = frappe.session.user
	# A review adapter can own a protected one-file source contract. Run its
	# final byte check after server capture metadata is present, but before this
	# payload reaches persistent storage. Older adapters have no such contract.
	if callable(enforce_envelope := getattr(adapter, "enforce_private_input_envelope", None)):
		current = enforce_envelope(current)
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
	if not chat_permissions.has_approval_permission(
		frappe._dict(conversation=run.conversation), ptype="create"
	):
		frappe.throw("Live review requires access to the linked conversation.", frappe.PermissionError)
	stored = frappe.parse_json(run.input_snapshot_json)
	# The reviewer is sealed with the launch policy. A later installation edit
	# must not change the person who can receive or decide an in-flight review.
	# Cycle Count seals a named reviewer in the launch config. Older review
	# agents keep their installation reviewer because their config has no such
	# field. This preserves their existing review contract.
	reviewer = stored["config"].get("reviewer") or inst.reviewer
	if not reviewer or not frappe.db.get_value("User", reviewer, "enabled"):
		frappe.throw("An enabled, named reviewer is required.")
	with impersonate(reviewer):
		if (
			_collect_current(adapter, stored["scope"], stored["config"])["source_digest"]
			!= stored["source_digest"]
		):
			frappe.throw(
				"The named reviewer cannot read the complete assessment evidence.", frappe.PermissionError
			)
	prepared = []
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
		prepared.append((finding, evidence, key))

	# Check every existing request before making any change. A matching key may
	# belong to another conversation because the key deliberately identifies the
	# assessment, not the run that produced it.
	existing = {}
	for _finding, evidence, key in prepared:
		if key in existing:
			continue
		name = frappe.db.get_value("Jarvis Approval Request", {"assessment_key": key}, "name")
		if not name:
			existing[key] = None
			continue
		prior = frappe.db.get_value(
			"Jarvis Approval Request",
			name,
			["run", "status", "assessment_evidence", "conversation"],
			as_dict=True,
			for_update=True,
		)
		if not prior:
			existing[key] = None
			continue
		if not chat_permissions.has_approval_permission(
			frappe._dict(name=name, conversation=prior.conversation), ptype="create"
		):
			frappe.throw("Live review requires access to the linked conversation.", frappe.PermissionError)
		if not prior.assessment_evidence:
			original = frappe.parse_json(
				frappe.db.get_value("Jarvis Agent Run", prior.run, "input_snapshot_json") or "{}"
			)
			if prior.status != "Pending" or original.get("source_digest") != stored["source_digest"]:
				frappe.throw(
					"The saved review has no matching source evidence; an administrator must check it."
				)
			# Keep the original run when repairing a request from the same sealed evidence.
			prior.repair_evidence = frappe.as_json({**evidence, "run": prior.run})
		existing[key] = frappe._dict(name=name, prior=prior)

	# Context rendering can reject a malformed finding. Do it before the write
	# phase so one bad row cannot leave another staged request behind.
	contexts = {}
	for finding, _evidence, key in prepared:
		if existing[key] is None and key not in contexts:
			contexts[key] = adapter.review_context(stored, finding)

	requests = []
	for finding, evidence, key in prepared:
		if entry := existing[key]:
			name, prior = entry.name, entry.prior
			if not prior.assessment_evidence:
				frappe.db.set_value(
					"Jarvis Approval Request",
					name,
					{
						"assessment_evidence": prior.repair_evidence,
						"assessment_input_digest": stored["source_digest"],
					},
					update_modified=False,
				)
				prior.assessment_evidence = True
			requests.append(name)
			continue
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
				"assessment_input_digest": stored["source_digest"],
				"question": adapter.REVIEW_QUESTION,
				"context_md": "<pre>"
				+ escape(contexts[key])
				+ "</pre>\n\nEvidence digest: "
				+ stored["source_digest"],
				"options": "[]",
			}
		)
		doc.flags.jarvis_server_write = True
		# Create permission is checked above. Keep the server-owned evidence at permlevel 1.
		doc.insert(ignore_permissions=True)
		frappe.share.add(
			"Jarvis Approval Request",
			doc.name,
			user=reviewer,
			read=1,
			flags={"ignore_share_permission": True},
		)
		existing[key] = frappe._dict(name=doc.name, prior=frappe._dict(assessment_evidence=True))
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
		current = _collect_current(adapter, evidence["scope"], evidence["policy"])
		if current["source_digest"] != evidence["input_digest"]:
			frappe.throw(
				"This review is stale or no longer fully readable. Re-run the assessment; no posting is authorised."
			)
