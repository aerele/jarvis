"""Explicit classification of every Jarvis whitelisted callable.

The names below are static: a newly imported endpoint fails completeness until
reviewed here. Non-chat classification never skips the generic value sweep.
Chat invokers use real owner fixture identifiers, including nested payloads.
"""

ENDPOINTS = {}


def _unrelated(module, reason, names):
	for name in names.split():
		ENDPOINTS[f"{module}.{name}"] = {"scope": "not_chat_scoped", "reason": reason}


_unrelated(
	"jarvis.account",
	"Workspace billing, subscription and LLM connection metadata; no transcript lookup.",
	"""
    cancel_plan_at_period_end
    cancel_scheduled_downgrade
    check_billing_payment_status
    download_invoice
    get_account
    get_billing_payment_state
    get_billing_profile
    get_invoices
    get_llm_apply_operation
    get_llm_connection_health
    get_llm_connection_status
    get_llm_usage
    get_subscription_status
    is_onboarded
    is_ready_for_chat
    preview_downgrade
    preview_upgrade
    reauthorize_autopay
    resume_plan
    start_downgrade
    start_upgrade
    stop_autopay_to_pay
    update_billing_details
    """,
)

_unrelated(
	"jarvis.api",
	"Agent credential administration; tool dispatch is separately classified as chat-scoped.",
	"""
    rotate_agent_token
    """,
)

_unrelated(
	"jarvis.api_errors",
	"Receives caller-supplied client telemetry; does not resolve conversation identifiers.",
	"""
    report_client_errors
    """,
)

_unrelated(
	"jarvis.branding",
	"Admin-gated workspace branding fields, independent of conversations.",
	"""
    get_branding
    update_branding
    """,
)

_unrelated(
	"jarvis.chat.actions_api",
	"Doctype form metadata and linked rule values use ERP read permissions, not chat grants.",
	"""
    get_doctype_form_meta
    rule_values
    """,
)

_unrelated(
	"jarvis.chat.admission",
	"Operator-gated pump configuration/cutover, not authority derived from a shared identifier.",
	"""
    pump_cutover_execute
    pump_cutover_preflight
    pump_set_transport_mode
    """,
)

_unrelated(
	"jarvis.chat.agent_models",
	"Tenant-wide model selection for installed agents, admin-gated for changes.",
	"""
    get_agent_model
    get_eligible_models
    reset_agent_model
    set_agent_model
    """,
)

_unrelated(
	"jarvis.chat.agents_api",
	"Agent catalogue, installations, findings and agent runs have their own owner/admin boundaries.",
	"""
    apply_agents
    demote_installation
    get_agent
    get_agent_admin_overview
    get_agents_caps
    get_agents_sync_status
    get_ap_review_defaults
    get_ar_review_defaults
    get_bank_recon_review_defaults
    get_installations
    install_agent
    list_agent_activity_page
    list_agents
    list_agents_page
    list_findings
    list_run_steps
    list_runs
    list_runs_page
    promote_installation
    raise_activation_ceiling
    run_agent_now
    search_users
    set_agent_access
    set_agent_roles
    set_config
    set_enabled
    set_finding_state
    set_listing_status
    set_operator_visibility
    set_run_as_user
    set_schedule
    stop_agent_run
    take_finding_to_chat
    uninstall_agent
    """,
)

_unrelated(
	"jarvis.chat.api",
	"Workspace catalogue/search/prewarm or doctype metadata, not a conversation-directed operation.",
	"""
    get_chat_ui_settings
    get_doctype_fields
    get_model_catalog_ui
    list_tools
    search_workspace
    warm_session
    """,
)

_unrelated(
	"jarvis.chat.app_learning_api",
	"Manage-gated custom-app learning configuration and jobs, independent of chat grants.",
	"""
    cancel_app_learning_run
    get_app_learning_overview
    list_app_learning_runs_page
    list_custom_apps
    schedule_app_learning
    """,
)

_unrelated(
	"jarvis.chat.custom_skills_api",
	"Custom skill owner/reviewer/sharing model is separate from conversation grants.",
	"""
    apply_custom_skills
    create_custom_skill
    decide_skill_promotion
    delete_custom_skill
    delete_custom_skills_bulk
    dismiss_skill_notices
    file_box_doctypes
    get_custom_skill
    get_custom_skills_sync_status
    get_skill_shares
    list_custom_skills
    list_custom_skills_page
    list_shareable_users
    list_skill_promotion_requests
    my_skill_promotion
    preflight_skill_promotion
    promotable_target_roles
    request_skill_promotion
    share_custom_skill
    update_custom_skill
    """,
)

_unrelated(
	"jarvis.chat.dashboards_api",
	"Dashboard ownership and publication records are separate; conversation-specific reads are classified separately.",
	"""
    delete_dashboard
    get_dashboard
    get_dashboards_caps
    list_dashboards_page
    preview_dashboard_source
    run_dashboard_source
    save_dashboard
    """,
)

_unrelated(
	"jarvis.chat.dev_seed",
	"Developer/admin seed operation; no recipient authority follows from a conversation id.",
	"""
    seed_feature_pages
    seed_varied_approvals
    """,
)

_unrelated(
	"jarvis.chat.device",
	"Current-user device/push registration, not transcript access.",
	"""
    rotate_chat_device
    """,
)

_unrelated(
	"jarvis.chat.feedback",
	"Current-user pulse feedback has no conversation lookup.",
	"""
    pulse_context
    submit_pulse_feedback
    """,
)

_unrelated(
	"jarvis.chat.filebox",
	"Skill capability check, not a conversation read or mutation.",
	"""
    check_skill
    """,
)

_unrelated(
	"jarvis.chat.greeting",
	"Current-user greeting/workspace readiness; no conversation identifier lookup.",
	"""
    dismiss_greeting
    hide_greeting
    maybe_greet
    """,
)

_unrelated(
	"jarvis.chat.learned_api",
	"Learned-item reviewer/configuration boundaries are independent of chat ownership.",
	"""
    acknowledge_learned_pattern
    apply_insight_skill_update
    apply_learned_skills
    approve_learned_pattern
    batch_approve
    decide_promotion
    draft_insight_skill_update
    flag_learned_default
    get_learned_apply_status
    get_learned_pattern
    get_learning_settings
    get_learning_status
    get_review_access
    go_to_chat_context
    list_learned_patterns_page
    list_promotion_requests_page
    pending_learned_count
    polish_learned_draft
    reject_learned_pattern
    restore_rejected_pattern
    run_pattern_analysis_now
    set_learning_settings
    snooze_learned_pattern
    trigger_followup_question
    unapprove_learned_pattern
    """,
)

_unrelated(
	"jarvis.chat.learned_skills_api",
	"Learned-skill catalogue and reviewer controls, not shared conversation authority.",
	"""
    get_learned_skills_sync_status
    """,
)

_unrelated(
	"jarvis.chat.list_filters",
	"Caller-owned saved list filters, not conversation records.",
	"""
    get_list_filter_schema
    list_filters_capabilities
    """,
)

_unrelated(
	"jarvis.chat.macros_admin_api",
	"Admin-only macro governance; a plain recipient acquires no admin authority.",
	"""
    admin_delete
    admin_get_macro
    admin_handover
    admin_handover_targets
    admin_hold
    admin_list_macros
    admin_macro_owners
    admin_release
    """,
)

_unrelated(
	"jarvis.chat.macros_api",
	"Caller-owned macro definitions/history; direct linked-run reads/stops are classified separately.",
	"""
    create_macro
    delete_macro
    delete_macros_bulk
    dismiss_macro_notices
    get_macro
    get_new_macro_arming
    list_macro_runs
    list_macros
    list_macros_page
    macro_run_stats
    run_macro
    summarize_macro
    update_macro
    """,
)

_unrelated(
	"jarvis.chat.pending_actions._ops",
	"Explicit operator-gated pending-action recovery; share membership grants no operator role.",
	"""
    operator_fail
    operator_settle
    """,
)

_unrelated(
	"jarvis.chat.personalise_api",
	"Caller-owned notes/questions and admin question rules; separate ownership model.",
	"""
    answer_question
    delete_note
    delete_question
    delete_question_rule
    generate_chat_questions_now
    get_note
    get_personalisation_settings
    get_question
    get_skills_area_caps
    ignore_question
    list_notes_page
    list_question_rules
    list_questions_page
    list_role_options
    save_note
    save_question_rule
    set_personalisation_settings
    """,
)

_unrelated(
	"jarvis.chat.proactive",
	"Creates a message for the current user from caller-supplied text, not a foreign-chat lookup.",
	"""
    demo_proactive
    """,
)

_unrelated(
	"jarvis.chat.skills_admin_api",
	"Admin-only skill governance, independent of chat grants.",
	"""
    admin_delete
    admin_get_skill
    admin_handover
    admin_handover_targets
    admin_hold
    admin_list_skills
    admin_release
    admin_skill_owners
    """,
)

_unrelated(
	"jarvis.chat.triggers_api",
	"Organisation trigger manage/read boundaries, not chat ownership.",
	"""
    activity_stats
    create_trigger
    delete_trigger
    delete_triggers_bulk
    get_trigger
    get_triggers_caps
    list_activity_page
    list_triggers_page
    set_trigger_enabled
    test_trigger_condition
    update_trigger
    """,
)

_unrelated(
	"jarvis.chat.user_settings_api",
	"Caller preferences and admin usage controls, separate from conversation access.",
	"""
    admin_list_user_usage
    admin_set_user_limit
    admin_set_user_model_limit
    admin_sync_usage
    get_capability_catalog
    get_my_settings
    get_prompt_suggestions
    log_capability_pick
    set_sidebar_order
    set_user_theme
    update_my_settings
    """,
)

_unrelated(
	"jarvis.chat.voice",
	"Transcribes the caller-supplied audio upload; no conversation lookup.",
	"""
    transcribe_audio
    """,
)

_unrelated(
	"jarvis.chat.voice_notes_api",
	"Caller-owned voice note records; the conversation-linked save is classified separately.",
	"""
    delete_voice_note
    get_business_status
    list_my_voice_notes_page
    process_voice_notes_now
    update_voice_note
    """,
)

_unrelated(
	"jarvis.chat.wiki",
	"Wiki scope/role write matrix is independent of chat sharing; conversation nudge classified separately.",
	"""
    add_wiki_link
    archive_wiki_page
    create_wiki_page
    delete_wiki_page
    get_wiki_caps
    get_wiki_graph
    get_wiki_graph_history
    get_wiki_page
    list_wiki_pages_page
    my_wiki_promotion
    remove_wiki_link
    request_wiki_promotion
    restore_wiki_page
    run_wiki_lint_now
    save_wiki_page
    set_knowledge_language
    sync_wiki_mirror_now
    """,
)

_unrelated(
	"jarvis.chat.wiki_graph",
	"Admin-only wiki mirror sync, not transcript read access.",
	"""
    sync_now
    """,
)

_unrelated(
	"jarvis.diagnostics",
	"Operator/admin infrastructure diagnostics, not recipient conversation access.",
	"""
    chat_recovery_stats
    force_resync
    import_announce_stats
    ping_admin
    ping_agent
    reset_agent_pairing
    """,
)

_unrelated(
	"jarvis.llm_key_probe",
	"Admin-gated caller-supplied LLM credential probe, not conversation data.",
	"""
    test_llm_api_key
    """,
)

_unrelated(
	"jarvis.maintenance_notice",
	"Workspace maintenance metadata fetched independently of conversations.",
	"""
    check
    """,
)

_unrelated(
	"jarvis.mobile.auth",
	"Current-user device credentials, not foreign-chat authority.",
	"""
    get_mobile_token
    get_pairing_qr
    get_pwa_qr
    list_mobile_devices
    revoke_all_mobile_devices
    revoke_mobile_device
    """,
)

_unrelated(
	"jarvis.oauth.api",
	"Admin/current-user OAuth capture and connection state, independent of transcript grants.",
	"""
    begin_claude_cli_login
    begin_paste_signin
    begin_pool_account_signin
    cancel_claude_cli_login
    cancel_pending_capture
    complete_claude_cli_login
    complete_paste_signin
    complete_pool_account_signin
    disconnect
    get_direct_subscription_status
    list_pending_captures
    poll_pool_account_signin
    """,
)

_unrelated(
	"jarvis.onboarding",
	"Workspace onboarding/account/LLM administration; no transcript resolution.",
	"""
    capture_onboarding_lead
    check_account_reconnect
    check_signup_payment_state
    check_signup_payment_status
    disconnect_llm
    finish_payment
    get_account_defaults
    get_company_onboarding_defaults
    get_llm_config
    get_llm_sync_status
    get_onboarding_state
    get_preset_catalog
    get_terms_url
    gstin_autofill
    gstin_autofill_available
    initiate_signup_payment
    list_payment_providers
    list_plans
    reconnect_available
    redeem_reconnect_code
    renew
    request_workspace_reset
    reset_onboarding
    resync_llm
    save_llm_creds
    save_llm_pool
    start_account_reconnect
    start_signup
    sync_connection
    update_billing
    workspace_reset_state
    """,
)

_unrelated(
	"jarvis.pdf_templates",
	"Template administration and owner-gated generated-document re-render, separate from conversation shares.",
	"""
    delete_pdf_template
    get_default_pdf_template
    get_pdf_template
    list_pdf_templates
    pdf_template_options
    preview_pdf_template
    rerender_document
    save_pdf_template
    set_default_pdf_template
    """,
)

_unrelated(
	"jarvis.preflight",
	"Workspace readiness checks; no transcript lookup.",
	"""
    run_chat_preflight
    """,
)

_unrelated(
	"jarvis.release_notice",
	"Workspace release metadata, independent of conversations.",
	"""
    check
    notes
    """,
)

_unrelated(
	"jarvis.subscription_health",
	"Workspace subscription notices; no transcript read.",
	"""
    get_subscription_notice
    """,
)

_unrelated(
	"jarvis.support.api",
	"Identity-scoped support ticket proxy; supplied ids are ticket inputs, never Jarvis conversation lookups.",
	"""
    awaiting_count
    close_ticket
    create_ticket
    get_thread
    list_tickets
    reply
    """,
)

_unrelated(
	"jarvis.support.media",
	"Identity-scoped Helpdesk attachment proxy; local chat attachments are not read here.",
	"""
    download
    upload
    """,
)

_unrelated(
	"jarvis.tools._export.jobs",
	"Background export lookup and cancel by export id; owner-only (System Manager reads), no conversation lookup.",
	"""
    cancel
    get_jobs
    """,
)

# Explicit, signature-bound invokers for conversation-directed paths.
ENDPOINTS.update(
	{
		"jarvis.api.call_tool": {"arguments": {"tool": "get_doc"}, "scope": "chat_scoped"},
		"jarvis.chat.actions_api.apply_action": {
			"arguments": {
				"action": {
					"conversation": "$conversation",
					"doctype": "Jarvis Conversation",
					"name": "$conversation",
					"values": {"title": "recipient edit"},
					"verb": "update",
				}
			},
			"scope": "chat_scoped",
		},
		"jarvis.chat.actions_api.approve_and_run": {
			"arguments": {"conversation": "$conversation", "token": "$token"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.actions_api.confirm_tool": {
			"arguments": {"conversation": "$conversation", "token": "$token"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.actions_api.dismiss_tool": {
			"arguments": {"conversation": "$conversation", "token": "$token"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.actions_api.draft_computed": {
			"arguments": {
				"action": {
					"columns": {},
					"conversation": "$conversation",
					"doctype": "Jarvis Conversation",
					"name": "$conversation",
					"values": {"title": "recipient edit"},
				}
			},
			"scope": "chat_scoped",
		},
		"jarvis.chat.actions_api.list_pending_confirmations": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.actions_api.load_doc": {
			"arguments": {"doctype": "Jarvis Conversation", "name": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.actions_api.propose_next_action": {
			"arguments": {
				"action": "save",
				"conversation": "$conversation",
				"doctype": "Jarvis Conversation",
				"kind": "update",
				"name": "$conversation",
			},
			"scope": "chat_scoped",
		},
		"jarvis.chat.admission.active_turn_for_conversation": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.admission.cancel_queued_turn": {"arguments": {"run_id": "$run"}, "scope": "chat_scoped"},
		"jarvis.chat.admission.queue_position": {"arguments": {"run_id": "$run"}, "scope": "chat_scoped"},
		"jarvis.chat.api.archive_conversation": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.clear_chat_history": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.api.compact_conversation": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.create_conversation": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.api.create_or_focus_empty": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.api.get_canvas": {
			"arguments": {"message": "$message", "name": "artifact"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.get_conversation": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.get_conversation_context": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.get_tool_activity": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.get_tool_detail": {"arguments": {"message": "$message"}, "scope": "chat_scoped"},
		"jarvis.chat.api.get_usage": {"arguments": {"conversation": "$conversation"}, "scope": "chat_scoped"},
		"jarvis.chat.api.list_conversations": {"arguments": {"refresh": True}, "scope": "chat_scoped"},
		"jarvis.chat.api.preview_file": {"arguments": {"file_url": "$file_url"}, "scope": "chat_scoped"},
		"jarvis.chat.api.rename_conversation": {
			"arguments": {"conversation": "$conversation", "title": "recipient edit"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.retry_message": {"arguments": {"message": "$message"}, "scope": "chat_scoped"},
		"jarvis.chat.api.search_conversations": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.api.send_message": {
			"arguments": {"conversation": "$conversation", "message": "$message"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.set_conversation_model": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.set_conversation_thinking": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.set_star": {
			"arguments": {"conversation": "$conversation", "starred": 1},
			"scope": "chat_scoped",
		},
		"jarvis.chat.api.stop_run": {
			"arguments": {"conversation": "$conversation", "run_id": "$run"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.apply_sheet": {"arguments": {"name": "$approval"}, "scope": "chat_scoped"},
		"jarvis.chat.approvals_api.approve_wiki_write": {
			"arguments": {"name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.decide": {
			"arguments": {"approve": 1, "decision": "approve", "name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.decide_held_action": {
			"arguments": {"action": "skip", "name": "$pending_action"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.discard_sheet": {
			"arguments": {"name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.dismiss_approval": {
			"arguments": {"name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.edit_and_create_held": {
			"arguments": {"name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.get_approval": {
			"arguments": {"name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.get_pending_action": {
			"arguments": {"name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.get_sheet_candidates": {
			"arguments": {"name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.list_approvals": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.approvals_api.list_approvals_page": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.approvals_api.list_pending_actions_lane": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.approvals_api.list_wiki_write_proposals": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.approvals_api.pending_badge": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.approvals_api.pending_count": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.approvals_api.reject_wiki_write": {
			"arguments": {"name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.restore_approval": {
			"arguments": {"name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.approvals_api.retry_wiki_write": {
			"arguments": {"name": "$approval"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.dashboards_api.dashboard_for_conversation": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.dashboards_api.list_dashboard_conversations": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.docmeta_api.add_comment": {
			"arguments": {
				"content": "recipient edit",
				"doctype": "Jarvis Conversation",
				"name": "$conversation",
			},
			"scope": "chat_scoped",
		},
		"jarvis.chat.docmeta_api.delete_attachment": {
			"arguments": {"doctype": "Jarvis Conversation", "file": "$file", "name": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.docmeta_api.delete_comment": {
			"arguments": {"comment": "$comment"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.docmeta_api.get_docmeta": {
			"arguments": {"doctype": "Jarvis Conversation", "name": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.docmeta_api.toggle_assignment": {
			"arguments": {"doctype": "Jarvis Conversation", "name": "$conversation", "user": "$recipient"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.docmeta_api.toggle_like": {
			"arguments": {"doctype": "Jarvis Conversation", "name": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.docmeta_api.toggle_share": {
			"arguments": {"doctype": "Jarvis Conversation", "name": "$conversation", "user": "$recipient"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.docmeta_api.update_comment": {
			"arguments": {"comment": "$comment", "content": "recipient edit"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.export.download_conversation": {
			"arguments": {"conversation": "$conversation", "format": "md"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.feedback.session_feedback_status": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.feedback.submit_feedback": {
			"arguments": {"message_id": "$message", "rating": "up"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.feedback.submit_session_feedback": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.filebox.bulk_rerun_inbound": {
			"arguments": {"conversations": ["$conversation"]},
			"scope": "chat_scoped",
		},
		"jarvis.chat.filebox.clear_processed_inbound": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.filebox.delete_inbound": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.filebox.delete_inbound_bulk": {
			"arguments": {"conversations": ["$conversation"]},
			"scope": "chat_scoped",
		},
		"jarvis.chat.filebox.drop_file": {"arguments": {"file_url": "$file_url"}, "scope": "chat_scoped"},
		"jarvis.chat.filebox.list_inbound_page": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.filebox.open_waits": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.filebox.rerun_inbound": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.macros_admin_api.admin_stop_run": {
			"arguments": {"run": "$macro_run"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.macros_api.get_macro_run": {"arguments": {"run": "$macro_run"}, "scope": "chat_scoped"},
		"jarvis.chat.macros_api.stop_macro_run": {"arguments": {"run": "$macro_run"}, "scope": "chat_scoped"},
		"jarvis.chat.pwa_send.check_delivery": {
			"arguments": {"request_id": "$request_id"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.pwa_send.send_message": {
			"arguments": {
				"conversation": "$conversation",
				"message": "$message",
				"request_id": "$request_id",
			},
			"scope": "chat_scoped",
		},
		"jarvis.chat.report_runs.chat_report_runs": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.send_requests.check_delivery": {
			"arguments": {"request_id": "$request_id"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.send_requests.send_message": {
			"arguments": {
				"conversation": "$conversation",
				"message": "$message",
				"request_id": "$request_id",
			},
			"scope": "chat_scoped",
		},
		"jarvis.chat.sharing.get_shared_canvas": {
			"arguments": {"message": "$message", "name": "artifact"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.sharing.get_shared_conversation": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.sharing.get_shared_file": {
			"arguments": {"conversation": "$conversation", "file_url": "$file_url"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.sharing.get_shared_tool_detail": {
			"arguments": {"message": "$message"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.sharing.get_shares": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.sharing.get_sharing_setting": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.sharing.is_shared_with_me": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.sharing.list_all_shares": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.sharing.list_shared_with_me": {"arguments": {}, "scope": "chat_scoped"},
		"jarvis.chat.sharing.revoke_share": {"arguments": {"name": "$share"}, "scope": "chat_scoped"},
		"jarvis.chat.sharing.set_shares": {
			"arguments": {"conversation": "$conversation", "users": ["$outsider"]},
			"scope": "chat_scoped",
		},
		"jarvis.chat.sharing.set_sharing_setting": {"arguments": {"enabled": False}, "scope": "chat_scoped"},
		"jarvis.chat.sharing.share_candidates": {"arguments": {"query": "share-"}, "scope": "chat_scoped"},
		"jarvis.chat.voice_notes_api.save_voice_note": {
			"arguments": {"conversation": "$conversation", "transcript": "recipient note"},
			"scope": "chat_scoped",
		},
		"jarvis.chat.wiki.dismiss_nudge": {
			"arguments": {"conversation": "$conversation"},
			"scope": "chat_scoped",
		},
	}
)
