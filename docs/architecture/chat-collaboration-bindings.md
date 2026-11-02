# Collaboration foundation (C1)

C1 adds identity storage and compatibility guards. It does **not** enable
collaborative sends or change the read-only shared UI. All existing grants default
to `View`. The existing membership-only sharing API keeps its response shape and
preserves an existing recipient's access mode/revision. Missing or unknown access
modes have effective `View` access. There is no mode-changing endpoint in C1.

## Identity and authority

`Jarvis Chat Binding` is an immutable identity ledger for one logical turn (one
new message plus its confirmation/continuation chain). It binds conversation,
initiating actor, logical run ID, opaque session key, conversation epoch and the
recipient's grant ID/revision. Actor and grant names are Data snapshots so retained audit does not pin live grants/users against revocation/deletion; the new binding validates its actor exists. Existing Session.user Link behavior is unchanged. An owner's binding has no recipient grant. Its
session key uses the reserved `:dashboard:collab:` namespace; it never replaces
`Jarvis Conversation.session_key`. `Jarvis Chat Session.collaboration_binding`
links the authentication lookup to this ledger. Message and Turn rows have additive
server-owned actor/binding fields, blank for legacy private turns.

`resolve_binding` returns an immutable **identity**, not an authorization decision.
It refuses missing, inconsistent, uncertain, retired, stale-epoch and stale-grant
bindings. C2 must additionally check live user/roles, sharing/rollout flags,
conversation eligibility, turn state and the dispatch/confirmation revocation
fence. No C1 caller dispatches using this resolver. All legacy plugin dispatch
refuses reserved, marked or ledger-backed sessions and legacy owner sessions of a
collaboration-marked thread. Losing/rebuilding an authentication row cannot make
the ledger-backed key legacy again. No general Jarvis-role restriction is added
to delegated legacy sessions.

Generic ORM writes cannot stamp/clear/rebind these identity fields, including as
Administrator or with `ignore_permissions`. Only an internal service with the
explicit server flag can insert a binding; bindings cannot be deleted through
the ORM. Existing identities cannot be edited, even with that flag. C2 must use
narrow, fenced server transitions for mutable state and never erase the ledger.

A durable conversation marker survives the last Chat grant's removal. In C1,
legacy raw-history/tool/file/export and execution APIs refuse marked threads
after retaining their original owner check. Owner share management and
archive/rename/star controls remain available. Generic Message/Turn/Conversation
reads are denied for marked threads, including their owner and System Manager.
Owner Conversation write/delete permission is retained for moderation, without
generic raw read permission. Shared readers retain their existing
explicit projected endpoint. Session generic reads exclude collaboration keys,
markers and ledger rows. Administrator remains Frappe's trusted superuser: it
bypasses permission hooks; this is not an end-user collaboration capability.
Pending Action already has no generic user permissions. The legacy approvals
board/detail/action paths must not expose or spend collaborative payloads.

## Expand, enable and rollback

1. Deploy C1 schema and code with collaboration off. Post-model-sync backfill is
   additive and idempotent: unset grants become View/revision 1; populated modes
   and positive revisions are preserved. No sessions/history/owner fields change.
2. Inventory **all** web, RQ, pump, scheduler and realtime processes. Every one
   must run at least C1 before a binding may be created. Only then may an operator
   attest `chat_collaboration_reader_floor: 1` in site config. This is an operator
   attestation, **not automatic fleet/version detection**; do not set it for a
   mixed deployment. Schema readiness is checked separately and fails closed.
3. `chat_collaboration_enabled` is a separate site-config flag, absent/off by
   default. C1's compiled writer capability is **0**, so even setting both config
   keys cannot enable writes. No C1 public service creates bindings or activates
   conversations. C2/C3, explicit phase authorization and all real-runtime/security
   acceptance gates are required before writable rollout.
4. Turn the flag off before rollback. Supported rollback targets retain at least
   the C1 guards. Before rollback to C1, collaborative non-terminal turns must be drained and uncertain bindings reconciled; the preflight refuses otherwise. C1 does not resume collaborative work, project raw results to the
   owner, reuse an owner session, delete binding evidence or guess that an uncertain
   external operation failed. Reconciliation remains necessary before retry.
5. Run `jarvis.chat.collaboration.assert_rollback_floor(target_floor)` as an operator
   preflight. It refuses rollback to C1 with undrained work or an enabled flag, and conservatively refuses below C1 if the flag is on or **any**
   binding/marked-conversation evidence remains, even retired evidence. This is
   not a git/deployment interception mechanism. An audited below-C1 migration
   (flag off, no active/uncertain bindings, retired auth, retained evidence and a
   demonstrated safe read policy) requires a separately reviewed procedure. There
   is no destructive downgrade helper. Do not drop additive columns/ledger rows.

## Consumer inventory and C2 obligations

Inventory base: `00cd1561370c218037bad88438f7ced10a3c037d` plus this foundation.
The table lists production functions with `session_key`, `conv.owner` or
`conversation.owner` references, including SQL strings and nested functions;
`(module)` denotes documentation/constants. Transport primitives carry keys but
have no database authorization context. They are **legacy-unchanged**, not an
independent permission boundary. C2 must route every shared invocation through
its canonical binding-aware service before those primitives. No frontend-supplied
actor/run/key is authoritative.

Classifications: **binding-resolved** is the identity resolver only;
**disabled** means C1 refuses marked conversations/sessions at the named boundary;
**legacy-unchanged** retains private/delegated behavior, with no shared entry
point in C1. C2 must not interpret an unchanged consumer as shared-ready. In
particular, canonical history, author-aware projection, confirmations/asks/notes,
realtime, callbacks, native-tool isolation and binding-specific retention are C2
work, not promised by additive fields.

| Consumer | C1 classification / boundary; C2 requirement |
|---|---|
| `jarvis/_plugin_auth.py`: `(module)`, `_canonical_request`, `_enforce_rate_limit`, `_should_audit_unsigned_reject`, `_validate_signature`, `validate_plugin_request` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/admin_client.py`: `post_agent_run` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/api.py`: `_dispatch_from_session`, `_file_box_wiki_write`, `_notify_stale_pairing`, `_parse_args`, `_persist_and_publish_tool_call`, `call_tool` | disabled — call_tool / _dispatch_from_session reject marked/ledger sessions before impersonation; _run_tool rejects marked conversations. C2 must route receipts and confirmation principals by binding. |
| `jarvis/chat/agent_client.py`: `(module)`, `chat_abort`, `chat_send`, `clear_session_model`, `compact_session`, `delete_session`, `fire_agent`, `get_history`, `get_session_messages`, `is_run_active`, `relay_turn_events`, `relay_yield_continuation`, `set_session_model`, `stream_agent_turn`, `subscribe_session` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/chat/agent_runs.py`: `record_delegate_run`, `teardown_run_session` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/chat/agent_scheduler.py`: `_audit_prompt`, `_launch_audit`, `_mint_run_session`, `_stale_candidates`, `_terminalize_failed`, `_terminalize_stuck_run`, `fail_run` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/chat/agents_api.py`: `_stop_running_run`, `_try_abort_gateway_session` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/chat/api.py`: `_conversation_history`, `_enqueue_turn`, `_ensure_session_key`, `compact_conversation`, `send_message`, `stop_run` | disabled — owner check unchanged; targeted raw-read/execution and _enqueue_turn guards; share management and owner moderation preserved. _conversation_history remains an internal projection input for sharing; never expose its raw result for collaborative owners. C2 adds explicit shared APIs. |
| `jarvis/chat/chat_permissions.py`: `has_session_permission`, `session_query_conditions` | disabled — raw Conversation/Message/Turn/Session/Approval permission hooks exclude collaborative state. Explicit shared projection remains the reader boundary. |
| `jarvis/chat/collaboration.py`: `(module)`, `is_collaborative_session`, `legacy_session_user`, `resolve_binding` | binding-resolved — identity only; all C1 execution refused. C2 adds live authorization and dispatch fences. |
| `jarvis/chat/compaction.py`: `_gateway_session_row`, `_last_turn_usage`, `_session_row`, `context_payload`, `run_compact`, `run_compact._write`, `write_compaction_result` | disabled — start_compaction and run_compact refuse marked threads. C2 needs binding-aware context/compaction, never a global owner session. |
| `jarvis/chat/export.py`: `_markdown` | legacy-unchanged — explicit owner API guard remains; shared export not exposed. C2 must review actor/control projection before any new read entry. |
| `jarvis/chat/feature_usage.py`: `_dashboard_builder` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/chat/feedback.py`: `_session_uuid`, `_turns_since`, `_user_messages_between`, `submit_feedback` | disabled — entry points check ownership and explicitly refuse legacy-session feedback for marked threads. C2 must bind feedback/usage to actor; aggregate counters are legacy-unchanged. |
| `jarvis/chat/filebox_skills.py`: `_record`, `gate_get_skill`, `target_refusal` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/chat/finalize.py`: `_effect_usage`, `_effect_usage.unit` | disabled — run_finalize refuses marked threads before any effects. C2 must attribute usage, notes, title and pending payloads to the binding. |
| `jarvis/chat/held_writes.py`: `_refusal_reason` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/chat/macros.py`: `_post_closing_message`, `reap_stale_macro_runs` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/chat/pending_actions/_execute.py`: `authorize`, `execute`, `_execute_locked` | disabled — authorize rejects collaborative rows or adoption targets before claim, unseal or dispatch; applies to individual, batch, typed-confirm and approve-and-run paths. |
| `jarvis/chat/pending_actions/_sheet.py`: `_identity_refusal` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/chat/prepare.py`: `run_prepare` | disabled — run_prepare refuses before claim/binding/prompt. C2 supplies actor identity, own model/profile, sanitized history and memory exclusions. |
| `jarvis/chat/pump.py`: `(module)`, `_cancel_sweep`, `_dispatch_one`, `_issue_recovery_tail`, `_load_dispatch`, `_local_active_session_keys`, `_parse_snapshot_frame`, `_reattach_lane`, `_reattach_or_recover`, `_reconcile_gateway_active`, `_reconcile_on_start`, `_yield_wait_sweep`, `drain_slice` | disabled — dispatch, resend, reconcile, reattach, recovery-tail and settlement guards; no owner fallback. C2 must cover ack/continuation adoption, cancel, recovery and uncertainty on the canonical binding. |
| `jarvis/chat/relay_mux.py`: `__init__`, `_route_event`, `abort`, `await_continuation`, `register_run`, `release_continuation`, `send_chat`, `set_session_model` | legacy-unchanged — transport/reader has no Frappe context; pump entry/adoption guards exclude marked threads. C2 must preserve binding identity through run rekey/yield/release; do not put database authorization on the reader thread. |
| `jarvis/chat/session_lifecycle.py`: `(module)`, `_delete_gateway_session`, `_free_idle_sessions`, `_reap_empty`, `_reap_orphans`, `reclaim_throwaway_session` | disabled — gateway delete/reclaim refuses collaborative sessions, retaining auth/ledger for reconciliation. C2 supplies bounded binding retention; empty-chat/legacy housekeeping unchanged. |
| `jarvis/chat/session_pin_sweep.py`: `_clear`, `_conversations_by_session_key`, `_verify` | disabled — classification skips collaborative keys; no global conversation pin applied. C2 needs actor-specific pin rules. |
| `jarvis/chat/shared_files.py`: `read_file` | legacy-unchanged — existing live-share gated, scrubbed/static common-file read. C2 must authorize actor uploads before deliberate group disclosure. |
| `jarvis/chat/stale_scan.py`: `(module)`, `_scan_one_row`, `scan_and_mark_errored` | disabled — marked rows skipped by both streaming and orphan scans. C2 must reconcile without owner attribution or blind redispatch. |
| `jarvis/chat/title.py`: `(module)` | disabled — enqueue/maybe_autotitle skip marked threads. C2 needs sanitized title input, never private owner context. |
| `jarvis/chat/turn_handler.py`: `_harvest_post_yield_media`, `_note_empty_reply`, `handle_chat_send` | disabled — direct RQ handler rejects marked conversations before owner execution. C2 must supply immutable actor, prompt and callback/recovery binding. |
| `jarvis/chat/turn_recovery.py`: `_active_map`, `_recover_one`, `recover_now`, `recover_pending_turns` | disabled — scheduler, immediate and per-row recovery refuse marked threads; no owner transcript lookup. C2 needs per-logical-turn session recovery. |
| `jarvis/chat/usage.py`: `(module)`, `_refresh_session_context_snapshot`, `_write_budget_fields`, `_write_turn_usage_row`, `fetch_fresh_session_row`, `record_turn_usage`, `refresh_session_snapshots` | disabled — record_turn_usage returns retry for collaborative keys. Snapshot refresh is identity metadata only; C2 must attribute fresh turns and quota to initiating actor. |
| `jarvis/chat/usage_push.py`: `_by_profile` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/learning/chat_mining.py`: `_build_batches` | legacy-unchanged — C2 must exclude collaborative raw rows from private preference mining and automatic personal context. |
| `jarvis/learning/polish.py`: `_run_gateway_turn` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/learning/source_guard.py`: `outbound_violations` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/preflight.py`: `_drain_probe_stream._drain` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/_agent_run_ctx.py`: `(module)`, `clear_session_key`, `get_session_key`, `set_session_key` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/_app_learning_ctx.py`: `(module)`, `resolve_scribe_run` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/_delegate_capability.py`: `(module)`, `resolve`, `tool_denial` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/_delegate_write_caps.py`: `(module)`, `resolve_delegate` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/_save_guards.py`: `(module)`, `require_session_key` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/_user_memory.py`: `_seen_key` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/finish_app_learning_run.py`: `(module)`, `finish_app_learning_run` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/get_bank_recon_inputs.py`: `get_bank_recon_inputs` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/get_engagement_config.py`: `(module)`, `get_engagement_config` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/get_purchase_match_inputs.py`: `get_purchase_match_inputs` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/get_receivables_review_inputs.py`: `get_receivables_review_inputs` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/list_app_modules.py`: `(module)` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/read_app_source.py`: `(module)` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/record_agent_run.py`: `(module)`, `record_agent_run` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/record_app_wiki.py`: `(module)`, `record_app_wiki` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/save_agent_dashboard.py`: `(module)`, `save_agent_dashboard` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |
| `jarvis/tools/save_dashboard.py`: `(module)`, `save_dashboard` | legacy-unchanged — private/delegated/non-shared consumer only in C1; shared callbacks cannot reach it through C1 plugin/turn gates. C2 must explicitly audit actor, context and lifecycle before any shared use. |

Additional boundaries without those literal references: `actions_api` guards owner actions and legacy confirmed dispatch; `pending_actions/_execute.authorize` refuses collaborative rows/adoption before claim/unseal across individual, batch and typed confirms; `approvals_api` / Approval permission hooks exclude collaborative raw approvals; `settlement.invoke_settlement` refuses marked turns before effects. `chat_asks`, pending notes, native memory/bootstrap, final events and gateway context budgets require C2 actor-aware implementation/probes. File Box, armed macro and agent-initiated origins remain ineligible; no C1 shared writer can enter them.

C1 review follow-ups retained for C2: the explicit workspace “also delete my data” reset must define binding-ledger erasure/export policy before writable rollout (C1 creates none publicly). The legacy approvals list currently filters collaborative rows after its limit, which can shorten a rollback-mode page but never returns their raw fields. Repeated per-row marker checks favor a fail-closed boundary; C2 must batch/cache safely without stale revocation authorization. No shared writable rollout is approved with these lifecycle/performance questions unaddressed.
