"""Direct SPA/PWA actions for the admin "User activity" report.

One metadata-only ``action`` / ``read`` / ``export`` event per Jarvis endpoint call a
person makes in the desktop SPA or the PWA, written by a pair of request hooks (plan:
``.claude/workflow/plans/2026-11-06-user-activity-report.md`` 4.4):

- ``capture_request`` (``before_request``) snapshots WHICH method the request names and a
  reference to the ORIGINAL ``form_dict``. It has to run before dispatch because
  ``frappe.set_user`` (frappe/__init__.py ``set_user``; v15 and v16) REPLACES
  ``local.form_dict``, and ``jarvis._session.impersonate`` calls it on entry and exit, so an
  ``after_request`` that read ``form_dict`` would find ``cmd`` and the id parameter gone.
- ``record_request`` (``after_request``) resolves what actually ran, looks it up in
  ``CLASSIFICATION`` and, for a LOGGED endpoint, inserts one event and commits its own row.

``CLASSIFICATION`` is the contract: every ``@frappe.whitelist`` function under ``jarvis/`` has
exactly one entry (``tests/test_activity_contract.py`` scans the source and fails otherwise),
keyed by the function's canonical name ``fn.__module__ + "." + fn.__qualname__``. It must stay
a PURE LITERAL (no calls, no names): the contract test reads it with ``ast`` and imports
nothing.

    ("LOGGED", id_param, event, ref_doctype, rows)
    ("SKIPPED", reason)

- ``event``: ``action`` (a change or an operator command), ``read`` (ERP data the call hands
  back without going through the tool path, D15) or ``export`` (a download).
- ``ref_doctype``: a DocType name, or ``"@param"`` to take it from that request parameter
  (kept only when it names a real DocType or Report). ``id_param``: the parameter holding the
  record's name; stored as ``ref_name`` ONLY when it names an existing record of
  ``ref_doctype``. Nothing else the caller sent is ever stored.
- ``rows``: ``row_count`` to stamp on a successful call (a single-document read is one row).
- SKIPPED reasons: ``read`` (a lookup that returns no ERP data: lists of our own rows, UI
  metadata), ``poll`` (status polls), ``ui_state`` (preferences, dismissals, feedback),
  ``chat`` (message traffic, already counted as turns), ``tool_path`` (the call runs a tool, so
  the tool path already records it: logging it here would double count), ``desk`` (Desk-only,
  D5: Desk is not tracked), ``self`` (this report and the error reporter), ``auth`` (plugin
  and device endpoints that authenticate by signature).

An endpoint that is ambiguous is LOGGED. A new whitelisted function fails the contract test
until it is classified here. The runbook (how to classify a new endpoint, what the contract
test enforces, how to debug a missing row) is the "App actions" section of the
``jarvis.activity_rollup`` module docstring.

Flood control (all three fail OPEN: a Redis error records the row rather than losing it). It
applies only to ``read`` rows and to denied / failed outcomes: a successful ``action`` or
``export`` is always recorded.

- a response that proves the function never ran (400, 404, 405, 415, plus 401 and 429) is not
  recorded;
- an identical (actor, method, ref_doctype, ref_name, outcome) inside ``DEDUPE_WINDOW_S`` is one row;
- one person gets at most ``ACTOR_CAP_PER_MINUTE`` rows a minute.

Health: every LOGGED request the hook sees, and every hook failure, bumps an hourly Redis
counter (``health_counters``) that the report's ``logging_health.app_warn`` compares with the
rows actually written.
"""

import hashlib
import json
import re
import time
import types

import frappe
import frappe.handler

from jarvis import activity_log

# One line per endpoint: the registry is reviewed as a diff, and read as literal source by the contract test.
# Add new endpoints here, alphabetical by key. When it is ambiguous, it is LOGGED.
# fmt: off
CLASSIFICATION = {
	"jarvis.account.cancel_plan_at_period_end": ("LOGGED", None, "action", None, 0),
	"jarvis.account.cancel_scheduled_downgrade": ("LOGGED", None, "action", None, 0),
	"jarvis.account.check_billing_payment_status": ("SKIPPED", "poll"),
	"jarvis.account.download_invoice": ("LOGGED", None, "export", None, 0),
	"jarvis.account.get_account": ("SKIPPED", "read"),
	"jarvis.account.get_billing_payment_state": ("SKIPPED", "read"),
	"jarvis.account.get_billing_profile": ("SKIPPED", "read"),
	"jarvis.account.get_invoices": ("SKIPPED", "read"),
	"jarvis.account.get_llm_apply_operation": ("SKIPPED", "poll"),
	"jarvis.account.get_llm_connection_health": ("SKIPPED", "read"),
	"jarvis.account.get_llm_connection_status": ("SKIPPED", "read"),
	"jarvis.account.get_llm_usage": ("SKIPPED", "read"),
	"jarvis.account.get_subscription_status": ("SKIPPED", "read"),
	"jarvis.account.is_onboarded": ("SKIPPED", "read"),
	"jarvis.account.is_ready_for_chat": ("SKIPPED", "poll"),
	"jarvis.account.preview_downgrade": ("SKIPPED", "read"),
	"jarvis.account.preview_upgrade": ("SKIPPED", "read"),
	"jarvis.account.reauthorize_autopay": ("LOGGED", None, "action", None, 0),
	"jarvis.account.resume_plan": ("LOGGED", None, "action", None, 0),
	"jarvis.account.start_downgrade": ("LOGGED", None, "action", None, 0),
	"jarvis.account.start_upgrade": ("LOGGED", None, "action", None, 0),
	"jarvis.account.stop_autopay_to_pay": ("LOGGED", None, "action", None, 0),
	"jarvis.account.update_billing_details": ("LOGGED", None, "action", None, 0),
	"jarvis.activity_report.get_user_activity": ("SKIPPED", "self"),
	"jarvis.api.call_tool": ("SKIPPED", "auth"),
	"jarvis.api.pairing_challenge": ("SKIPPED", "auth"),
	"jarvis.api.rotate_agent_token": ("SKIPPED", "desk"),
	"jarvis.api.terminalize_unusable_payroll_deposit_private_source": ("SKIPPED", "auth"),
	"jarvis.api.vouch_session": ("SKIPPED", "auth"),
	"jarvis.api_errors.report_client_errors": ("SKIPPED", "self"),
	"jarvis.branding.get_branding": ("SKIPPED", "read"),
	"jarvis.branding.update_branding": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.actions_api.apply_action": ("SKIPPED", "tool_path"),
	"jarvis.chat.actions_api.approve_and_run": ("SKIPPED", "tool_path"),
	"jarvis.chat.actions_api.confirm_tool": ("SKIPPED", "tool_path"),
	"jarvis.chat.actions_api.dismiss_draft": ("SKIPPED", "ui_state"),
	"jarvis.chat.actions_api.dismiss_tool": ("SKIPPED", "tool_path"),
	"jarvis.chat.actions_api.draft_computed": ("SKIPPED", "read"),
	"jarvis.chat.actions_api.get_doctype_form_meta": ("SKIPPED", "read"),
	"jarvis.chat.actions_api.list_pending_confirmations": ("SKIPPED", "read"),
	"jarvis.chat.actions_api.load_doc": ("LOGGED", "name", "read", "@doctype", 1),
	"jarvis.chat.actions_api.propose_next_action": ("SKIPPED", "tool_path"),
	"jarvis.chat.actions_api.rule_values": ("SKIPPED", "read"),
	"jarvis.chat.actions_api.save_draft_card": ("SKIPPED", "tool_path"),
	"jarvis.chat.admission.active_turn_for_conversation": ("SKIPPED", "poll"),
	"jarvis.chat.admission.cancel_queued_turn": ("SKIPPED", "chat"),
	"jarvis.chat.admission.pump_cutover_execute": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.admission.pump_cutover_preflight": ("SKIPPED", "read"),
	"jarvis.chat.admission.pump_set_transport_mode": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.admission.queue_position": ("SKIPPED", "poll"),
	"jarvis.chat.agent_models.get_agent_model": ("SKIPPED", "read"),
	"jarvis.chat.agent_models.get_eligible_models": ("SKIPPED", "read"),
	"jarvis.chat.agent_models.reset_agent_model": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.agent_models.set_agent_model": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.agents_api.apply_agents": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.agents_api.demote_installation": ("LOGGED", "installation", "action", "Jarvis Agent Installation", 0),
	"jarvis.chat.agents_api.get_agent": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.get_agent_admin_overview": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.get_agents_caps": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.get_agents_sync_status": ("SKIPPED", "poll"),
	"jarvis.chat.agents_api.get_ap_review_defaults": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.get_ar_review_defaults": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.get_bank_recon_review_defaults": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.get_cycle_count_review_defaults": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.get_installations": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.install_agent": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.agents_api.list_agent_activity_page": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.list_agents": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.list_agents_page": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.list_findings": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.list_run_steps": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.list_runs": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.list_runs_page": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.promote_installation": ("LOGGED", "installation", "action", "Jarvis Agent Installation", 0),
	"jarvis.chat.agents_api.raise_activation_ceiling": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.agents_api.run_agent_now": ("LOGGED", "installation", "action", "Jarvis Agent Installation", 0),
	"jarvis.chat.agents_api.search_users": ("SKIPPED", "read"),
	"jarvis.chat.agents_api.set_agent_access": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.agents_api.set_agent_roles": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.agents_api.set_config": ("LOGGED", "installation", "action", "Jarvis Agent Installation", 0),
	"jarvis.chat.agents_api.set_enabled": ("LOGGED", "installation", "action", "Jarvis Agent Installation", 0),
	"jarvis.chat.agents_api.set_finding_state": ("LOGGED", "finding", "action", "Jarvis Agent Finding", 0),
	"jarvis.chat.agents_api.set_listing_status": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.agents_api.set_operator_visibility": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.agents_api.set_run_as_user": ("LOGGED", "installation", "action", "Jarvis Agent Installation", 0),
	"jarvis.chat.agents_api.set_schedule": ("LOGGED", "installation", "action", "Jarvis Agent Installation", 0),
	"jarvis.chat.agents_api.stop_agent_run": ("LOGGED", "run", "action", "Jarvis Agent Run", 0),
	"jarvis.chat.agents_api.take_finding_to_chat": ("LOGGED", "finding", "action", "Jarvis Agent Finding", 0),
	"jarvis.chat.agents_api.uninstall_agent": ("LOGGED", "installation", "action", "Jarvis Agent Installation", 0),
	"jarvis.chat.api.archive_conversation": ("LOGGED", "conversation", "action", "Jarvis Conversation", 0),
	"jarvis.chat.api.clear_chat_history": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.api.compact_conversation": ("SKIPPED", "chat"),
	"jarvis.chat.api.create_conversation": ("SKIPPED", "chat"),
	"jarvis.chat.api.create_or_focus_empty": ("SKIPPED", "chat"),
	"jarvis.chat.api.get_canvas": ("SKIPPED", "read"),
	"jarvis.chat.api.get_chat_ui_settings": ("SKIPPED", "read"),
	"jarvis.chat.api.get_conversation": ("SKIPPED", "read"),
	"jarvis.chat.api.get_conversation_context": ("SKIPPED", "read"),
	"jarvis.chat.api.get_doctype_fields": ("SKIPPED", "read"),
	"jarvis.chat.api.get_model_catalog_ui": ("SKIPPED", "read"),
	"jarvis.chat.api.get_tool_activity": ("SKIPPED", "read"),
	"jarvis.chat.api.get_tool_detail": ("SKIPPED", "read"),
	"jarvis.chat.api.get_usage": ("SKIPPED", "read"),
	"jarvis.chat.api.list_conversations": ("SKIPPED", "read"),
	"jarvis.chat.api.list_tools": ("SKIPPED", "read"),
	"jarvis.chat.api.preview_file": ("SKIPPED", "read"),
	"jarvis.chat.api.rename_conversation": ("SKIPPED", "ui_state"),
	"jarvis.chat.api.retry_message": ("SKIPPED", "chat"),
	"jarvis.chat.api.search_conversations": ("SKIPPED", "read"),
	"jarvis.chat.api.search_workspace": ("SKIPPED", "read"),
	"jarvis.chat.api.send_message": ("SKIPPED", "chat"),
	"jarvis.chat.api.set_conversation_model": ("SKIPPED", "ui_state"),
	"jarvis.chat.api.set_conversation_thinking": ("SKIPPED", "ui_state"),
	"jarvis.chat.api.set_star": ("SKIPPED", "ui_state"),
	"jarvis.chat.api.stop_run": ("SKIPPED", "chat"),
	"jarvis.chat.api.warm_session": ("SKIPPED", "ui_state"),
	"jarvis.chat.app_learning_api.cancel_app_learning_run": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.app_learning_api.get_app_learning_overview": ("SKIPPED", "read"),
	"jarvis.chat.app_learning_api.list_app_learning_runs_page": ("SKIPPED", "read"),
	"jarvis.chat.app_learning_api.list_custom_apps": ("SKIPPED", "read"),
	"jarvis.chat.app_learning_api.schedule_app_learning": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.approvals_api.apply_sheet": ("LOGGED", "name", "action", "Jarvis Pending Action", 0),
	"jarvis.chat.approvals_api.approve_wiki_write": ("LOGGED", "name", "action", "Jarvis Approval Request", 0),
	"jarvis.chat.approvals_api.continue_approval_chat": ("SKIPPED", "chat"),
	"jarvis.chat.approvals_api.decide": ("LOGGED", "name", "action", "Jarvis Approval Request", 0),
	"jarvis.chat.approvals_api.decide_held_action": ("LOGGED", "name", "action", "Jarvis Pending Action", 0),
	"jarvis.chat.approvals_api.discard_sheet": ("LOGGED", "name", "action", "Jarvis Pending Action", 0),
	"jarvis.chat.approvals_api.dismiss_approval": ("LOGGED", "name", "action", "Jarvis Approval Request", 0),
	"jarvis.chat.approvals_api.edit_and_create_held": ("SKIPPED", "tool_path"),
	"jarvis.chat.approvals_api.get_approval": ("SKIPPED", "read"),
	"jarvis.chat.approvals_api.get_pending_action": ("SKIPPED", "read"),
	"jarvis.chat.approvals_api.get_sheet_candidates": ("SKIPPED", "read"),
	"jarvis.chat.approvals_api.list_approval_reviewers": ("SKIPPED", "read"),
	"jarvis.chat.approvals_api.list_approvals": ("SKIPPED", "read"),
	"jarvis.chat.approvals_api.list_approvals_page": ("SKIPPED", "read"),
	"jarvis.chat.approvals_api.list_pending_actions_lane": ("SKIPPED", "read"),
	"jarvis.chat.approvals_api.list_wiki_write_proposals": ("SKIPPED", "read"),
	"jarvis.chat.approvals_api.pending_badge": ("SKIPPED", "poll"),
	"jarvis.chat.approvals_api.pending_count": ("SKIPPED", "poll"),
	"jarvis.chat.approvals_api.reject_wiki_write": ("LOGGED", "name", "action", "Jarvis Approval Request", 0),
	"jarvis.chat.approvals_api.restore_approval": ("LOGGED", "name", "action", "Jarvis Approval Request", 0),
	"jarvis.chat.approvals_api.retry_wiki_write": ("LOGGED", "name", "action", "Jarvis Approval Request", 0),
	"jarvis.chat.approvals_api.tag_reviewer": ("LOGGED", "name", "action", "Jarvis Approval Request", 0),
	"jarvis.chat.approvals_api.untag_reviewer": ("LOGGED", "name", "action", "Jarvis Approval Request", 0),
	"jarvis.chat.collaboration_access.get_access": ("SKIPPED", "read"),
	"jarvis.chat.collaboration_access.set_access": ("LOGGED", "conversation", "action", "Jarvis Conversation", 0),
	"jarvis.chat.collaboration_access.set_recipients": ("LOGGED", "conversation", "action", "Jarvis Conversation", 0),
	"jarvis.chat.collaboration_actions.confirm_action": ("SKIPPED", "tool_path"),
	"jarvis.chat.collaboration_actions.discard_action": ("SKIPPED", "tool_path"),
	"jarvis.chat.collaboration_api.answer_question": ("LOGGED", "conversation", "action", "Jarvis Conversation", 0),
	"jarvis.chat.collaboration_api.get_canvas": ("SKIPPED", "read"),
	"jarvis.chat.collaboration_api.get_conversation": ("SKIPPED", "read"),
	"jarvis.chat.collaboration_api.get_file": ("SKIPPED", "read"),
	"jarvis.chat.collaboration_api.get_tool_detail": ("SKIPPED", "read"),
	"jarvis.chat.collaboration_api.retry_run": ("SKIPPED", "chat"),
	"jarvis.chat.collaboration_api.send_message": ("SKIPPED", "chat"),
	"jarvis.chat.collaboration_api.stop_run": ("SKIPPED", "chat"),
	"jarvis.chat.collaboration_api.upload_file": ("LOGGED", "conversation", "action", "Jarvis Conversation", 0),
	"jarvis.chat.connectors_api.add_connector": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.connectors_api.connect_oauth": ("LOGGED", "name", "action", "Jarvis Connector", 0),
	"jarvis.chat.connectors_api.delete_connector": ("LOGGED", "name", "action", "Jarvis Connector", 0),
	"jarvis.chat.connectors_api.disconnect_oauth": ("LOGGED", "name", "action", "Jarvis Connector", 0),
	"jarvis.chat.connectors_api.list_connectors": ("SKIPPED", "read"),
	"jarvis.chat.connectors_api.mcp_oauth_callback": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.connectors_api.oauth_signin_status": ("SKIPPED", "poll"),
	"jarvis.chat.connectors_api.probe_connector_auth": ("SKIPPED", "read"),
	"jarvis.chat.connectors_api.set_allowed_actions": ("LOGGED", "name", "action", "Jarvis Connector", 0),
	"jarvis.chat.connectors_api.set_oauth_client_credentials": ("LOGGED", "name", "action", "Jarvis Connector", 0),
	"jarvis.chat.connectors_api.test_connector": ("LOGGED", "name", "action", "Jarvis Connector", 0),
	"jarvis.chat.connectors_api.update_connector": ("LOGGED", "name", "action", "Jarvis Connector", 0),
	"jarvis.chat.custom_skills_api.apply_custom_skills": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.custom_skills_api.create_custom_skill": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.custom_skills_api.decide_skill_promotion": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.custom_skills_api.delete_custom_skill": ("LOGGED", "name", "action", "Jarvis Custom Skill", 0),
	"jarvis.chat.custom_skills_api.delete_custom_skills_bulk": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.custom_skills_api.dismiss_skill_notices": ("SKIPPED", "ui_state"),
	"jarvis.chat.custom_skills_api.file_box_doctypes": ("SKIPPED", "read"),
	"jarvis.chat.custom_skills_api.get_custom_skill": ("SKIPPED", "read"),
	"jarvis.chat.custom_skills_api.get_custom_skills_sync_status": ("SKIPPED", "poll"),
	"jarvis.chat.custom_skills_api.get_skill_shares": ("SKIPPED", "read"),
	"jarvis.chat.custom_skills_api.list_custom_skills": ("SKIPPED", "read"),
	"jarvis.chat.custom_skills_api.list_custom_skills_page": ("SKIPPED", "read"),
	"jarvis.chat.custom_skills_api.list_shareable_users": ("SKIPPED", "read"),
	"jarvis.chat.custom_skills_api.list_skill_promotion_requests": ("SKIPPED", "read"),
	"jarvis.chat.custom_skills_api.my_skill_promotion": ("SKIPPED", "read"),
	"jarvis.chat.custom_skills_api.preflight_skill_promotion": ("SKIPPED", "read"),
	"jarvis.chat.custom_skills_api.promotable_target_roles": ("SKIPPED", "read"),
	"jarvis.chat.custom_skills_api.request_skill_promotion": ("LOGGED", "name", "action", "Jarvis Custom Skill", 0),
	"jarvis.chat.custom_skills_api.share_custom_skill": ("LOGGED", "name", "action", "Jarvis Custom Skill", 0),
	"jarvis.chat.custom_skills_api.update_custom_skill": ("LOGGED", "name", "action", "Jarvis Custom Skill", 0),
	"jarvis.chat.cycle_count_entry.correct_counts": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.cycle_count_entry.get_count_plan": ("LOGGED", "request", "read", "Jarvis Approval Request", 1),
	"jarvis.chat.cycle_count_entry.list_count_plans": ("SKIPPED", "read"),
	"jarvis.chat.cycle_count_entry.record_counts": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.dashboards_api.dashboard_for_conversation": ("SKIPPED", "read"),
	"jarvis.chat.dashboards_api.delete_dashboard": ("LOGGED", "name", "action", "Jarvis Dashboard", 0),
	"jarvis.chat.dashboards_api.get_dashboard": ("SKIPPED", "read"),
	"jarvis.chat.dashboards_api.get_dashboards_caps": ("SKIPPED", "read"),
	"jarvis.chat.dashboards_api.list_dashboard_conversations": ("SKIPPED", "read"),
	"jarvis.chat.dashboards_api.list_dashboards_page": ("SKIPPED", "read"),
	"jarvis.chat.dashboards_api.preview_dashboard_source": ("LOGGED", None, "read", None, 0),
	"jarvis.chat.dashboards_api.run_dashboard_source": ("LOGGED", None, "read", None, 0),
	"jarvis.chat.dashboards_api.save_dashboard": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.dev_seed.seed_feature_pages": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.dev_seed.seed_varied_approvals": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.device.rotate_chat_device": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.docmeta_api.add_comment": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.docmeta_api.delete_attachment": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.docmeta_api.delete_comment": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.docmeta_api.get_docmeta": ("SKIPPED", "read"),
	"jarvis.chat.docmeta_api.toggle_assignment": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.docmeta_api.toggle_like": ("SKIPPED", "ui_state"),
	"jarvis.chat.docmeta_api.toggle_share": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.docmeta_api.update_comment": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.export.download_conversation": ("LOGGED", "conversation", "export", "Jarvis Conversation", 0),
	"jarvis.chat.feedback.pulse_context": ("SKIPPED", "read"),
	"jarvis.chat.feedback.session_feedback_status": ("SKIPPED", "read"),
	"jarvis.chat.feedback.submit_feedback": ("SKIPPED", "ui_state"),
	"jarvis.chat.feedback.submit_pulse_feedback": ("SKIPPED", "ui_state"),
	"jarvis.chat.feedback.submit_session_feedback": ("SKIPPED", "ui_state"),
	"jarvis.chat.filebox.bulk_rerun_inbound": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.filebox.check_skill": ("SKIPPED", "read"),
	"jarvis.chat.filebox.clear_processed_inbound": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.filebox.delete_inbound": ("LOGGED", "conversation", "action", "Jarvis Conversation", 0),
	"jarvis.chat.filebox.delete_inbound_bulk": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.filebox.drop_file": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.filebox.get_intake_detail": ("SKIPPED", "read"),
	"jarvis.chat.filebox.list_inbound_page": ("SKIPPED", "read"),
	"jarvis.chat.filebox.list_intake_page": ("SKIPPED", "read"),
	"jarvis.chat.filebox.open_waits": ("SKIPPED", "read"),
	"jarvis.chat.filebox.rerun_inbound": ("LOGGED", "conversation", "action", "Jarvis Conversation", 0),
	"jarvis.chat.filebox.retry_intake": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.filebox_source.get_source_state": ("SKIPPED", "read"),
	"jarvis.chat.filebox_source.set_source": ("SKIPPED", "ui_state"),
	"jarvis.chat.greeting.dismiss_greeting": ("SKIPPED", "ui_state"),
	"jarvis.chat.greeting.hide_greeting": ("SKIPPED", "ui_state"),
	"jarvis.chat.greeting.maybe_greet": ("SKIPPED", "read"),
	"jarvis.chat.learned_api.acknowledge_learned_pattern": ("LOGGED", "name", "action", "Jarvis Learned Pattern", 0),
	"jarvis.chat.learned_api.apply_insight_skill_update": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.learned_api.apply_learned_skills": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.learned_api.approve_learned_pattern": ("LOGGED", "name", "action", "Jarvis Learned Pattern", 0),
	"jarvis.chat.learned_api.batch_approve": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.learned_api.decide_promotion": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.learned_api.draft_insight_skill_update": ("SKIPPED", "read"),
	"jarvis.chat.learned_api.flag_learned_default": ("LOGGED", "name", "action", "Jarvis Learned Pattern", 0),
	"jarvis.chat.learned_api.get_learned_apply_status": ("SKIPPED", "poll"),
	"jarvis.chat.learned_api.get_learned_pattern": ("SKIPPED", "read"),
	"jarvis.chat.learned_api.get_learning_settings": ("SKIPPED", "read"),
	"jarvis.chat.learned_api.get_learning_status": ("SKIPPED", "read"),
	"jarvis.chat.learned_api.get_review_access": ("SKIPPED", "read"),
	"jarvis.chat.learned_api.go_to_chat_context": ("SKIPPED", "read"),
	"jarvis.chat.learned_api.list_learned_patterns_page": ("SKIPPED", "read"),
	"jarvis.chat.learned_api.list_promotion_requests_page": ("SKIPPED", "read"),
	"jarvis.chat.learned_api.pending_learned_count": ("SKIPPED", "read"),
	"jarvis.chat.learned_api.polish_learned_draft": ("LOGGED", "name", "action", "Jarvis Learned Pattern", 0),
	"jarvis.chat.learned_api.reject_learned_pattern": ("LOGGED", "name", "action", "Jarvis Learned Pattern", 0),
	"jarvis.chat.learned_api.restore_rejected_pattern": ("LOGGED", "name", "action", "Jarvis Learned Pattern", 0),
	"jarvis.chat.learned_api.run_pattern_analysis_now": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.learned_api.set_learning_settings": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.learned_api.snooze_learned_pattern": ("LOGGED", "name", "action", "Jarvis Learned Pattern", 0),
	"jarvis.chat.learned_api.trigger_followup_question": ("LOGGED", "name", "action", "Jarvis Learned Pattern", 0),
	"jarvis.chat.learned_api.unapprove_learned_pattern": ("LOGGED", "name", "action", "Jarvis Learned Pattern", 0),
	"jarvis.chat.learned_skills_api.get_learned_skills_sync_status": ("SKIPPED", "poll"),
	"jarvis.chat.list_filters.get_list_filter_schema": ("SKIPPED", "read"),
	"jarvis.chat.list_filters.list_filters_capabilities": ("SKIPPED", "read"),
	"jarvis.chat.macros_admin_api.admin_delete": ("LOGGED", "macro", "action", "Jarvis Macro", 0),
	"jarvis.chat.macros_admin_api.admin_get_macro": ("LOGGED", "name", "read", "Jarvis Macro", 1),
	"jarvis.chat.macros_admin_api.admin_handover": ("LOGGED", "macro", "action", "Jarvis Macro", 0),
	"jarvis.chat.macros_admin_api.admin_handover_targets": ("SKIPPED", "read"),
	"jarvis.chat.macros_admin_api.admin_hold": ("LOGGED", "macro", "action", "Jarvis Macro", 0),
	"jarvis.chat.macros_admin_api.admin_list_macros": ("SKIPPED", "read"),
	"jarvis.chat.macros_admin_api.admin_macro_owners": ("SKIPPED", "read"),
	"jarvis.chat.macros_admin_api.admin_release": ("LOGGED", "macro", "action", "Jarvis Macro", 0),
	"jarvis.chat.macros_admin_api.admin_stop_run": ("LOGGED", "run", "action", "Jarvis Macro Run", 0),
	"jarvis.chat.macros_api.create_macro": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.macros_api.delete_macro": ("LOGGED", "name", "action", "Jarvis Macro", 0),
	"jarvis.chat.macros_api.delete_macros_bulk": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.macros_api.dismiss_macro_notices": ("SKIPPED", "ui_state"),
	"jarvis.chat.macros_api.get_macro": ("SKIPPED", "read"),
	"jarvis.chat.macros_api.get_macro_run": ("SKIPPED", "poll"),
	"jarvis.chat.macros_api.get_new_macro_arming": ("SKIPPED", "read"),
	"jarvis.chat.macros_api.list_macro_runs": ("SKIPPED", "read"),
	"jarvis.chat.macros_api.list_macros": ("SKIPPED", "read"),
	"jarvis.chat.macros_api.list_macros_page": ("SKIPPED", "read"),
	"jarvis.chat.macros_api.macro_run_stats": ("SKIPPED", "read"),
	"jarvis.chat.macros_api.run_macro": ("LOGGED", "name", "action", "Jarvis Macro", 0),
	"jarvis.chat.macros_api.stop_macro_run": ("LOGGED", "run", "action", "Jarvis Macro Run", 0),
	"jarvis.chat.macros_api.summarize_macro": ("SKIPPED", "chat"),
	"jarvis.chat.macros_api.update_macro": ("LOGGED", "name", "action", "Jarvis Macro", 0),
	"jarvis.chat.pending_actions._ops.operator_fail": ("LOGGED", "name", "action", "Jarvis Pending Action", 0),
	"jarvis.chat.pending_actions._ops.operator_settle": ("LOGGED", "name", "action", "Jarvis Pending Action", 0),
	"jarvis.chat.personalise_api.answer_question": ("LOGGED", "name", "action", "Jarvis Personalise Question", 0),
	"jarvis.chat.personalise_api.delete_note": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.personalise_api.delete_question": ("LOGGED", "name", "action", "Jarvis Personalise Question", 0),
	"jarvis.chat.personalise_api.delete_question_rule": ("LOGGED", "name", "action", "Jarvis Personalise Question Rule", 0),
	"jarvis.chat.personalise_api.generate_chat_questions_now": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.personalise_api.get_note": ("SKIPPED", "read"),
	"jarvis.chat.personalise_api.get_personalisation_settings": ("SKIPPED", "read"),
	"jarvis.chat.personalise_api.get_question": ("SKIPPED", "read"),
	"jarvis.chat.personalise_api.get_skills_area_caps": ("SKIPPED", "read"),
	"jarvis.chat.personalise_api.ignore_question": ("SKIPPED", "ui_state"),
	"jarvis.chat.personalise_api.list_notes_page": ("SKIPPED", "read"),
	"jarvis.chat.personalise_api.list_question_rules": ("SKIPPED", "read"),
	"jarvis.chat.personalise_api.list_questions_page": ("SKIPPED", "read"),
	"jarvis.chat.personalise_api.list_role_options": ("SKIPPED", "read"),
	"jarvis.chat.personalise_api.save_note": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.personalise_api.save_question_rule": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.personalise_api.set_personalisation_settings": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.proactive.demo_proactive": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.pwa_send.check_delivery": ("SKIPPED", "poll"),
	"jarvis.chat.pwa_send.send_message": ("SKIPPED", "chat"),
	"jarvis.chat.reorder_review.decide": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.reorder_review.get_review": ("LOGGED", "name", "read", "Jarvis Agent Run", 1),
	"jarvis.chat.reorder_review.list_reviews": ("LOGGED", None, "read", None, 0),
	"jarvis.chat.report_runs.chat_report_runs": ("SKIPPED", "poll"),
	"jarvis.chat.send_requests.check_delivery": ("SKIPPED", "poll"),
	"jarvis.chat.send_requests.send_message": ("SKIPPED", "chat"),
	"jarvis.chat.sharing.get_shared_canvas": ("SKIPPED", "read"),
	"jarvis.chat.sharing.get_shared_conversation": ("SKIPPED", "read"),
	"jarvis.chat.sharing.get_shared_file": ("SKIPPED", "read"),
	"jarvis.chat.sharing.get_shared_tool_detail": ("SKIPPED", "read"),
	"jarvis.chat.sharing.get_shares": ("SKIPPED", "read"),
	"jarvis.chat.sharing.get_sharing_setting": ("SKIPPED", "read"),
	"jarvis.chat.sharing.is_shared_with_me": ("SKIPPED", "read"),
	"jarvis.chat.sharing.list_all_shares": ("SKIPPED", "read"),
	"jarvis.chat.sharing.list_shared_with_me": ("SKIPPED", "read"),
	"jarvis.chat.sharing.revoke_share": ("LOGGED", "name", "action", "Jarvis Conversation Share", 0),
	"jarvis.chat.sharing.set_shares": ("LOGGED", "conversation", "action", "Jarvis Conversation", 0),
	"jarvis.chat.sharing.set_sharing_setting": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.sharing.share_candidates": ("SKIPPED", "read"),
	"jarvis.chat.site_pin.reverify": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.skills_admin_api.admin_delete": ("LOGGED", "skill", "action", "Jarvis Custom Skill", 0),
	"jarvis.chat.skills_admin_api.admin_get_skill": ("LOGGED", "name", "read", "Jarvis Custom Skill", 1),
	"jarvis.chat.skills_admin_api.admin_handover": ("LOGGED", "skill", "action", "Jarvis Custom Skill", 0),
	"jarvis.chat.skills_admin_api.admin_handover_targets": ("SKIPPED", "read"),
	"jarvis.chat.skills_admin_api.admin_hold": ("LOGGED", "skill", "action", "Jarvis Custom Skill", 0),
	"jarvis.chat.skills_admin_api.admin_list_skills": ("SKIPPED", "read"),
	"jarvis.chat.skills_admin_api.admin_release": ("LOGGED", "skill", "action", "Jarvis Custom Skill", 0),
	"jarvis.chat.skills_admin_api.admin_skill_owners": ("SKIPPED", "read"),
	"jarvis.chat.ticket_triage.confirm_ticket_triage_proposal": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.ticket_triage.get_ticket_triage_proposal": ("SKIPPED", "read"),
	"jarvis.chat.ticket_triage.list_ticket_triage_proposals": ("SKIPPED", "read"),
	"jarvis.chat.ticket_triage.reject_ticket_triage_proposal": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.triggers_api.activity_stats": ("SKIPPED", "read"),
	"jarvis.chat.triggers_api.create_trigger": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.triggers_api.delete_trigger": ("LOGGED", "name", "action", "Jarvis Trigger", 0),
	"jarvis.chat.triggers_api.delete_triggers_bulk": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.triggers_api.get_activity_detail": ("SKIPPED", "read"),
	"jarvis.chat.triggers_api.get_intake_field_options": ("SKIPPED", "read"),
	"jarvis.chat.triggers_api.get_trigger": ("SKIPPED", "read"),
	"jarvis.chat.triggers_api.get_triggers_caps": ("SKIPPED", "read"),
	"jarvis.chat.triggers_api.list_activity_page": ("SKIPPED", "read"),
	"jarvis.chat.triggers_api.list_intake_folders": ("SKIPPED", "read"),
	"jarvis.chat.triggers_api.list_triggers_page": ("SKIPPED", "read"),
	"jarvis.chat.triggers_api.search_intake_doctypes": ("SKIPPED", "read"),
	"jarvis.chat.triggers_api.set_trigger_enabled": ("LOGGED", "name", "action", "Jarvis Trigger", 0),
	"jarvis.chat.triggers_api.test_trigger_condition": ("SKIPPED", "read"),
	"jarvis.chat.triggers_api.update_trigger": ("LOGGED", "name", "action", "Jarvis Trigger", 0),
	"jarvis.chat.user_settings_api.admin_list_user_usage": ("SKIPPED", "read"),
	"jarvis.chat.user_settings_api.admin_set_user_limit": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.user_settings_api.admin_set_user_model_limit": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.user_settings_api.admin_sync_usage": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.user_settings_api.get_capability_catalog": ("SKIPPED", "read"),
	"jarvis.chat.user_settings_api.get_my_settings": ("SKIPPED", "read"),
	"jarvis.chat.user_settings_api.get_prompt_suggestions": ("SKIPPED", "read"),
	"jarvis.chat.user_settings_api.log_capability_pick": ("SKIPPED", "ui_state"),
	"jarvis.chat.user_settings_api.set_sidebar_order": ("SKIPPED", "ui_state"),
	"jarvis.chat.user_settings_api.set_user_theme": ("SKIPPED", "ui_state"),
	"jarvis.chat.user_settings_api.update_my_settings": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.voice.transcribe_audio": ("SKIPPED", "chat"),
	"jarvis.chat.voice_notes_api.delete_voice_note": ("LOGGED", "name", "action", "Jarvis Voice Note", 0),
	"jarvis.chat.voice_notes_api.get_business_status": ("SKIPPED", "read"),
	"jarvis.chat.voice_notes_api.list_my_voice_notes_page": ("SKIPPED", "read"),
	"jarvis.chat.voice_notes_api.process_voice_notes_now": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.voice_notes_api.save_voice_note": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.voice_notes_api.update_voice_note": ("LOGGED", "name", "action", "Jarvis Voice Note", 0),
	"jarvis.chat.wiki.add_wiki_link": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki.archive_wiki_page": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki.create_wiki_page": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki.delete_wiki_page": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki.dismiss_nudge": ("SKIPPED", "ui_state"),
	"jarvis.chat.wiki.get_wiki_caps": ("SKIPPED", "read"),
	"jarvis.chat.wiki.get_wiki_graph": ("SKIPPED", "read"),
	"jarvis.chat.wiki.get_wiki_graph_history": ("SKIPPED", "read"),
	"jarvis.chat.wiki.get_wiki_page": ("SKIPPED", "read"),
	"jarvis.chat.wiki.list_wiki_pages_page": ("SKIPPED", "read"),
	"jarvis.chat.wiki.my_wiki_promotion": ("SKIPPED", "read"),
	"jarvis.chat.wiki.remove_wiki_link": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki.request_wiki_promotion": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki.restore_wiki_page": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki.run_wiki_lint_now": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki.save_wiki_page": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki.set_knowledge_language": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki.sync_wiki_mirror_now": ("LOGGED", None, "action", None, 0),
	"jarvis.chat.wiki_graph.sync_now": ("LOGGED", None, "action", None, 0),
	"jarvis.diagnostics.chat_recovery_stats": ("SKIPPED", "read"),
	"jarvis.diagnostics.force_resync": ("SKIPPED", "desk"),
	"jarvis.diagnostics.import_announce_stats": ("SKIPPED", "read"),
	"jarvis.diagnostics.ping_admin": ("SKIPPED", "desk"),
	"jarvis.diagnostics.ping_agent": ("SKIPPED", "desk"),
	"jarvis.diagnostics.reset_agent_pairing": ("SKIPPED", "desk"),
	"jarvis.holiday_touch.api.explain": ("SKIPPED", "read"),
	"jarvis.holiday_touch.api.get_today": ("SKIPPED", "read"),
	"jarvis.holiday_touch.api.record_shown": ("SKIPPED", "ui_state"),
	"jarvis.llm_key_probe.test_llm_api_key": ("SKIPPED", "read"),
	"jarvis.maintenance_notice.check": ("SKIPPED", "poll"),
	"jarvis.mobile.auth.get_mobile_token": ("LOGGED", None, "action", None, 0),
	"jarvis.mobile.auth.get_pairing_qr": ("SKIPPED", "read"),
	"jarvis.mobile.auth.get_pwa_qr": ("SKIPPED", "read"),
	"jarvis.mobile.auth.list_mobile_devices": ("SKIPPED", "read"),
	"jarvis.mobile.auth.revoke_all_mobile_devices": ("LOGGED", None, "action", None, 0),
	"jarvis.mobile.auth.revoke_mobile_device": ("LOGGED", "device", "action", "Jarvis Mobile Device", 0),
	"jarvis.network_check.answer": ("SKIPPED", "auth"),
	"jarvis.oauth.api.begin_claude_cli_login": ("LOGGED", None, "action", None, 0),
	"jarvis.oauth.api.begin_paste_signin": ("LOGGED", None, "action", None, 0),
	"jarvis.oauth.api.begin_pool_account_signin": ("LOGGED", None, "action", None, 0),
	"jarvis.oauth.api.cancel_claude_cli_login": ("LOGGED", None, "action", None, 0),
	"jarvis.oauth.api.cancel_pending_capture": ("LOGGED", None, "action", None, 0),
	"jarvis.oauth.api.complete_claude_cli_login": ("LOGGED", None, "action", None, 0),
	"jarvis.oauth.api.complete_paste_signin": ("LOGGED", None, "action", None, 0),
	"jarvis.oauth.api.complete_pool_account_signin": ("LOGGED", None, "action", None, 0),
	"jarvis.oauth.api.disconnect": ("LOGGED", None, "action", None, 0),
	"jarvis.oauth.api.get_direct_subscription_status": ("SKIPPED", "read"),
	"jarvis.oauth.api.list_pending_captures": ("SKIPPED", "read"),
	"jarvis.oauth.api.poll_pool_account_signin": ("SKIPPED", "poll"),
	"jarvis.onboarding.capture_onboarding_lead": ("SKIPPED", "ui_state"),
	"jarvis.onboarding.check_account_reconnect": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.check_signup_payment_state": ("SKIPPED", "poll"),
	"jarvis.onboarding.check_signup_payment_status": ("SKIPPED", "poll"),
	"jarvis.onboarding.disconnect_llm": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.finish_payment": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.get_account_defaults": ("SKIPPED", "read"),
	"jarvis.onboarding.get_company_onboarding_defaults": ("SKIPPED", "read"),
	"jarvis.onboarding.get_llm_config": ("SKIPPED", "read"),
	"jarvis.onboarding.get_llm_sync_status": ("SKIPPED", "poll"),
	"jarvis.onboarding.get_onboarding_state": ("SKIPPED", "read"),
	"jarvis.onboarding.get_preset_catalog": ("SKIPPED", "read"),
	"jarvis.onboarding.get_terms_url": ("SKIPPED", "read"),
	"jarvis.onboarding.gstin_autofill": ("SKIPPED", "read"),
	"jarvis.onboarding.gstin_autofill_available": ("SKIPPED", "read"),
	"jarvis.onboarding.initiate_signup_payment": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.list_payment_providers": ("SKIPPED", "read"),
	"jarvis.onboarding.list_plans": ("SKIPPED", "read"),
	"jarvis.onboarding.reconnect_available": ("SKIPPED", "read"),
	"jarvis.onboarding.redeem_reconnect_code": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.renew": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.request_workspace_reset": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.reset_onboarding": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.resync_llm": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.save_llm_creds": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.save_llm_pool": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.start_account_reconnect": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.start_signup": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.sync_connection": ("SKIPPED", "poll"),
	"jarvis.onboarding.update_billing": ("LOGGED", None, "action", None, 0),
	"jarvis.onboarding.workspace_reset_state": ("SKIPPED", "poll"),
	"jarvis.payment_calendar.api.configure_floor": ("LOGGED", None, "action", None, 0),
	"jarvis.payment_calendar.api.get_account_balances_status": ("SKIPPED", "poll"),
	"jarvis.payment_calendar.api.get_bucket_detail": ("LOGGED", None, "read", None, 0),
	"jarvis.payment_calendar.api.get_capabilities": ("SKIPPED", "read"),
	"jarvis.payment_calendar.api.get_day_detail": ("LOGGED", None, "read", None, 0),
	"jarvis.payment_calendar.api.get_preferences": ("SKIPPED", "read"),
	"jarvis.payment_calendar.api.get_snapshot_status": ("SKIPPED", "poll"),
	"jarvis.payment_calendar.api.get_upcoming": ("LOGGED", None, "read", None, 0),
	"jarvis.payment_calendar.api.request_account_balances": ("LOGGED", None, "read", None, 0),
	"jarvis.payment_calendar.api.request_snapshot": ("LOGGED", None, "read", None, 0),
	"jarvis.payment_calendar.api.set_preferences": ("LOGGED", None, "action", None, 0),
	"jarvis.payment_calendar.cache.reset": ("LOGGED", "company", "action", "Company", 0),
	"jarvis.payment_calendar.cache_status.cache_status": ("SKIPPED", "read"),
	"jarvis.pdf_templates.delete_pdf_template": ("LOGGED", None, "action", None, 0),
	"jarvis.pdf_templates.get_default_pdf_template": ("SKIPPED", "read"),
	"jarvis.pdf_templates.get_pdf_template": ("SKIPPED", "read"),
	"jarvis.pdf_templates.list_pdf_templates": ("SKIPPED", "read"),
	"jarvis.pdf_templates.pdf_template_options": ("SKIPPED", "read"),
	"jarvis.pdf_templates.preview_pdf_template": ("SKIPPED", "read"),
	"jarvis.pdf_templates.rerender_document": ("LOGGED", None, "action", None, 0),
	"jarvis.pdf_templates.save_pdf_template": ("LOGGED", None, "action", None, 0),
	"jarvis.pdf_templates.set_default_pdf_template": ("LOGGED", None, "action", None, 0),
	"jarvis.preflight.run_chat_preflight": ("SKIPPED", "read"),
	"jarvis.private_network.check_admin_connection": ("SKIPPED", "read"),
	"jarvis.private_network.connection_test_status": ("SKIPPED", "poll"),
	"jarvis.private_network.network_status": ("SKIPPED", "read"),
	"jarvis.private_network.private_setup_targets": ("SKIPPED", "read"),
	"jarvis.private_network.reserve_host": ("LOGGED", None, "action", None, 0),
	"jarvis.private_network.save_public_address": ("LOGGED", None, "action", None, 0),
	"jarvis.private_network.start_connection_test": ("SKIPPED", "read"),
	"jarvis.release_notice.check": ("SKIPPED", "poll"),
	"jarvis.release_notice.notes": ("SKIPPED", "read"),
	"jarvis.subscription_health.get_subscription_notice": ("SKIPPED", "read"),
	"jarvis.support.api.awaiting_count": ("SKIPPED", "poll"),
	"jarvis.support.api.close_ticket": ("LOGGED", None, "action", None, 0),
	"jarvis.support.api.create_ticket": ("LOGGED", None, "action", None, 0),
	"jarvis.support.api.error_report_details": ("SKIPPED", "read"),
	"jarvis.support.api.get_thread": ("SKIPPED", "read"),
	"jarvis.support.api.list_tickets": ("SKIPPED", "read"),
	"jarvis.support.api.reply": ("LOGGED", None, "action", None, 0),
	"jarvis.support.media.download": ("LOGGED", None, "export", None, 0),
	"jarvis.support.media.upload": ("LOGGED", None, "action", None, 0),
	"jarvis.tools._export.jobs.cancel": ("LOGGED", "name", "action", "Jarvis Export Job", 0),
	"jarvis.tools._export.jobs.get_jobs": ("SKIPPED", "poll"),
}
# fmt: on

# Endpoints whose function name humanised reads badly, or that deserve a plainer sentence.
# fmt: off
LABEL_OVERRIDES = {
	"jarvis.account.stop_autopay_to_pay": "Stop autopay to pay now",
	"jarvis.chat.actions_api.load_doc": "Opened a document",
	"jarvis.chat.admission.pump_cutover_execute": "Run relay pump cutover",
	"jarvis.chat.admission.pump_set_transport_mode": "Set relay transport mode",
	"jarvis.chat.agents_api.apply_agents": "Apply agents to the assistant",
	"jarvis.chat.agents_api.demote_installation": "Demote an agent to shadow",
	"jarvis.chat.agents_api.install_agent": "Install an agent",
	"jarvis.chat.agents_api.promote_installation": "Promote an agent to live",
	"jarvis.chat.agents_api.raise_activation_ceiling": "Raise the agent activation ceiling",
	"jarvis.chat.agents_api.run_agent_now": "Run an agent now",
	"jarvis.chat.agents_api.set_agent_access": "Set who can use an agent",
	"jarvis.chat.agents_api.set_config": "Change agent settings",
	"jarvis.chat.agents_api.set_enabled": "Enable or disable an agent",
	"jarvis.chat.agents_api.set_listing_status": "Set agent listing status",
	"jarvis.chat.agents_api.set_operator_visibility": "Set agent catalogue visibility",
	"jarvis.chat.agents_api.set_run_as_user": "Set an agent's run-as user",
	"jarvis.chat.agents_api.set_schedule": "Set an agent schedule",
	"jarvis.chat.agents_api.stop_agent_run": "Stop an agent run",
	"jarvis.chat.agents_api.take_finding_to_chat": "Open a chat from a finding",
	"jarvis.chat.agents_api.uninstall_agent": "Uninstall an agent",
	"jarvis.chat.api.archive_conversation": "Archive a conversation",
	"jarvis.chat.api.clear_chat_history": "Clear all chat history",
	"jarvis.chat.approvals_api.apply_sheet": "Applied a sheet",
	"jarvis.chat.approvals_api.approve_wiki_write": "Approve a wiki proposal",
	"jarvis.chat.approvals_api.decide": "Decide an approval",
	"jarvis.chat.approvals_api.decide_held_action": "Decided a held change",
	"jarvis.chat.approvals_api.discard_sheet": "Discarded a sheet",
	"jarvis.chat.approvals_api.dismiss_approval": "Dismiss an approval",
	"jarvis.chat.approvals_api.reject_wiki_write": "Reject a wiki proposal",
	"jarvis.chat.approvals_api.restore_approval": "Restore a dismissed approval",
	"jarvis.chat.approvals_api.retry_wiki_write": "Retry a wiki proposal",
	"jarvis.chat.approvals_api.tag_reviewer": "Invite a reviewer",
	"jarvis.chat.approvals_api.untag_reviewer": "Remove a reviewer",
	"jarvis.chat.collaboration_access.set_access": "Change shared-chat access",
	"jarvis.chat.collaboration_access.set_recipients": "Change shared-chat recipients",
	"jarvis.chat.collaboration_api.answer_question": "Answer a shared-chat question",
	"jarvis.chat.collaboration_api.upload_file": "Upload a file to a shared chat",
	"jarvis.chat.connectors_api.connect_oauth": "Start connector sign-in",
	"jarvis.chat.connectors_api.mcp_oauth_callback": "Connect a connector (sign-in finished)",
	"jarvis.chat.connectors_api.test_connector": "Test a connector",
	"jarvis.chat.custom_skills_api.apply_custom_skills": "Apply skills to the assistant",
	"jarvis.chat.custom_skills_api.decide_skill_promotion": "Decide a skill promotion",
	"jarvis.chat.custom_skills_api.delete_custom_skills_bulk": "Delete skills",
	"jarvis.chat.custom_skills_api.request_skill_promotion": "Request a skill promotion",
	"jarvis.chat.cycle_count_entry.correct_counts": "Correct stock counts",
	"jarvis.chat.cycle_count_entry.get_count_plan": "Opened a cycle count plan",
	"jarvis.chat.cycle_count_entry.record_counts": "Record stock counts",
	"jarvis.chat.dashboards_api.preview_dashboard_source": "Previewed a dashboard data source",
	"jarvis.chat.dashboards_api.run_dashboard_source": "Ran a dashboard data source",
	"jarvis.chat.dev_seed.seed_feature_pages": "Seed sample feature pages",
	"jarvis.chat.dev_seed.seed_varied_approvals": "Seed sample approvals",
	"jarvis.chat.device.rotate_chat_device": "Rotate chat device keys",
	"jarvis.chat.docmeta_api.add_comment": "Comment on a document",
	"jarvis.chat.docmeta_api.delete_attachment": "Delete a document attachment",
	"jarvis.chat.docmeta_api.delete_comment": "Delete a comment",
	"jarvis.chat.docmeta_api.toggle_assignment": "Assign or unassign a document",
	"jarvis.chat.docmeta_api.toggle_share": "Share or unshare a document",
	"jarvis.chat.docmeta_api.update_comment": "Edit a comment",
	"jarvis.chat.export.download_conversation": "Download a conversation",
	"jarvis.chat.filebox.bulk_rerun_inbound": "Re-run File Box files",
	"jarvis.chat.filebox.clear_processed_inbound": "Clear processed File Box files",
	"jarvis.chat.filebox.delete_inbound": "Delete a File Box file",
	"jarvis.chat.filebox.delete_inbound_bulk": "Delete File Box files",
	"jarvis.chat.filebox.drop_file": "Drop a file in File Box",
	"jarvis.chat.filebox.rerun_inbound": "Re-run a File Box file",
	"jarvis.chat.filebox.retry_intake": "Retry an intake file",
	"jarvis.chat.learned_api.apply_insight_skill_update": "Apply an insight to a skill",
	"jarvis.chat.learned_api.batch_approve": "Approve learned patterns in bulk",
	"jarvis.chat.learned_api.decide_promotion": "Decide a wiki promotion",
	"jarvis.chat.learned_api.polish_learned_draft": "Polish a learned-pattern draft",
	"jarvis.chat.learned_api.set_learning_settings": "Change learning settings",
	"jarvis.chat.learned_api.trigger_followup_question": "Ask a follow-up question",
	"jarvis.chat.macros_admin_api.admin_delete": "Delete another user's macro",
	"jarvis.chat.macros_admin_api.admin_handover": "Hand over a macro",
	"jarvis.chat.macros_admin_api.admin_hold": "Put a macro on hold",
	"jarvis.chat.macros_admin_api.admin_release": "Release a macro hold",
	"jarvis.chat.macros_admin_api.admin_stop_run": "Stop another user's macro run",
	"jarvis.chat.macros_api.delete_macros_bulk": "Delete macros",
	"jarvis.chat.macros_api.run_macro": "Run a macro",
	"jarvis.chat.pending_actions._ops.operator_fail": "Fail a pending action (operator)",
	"jarvis.chat.pending_actions._ops.operator_settle": "Settle a pending action (operator)",
	"jarvis.chat.personalise_api.generate_chat_questions_now": "Generate questions now",
	"jarvis.chat.personalise_api.set_personalisation_settings": "Change personalisation settings",
	"jarvis.chat.proactive.demo_proactive": "Send a demo proactive message",
	"jarvis.chat.reorder_review.decide": "Decide a reorder suggestion",
	"jarvis.chat.reorder_review.get_review": "Opened a reorder review",
	"jarvis.chat.reorder_review.list_reviews": "Listed reorder reviews",
	"jarvis.chat.sharing.revoke_share": "Revoke a conversation share",
	"jarvis.chat.sharing.set_shares": "Share a conversation",
	"jarvis.chat.sharing.set_sharing_setting": "Change the chat sharing setting",
	"jarvis.chat.site_pin.reverify": "Re-verify this site",
	"jarvis.chat.skills_admin_api.admin_delete": "Delete another user's skill",
	"jarvis.chat.skills_admin_api.admin_handover": "Hand over a skill",
	"jarvis.chat.skills_admin_api.admin_hold": "Put a skill on hold",
	"jarvis.chat.skills_admin_api.admin_release": "Release a skill hold",
	"jarvis.chat.triggers_api.delete_triggers_bulk": "Delete triggers",
	"jarvis.chat.triggers_api.set_trigger_enabled": "Enable or disable a trigger",
	"jarvis.chat.user_settings_api.admin_set_user_limit": "Set a user's token limit",
	"jarvis.chat.user_settings_api.admin_set_user_model_limit": "Set a user's model token limit",
	"jarvis.chat.user_settings_api.admin_sync_usage": "Sync usage from the agent",
	"jarvis.chat.user_settings_api.update_my_settings": "Changed my chat settings",
	"jarvis.chat.wiki.set_knowledge_language": "Set the knowledge language",
	"jarvis.chat.wiki.sync_wiki_mirror_now": "Sync the wiki mirror now",
	"jarvis.chat.wiki_graph.sync_now": "Sync the wiki graph now",
	"jarvis.mobile.auth.get_mobile_token": "Pair a mobile device",
	"jarvis.oauth.api.begin_claude_cli_login": "Start Claude sign-in",
	"jarvis.oauth.api.begin_paste_signin": "Start an LLM sign-in",
	"jarvis.oauth.api.begin_pool_account_signin": "Start adding an LLM sign-in account",
	"jarvis.oauth.api.cancel_claude_cli_login": "Cancel Claude sign-in",
	"jarvis.oauth.api.cancel_pending_capture": "Discard a pending sign-in",
	"jarvis.oauth.api.complete_claude_cli_login": "Complete Claude sign-in",
	"jarvis.oauth.api.complete_paste_signin": "Connect an LLM sign-in",
	"jarvis.oauth.api.complete_pool_account_signin": "Add an LLM sign-in account",
	"jarvis.oauth.api.disconnect": "Disconnect the LLM sign-in",
	"jarvis.onboarding.check_account_reconnect": "Redeem a reconnect code",
	"jarvis.onboarding.disconnect_llm": "Disconnect the LLM",
	"jarvis.onboarding.request_workspace_reset": "Request a workspace reset",
	"jarvis.onboarding.resync_llm": "Re-sync the AI configuration",
	"jarvis.onboarding.save_llm_creds": "Save LLM credentials",
	"jarvis.onboarding.save_llm_pool": "Save the LLM model pool",
	"jarvis.onboarding.update_billing": "Update billing details",
	"jarvis.payment_calendar.api.get_bucket_detail": "Opened a payment calendar bucket",
	"jarvis.payment_calendar.api.get_day_detail": "Opened a payment calendar day",
	"jarvis.payment_calendar.api.get_upcoming": "Opened upcoming payments",
	"jarvis.payment_calendar.api.request_account_balances": "Requested bank balances",
	"jarvis.payment_calendar.api.request_snapshot": "Requested a payment calendar snapshot",
	"jarvis.pdf_templates.rerender_document": "Re-render a document",
	"jarvis.private_network.reserve_host": "Reserve a private host",
	"jarvis.support.media.download": "Download a support attachment",
	"jarvis.support.media.upload": "Upload a support attachment",
	"jarvis.tools._export.jobs.cancel": "Cancel an export",
}
# fmt: on

UPLOAD_LABEL = "Uploaded a file"
UPLOAD_METHOD = "frappe.handler.upload_file"

_SNAPSHOT = "jarvis_activity_req"
_V1_PREFIXES = ("/api/method/", "/api/v1/method/")
_V2_PREFIX = "/api/v2/method/"
_CLIENT_HEADER = "X-Jarvis-Client"
_CLIENTS = frozenset({"desktop", "pwa"})

# A failed call that answered HTTP 200 with ``{"ok": false, ...}`` (jarvis envelopes) is a
# failure, not an ``ok``. Only a small JSON body is read, and only when the cheap byte test
# says it may hold the marker.
_OK_FALSE = re.compile(rb'"ok"\s*:\s*false')
_MAX_BODY = 65536
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:\- ]{1,64}$")

# Statuses that prove the endpoint's function never ran: 400 (bad request), 404 (no such
# method), 405 (wrong verb), 415 (wrong media type). 401 (signed out) and 429 (throttled) are
# the same kind of answer. Nothing happened, so nothing is attributed, and a client cannot fill
# the table with them.
_NEVER_RAN = frozenset({400, 401, 404, 405, 415, 429})

# Flood control. Redis keys are site-scoped (``cache.make_key`` prefixes the db name) and listed
# in ``persistent_cache_keys`` (hooks.py) so the periodic full cache clear does not reset them.
FLOOD_PREFIX = "jarvis:activity_flood:"
SEEN_PREFIX = "jarvis:activity_seen:"
DEDUPE_WINDOW_S = 300  # an identical call inside this is one row
ACTOR_CAP_PER_MINUTE = 120  # rows one person can add a minute (a dashboard of tiles, a retry loop)
_CAP_TTL_S = 120  # a minute bucket outlives its minute by one more
_HEALTH_HOURS = 24
_HEALTH_TTL_S = (_HEALTH_HOURS + 2) * 3600


def _epoch() -> float:
	"""The clock for the dedupe/cap/health buckets (a test drives it)."""
	return time.time()


def _cache():
	"""The Redis client: ``frappe.cache()`` on v15 and v16 (v16's ``frappe.cache`` is the client
	itself, which stays callable for this)."""
	return frappe.cache()


def _digest(*parts: str) -> str:
	return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _dedupe_key(cache, actor: str, key: str, ref_doctype: str, ref_name: str, outcome: str):
	return cache.make_key(f"{FLOOD_PREFIX}dedupe:{_digest(actor, key, ref_doctype, ref_name, outcome)}")


def _floods(entry, outcome: str) -> bool:
	"""Flood control applies only to what a client can repeat for free: READS and refused or
	failed calls. A successful ``action`` or ``export`` is a change (or a download) somebody
	made, and is ALWAYS recorded: five real deletes in five minutes are five rows, and a burst
	of reads can never cost a person the record of a change."""
	return entry[2] == "read" or outcome != "ok"


def _forget_dedupe(actor: str, key: str, ref_doctype: str, ref_name: str, outcome: str) -> None:
	"""Release a dedupe claim whose row was not written, so the next identical call is
	recorded instead of being dropped as a repeat of something that never made it."""
	try:
		cache = _cache()
		cache.delete(_dedupe_key(cache, actor, key, ref_doctype, ref_name, outcome))
	except Exception:
		pass


def _flood_allows(actor: str, key: str, ref_doctype: str, ref_name: str, outcome: str) -> bool:
	"""False when this row would be a repeat inside ``DEDUPE_WINDOW_S`` or over the person's
	per-minute cap. Fails OPEN: any error answers True (record it) and is file-logged.

	The dedupe check goes first so a repeat does not spend the person's cap."""
	try:
		cache = _cache()
		dedupe = _dedupe_key(cache, actor, key, ref_doctype, ref_name, outcome)
		if not cache.set(dedupe, 1, nx=True, ex=DEDUPE_WINDOW_S):
			return False  # the key already exists: an identical call inside the window
		bucket = cache.make_key(f"{FLOOD_PREFIX}cap:{_digest(actor)}:{int(_epoch() // 60)}")
		pipe = cache.pipeline()
		pipe.incr(bucket)
		pipe.expire(bucket, _CAP_TTL_S)  # on every call: a crash between the two cannot leave it immortal
		return int(pipe.execute()[0]) <= ACTOR_CAP_PER_MINUTE
	except Exception as e:
		activity_log._fail("flood control unavailable", e)
		return True


def _note(kind: str) -> None:
	"""Bump this hour's ``seen`` / ``failures`` counter. Never raises."""
	try:
		cache = _cache()
		key = cache.make_key(f"{SEEN_PREFIX}{kind}:{int(_epoch() // 3600)}")
		pipe = cache.pipeline()
		pipe.incr(key)
		pipe.expire(key, _HEALTH_TTL_S)
		pipe.execute()
	except Exception:
		pass  # a health counter must never cost the request, nor log a line per request


def health_counters() -> dict:
	"""``{"seen": n, "failures": n, "dropped": n}`` over the last 24 hourly buckets: LOGGED
	requests the hook saw, hook failures (snapshot, record, or an insert that wrote nothing) and
	rows flood control dropped (repeat or over the cap). Zeros when Redis
	cannot be read: a health check that raises would take the page down with it."""
	try:
		cache = _cache()
		hour = int(_epoch() // 3600)
		out = {}
		for kind in ("seen", "failures", "dropped"):
			keys = [
				cache.make_key(f"{SEEN_PREFIX}{kind}:{h}") for h in range(hour - _HEALTH_HOURS + 1, hour + 1)
			]
			out[kind] = sum(int(v or 0) for v in cache.mget(keys))
		return out
	except Exception:
		return {"seen": 0, "failures": 0, "dropped": 0}


def label_for(key: str) -> str:
	"""The admin-facing label of a classified endpoint. The tool-path twin is
	``activity_report._humanise`` (a tool name, no overrides): keep how they read in step."""
	label = LABEL_OVERRIDES.get(key)
	if label:
		return label
	name = key.rsplit(".", 1)[-1].replace("_", " ").strip()
	return name[:1].upper() + name[1:]


def _method_from(form, path: str):
	"""``(method, doctype)`` the request names, in the order Frappe runs it.

	1. A client-supplied ``cmd`` form key wins on ANY path (frappe/app.py: the
	   ``elif frappe.form_dict.cmd`` branch precedes ``/api/`` routing);
	2. ``/api/method/<m>`` and ``/api/v1/method/<m>`` (v1: ``handle_rpc_call`` keeps the
	   segment before the first "/", api/v1.py);
	3. ``/api/v2/method/<m>`` or ``/api/v2/method/<doctype>/<m>`` (api/v2.py).

	Pure string work, no imports, no DB. ``jarvis.api_errors._METHOD_PATH_RE`` reads the same
	paths for the error reporter (v1 forms only): a new route form must be taught to both."""
	cmd = form.get("cmd") if form is not None else None
	if cmd:
		return (cmd, None) if isinstance(cmd, str) else (None, None)
	for prefix in _V1_PREFIXES:
		if path.startswith(prefix):
			return path[len(prefix) :].split("/")[0] or None, None
	if path.startswith(_V2_PREFIX):
		parts = [p for p in path[len(_V2_PREFIX) :].split("/") if p]
		if len(parts) == 1:
			return parts[0], None
		if len(parts) == 2:
			return parts[1], parts[0]
	return None, None


def capture_request() -> None:
	"""``before_request``: snapshot the method name and the original ``form_dict``.

	Never raises: a hook that throws would fail the request it is only watching."""
	try:
		frappe.local.jarvis_activity_req = None
		request = frappe.local.request
		if request is None or request.method == "OPTIONS":
			return
		form = frappe.local.form_dict
		method, doctype = _method_from(form, request.path or "")
		if method:
			frappe.local.jarvis_activity_req = types.SimpleNamespace(
				method=method, doctype=doctype, form=form
			)
	except Exception as e:
		# Not silent: a snapshot that always fails means no request is ever recorded. Its own
		# text (the record hook has another) and the failure counter the health check reads.
		activity_log._fail("request snapshot failed", e)
		_note("failures")


def _client(request) -> str:
	try:
		value = (request.headers.get(_CLIENT_HEADER) or "").strip().lower()
	except Exception:
		return ""
	return value if value in _CLIENTS else ""


def _is_upload_name(name: str) -> bool:
	return name == "upload_file" or name.endswith(".upload_file")


def _resolve(name: str):
	"""The function ``frappe.handler.get_attr`` would run for ``name``: dotted names through
	``frappe.get_attr``, a bare name from ``frappe.handler`` itself (the ``upload_file``
	shorthand). ``None`` when it does not resolve."""
	try:
		if "." in name:
			return frappe.get_attr(name)
		return vars(frappe.handler).get(name)
	except Exception:
		return None


def _canonical(fn) -> str:
	module = getattr(fn, "__module__", None)
	qualname = getattr(fn, "__qualname__", None)
	if not isinstance(module, str) or not isinstance(qualname, str):
		return ""
	return f"{module}.{qualname}"


def _safe_code(value) -> str:
	return value if isinstance(value, str) and _SAFE_CODE.match(value) else ""


def _envelope_failure(response):
	"""``None`` for a response that is not a failure envelope, else the envelope's error code
	(``""`` when it carries none). Looks only at a small, non-streamed JSON body."""
	try:
		if (response.mimetype or "") != "application/json":
			return None
		if getattr(response, "direct_passthrough", False) or getattr(response, "is_streamed", False):
			return None
		length = response.content_length
		if length is not None and length > _MAX_BODY:
			return None
		data = response.get_data()
		if len(data) > _MAX_BODY or not _OK_FALSE.search(data):
			return None
		body = json.loads(data)
		if not isinstance(body, dict):
			return None
		# v1 wraps the return value in ``message``; v2 in ``data``.
		for key in ("message", "data"):
			payload = body.get(key)
			if isinstance(payload, dict) and payload.get("ok") is False:
				error = payload.get("error")
				return _safe_code(error.get("code")) if isinstance(error, dict) else ""
	except Exception:
		return None
	return None


def _outcome(response, status: int) -> tuple[str, str]:
	"""``(outcome, code)``: 2xx and 3xx ok (a redirect is how an OAuth callback succeeds),
	403 denied, anything else failed; a 2xx whose body is an ``ok: false`` envelope failed.
	``code`` is a class name or error code, never a message."""
	if status < 400:
		code = _envelope_failure(response)
		return ("failed", code or "ok_false") if code is not None else ("ok", "")
	exc_type = _safe_code((getattr(frappe.local, "response", None) or {}).get("exc_type"))
	if status == 403:
		return "denied", exc_type or "PermissionError"
	return "failed", exc_type or f"HTTP {status}"


def _expand_doctype(method: str, doctype: str | None) -> str:
	"""v2 ``/method/<doctype>/<m>`` runs ``<doctype controller module>.<m>`` (api/v2.py)."""
	if not doctype:
		return method
	from frappe.modules.utils import load_doctype_module

	return load_doctype_module(doctype).__name__ + "." + method


def _ref_of(entry, form) -> tuple[str, str]:
	"""``(ref_doctype, ref_name)`` for a LOGGED entry. The DocType is the declared one, or the
	request's own value when the entry names a parameter and that value IS a DocType or
	Report. The name is only the declared id parameter, only when that record exists."""
	_, id_param, _event, spec, _rows = entry
	ref_doctype = ""
	if isinstance(spec, str) and spec.startswith("@"):
		value = form.get(spec[1:]) if form is not None else None
		ref_doctype = activity_log.ref_doctype_of(None, {"doctype": value})
	elif spec:
		ref_doctype = spec
	# A successful delete leaves ref_name blank: the existence check runs after the delete, so the
	# record is gone. The label still records the kind of action and the actor.
	ref_name = ""
	if id_param and ref_doctype and form is not None:
		value = form.get(id_param)
		if isinstance(value, str) and 0 < len(value) <= 140:
			try:
				ref_name = str(frappe.db.exists(ref_doctype, value) or "")
			except Exception:
				ref_name = ""
	return ref_doctype, ref_name


def record_request(response=None, request=None) -> None:
	"""``after_request``: record the LOGGED endpoint this request ran, if any.

	Runs after Frappe finished the request transaction. A 2xx/3xx was already committed, so the
	hook only inserts and commits its own row; for a failure it first rolls back whatever the
	failed request left. Never raises: an after_request hook that throws is only logged by
	Frappe, and the user's call must not be touched by it."""
	snap = getattr(frappe.local, _SNAPSHOT, None)
	try:
		frappe.local.jarvis_activity_req = None
		if snap is not None:
			_record(snap, response, request)
	except Exception as e:
		activity_log._fail("request not recorded", e)
		_note("failures")


def _record(snap, response, request) -> None:
	"""Decide, in the cheapest order, whether this request is a LOGGED endpoint and write it.

	Free checks first (no response, status, user), then the name: ``_expand_doctype`` is free
	for the plain forms and loads the doctype's controller module only for the v2
	``/method/<doctype>/<m>`` form (the name is not known without it). A request that is not a
	``jarvis.`` method (or a header-marked upload) is dropped before ``frappe.get_attr``."""
	if response is None:  # v15 app.py can leave no response after an HTTPException
		return
	status = int(getattr(response, "status_code", 0) or 0)
	if not 100 <= status < 600 or status in _NEVER_RAN:
		return
	session = getattr(frappe.local, "session", None)
	user = session.user if session else None
	if not user or user == "Guest" or not getattr(frappe.local, "db", None):
		return

	# Cheap prefilter before ``frappe.get_attr`` or any query: only a jarvis method or an upload
	# (and an upload only when the SPA/PWA shim marked it) can be LOGGED.
	try:
		name = frappe.override_whitelisted_method(_expand_doctype(snap.method, snap.doctype))
	except Exception:
		return  # a doctype that has no controller module: Frappe answered 4xx, nothing to log
	if not name.startswith("jarvis.") and not (_is_upload_name(name) and _client(request)):
		return

	fn = _resolve(name)
	if fn is None:
		return
	if fn is frappe.handler.upload_file:
		# Stock Frappe upload (any route to it: /api/method/upload_file, the dotted path,
		# /api/v2/method/upload_file). Desk uploads are not Jarvis (D5): only the SPA/PWA
		# shim's header makes it one.
		if not _client(request):
			return
		entry = ("LOGGED", None, "action", None, 0)
		key, label = UPLOAD_METHOD, UPLOAD_LABEL
	else:
		key = _canonical(fn)
		entry = CLASSIFICATION.get(key)
		if not entry or entry[0] != "LOGGED":
			return
		label = label_for(key)

	outcome, code = _outcome(response, status)
	_note("seen")  # every LOGGED request, even one flood control drops: health compares it with the rows
	if status >= 400:
		# Mirrors api_errors.capture_failed_request: drop what a failed request left, so this commit
		# cannot persist half of it. That also drops any other app's uncommitted after_request work
		# on this request, which is why a SUCCESS (already committed by Frappe) is never rolled
		# back: insert and commit only.
		frappe.db.rollback()
	ref_doctype, ref_name = _ref_of(entry, snap.form)
	flooded = _floods(entry, outcome)
	if flooded and not _flood_allows(user, key, ref_doctype, ref_name, outcome):
		_note("dropped")
		# Throttled by ``_fail`` (one line per site per five minutes): the counts are in the health block.
		activity_log._fail("flood control dropped rows", event=entry[2])
		return
	name = activity_log.record(
		entry[2],
		actor=user,
		method=key,
		label=label,
		ref_doctype=ref_doctype,
		ref_name=ref_name,
		row_count=entry[4] if outcome == "ok" else 0,
		outcome=outcome,
		code=code,
	)
	if name is None:
		_note("failures")  # record() already file-logged why; the health check counts it
		if flooded:
			_forget_dedupe(user, key, ref_doctype, ref_name, outcome)  # the next identical call may succeed
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- after_request runs past the request commit
