"""Shared worker seams reuse the canonical claims, prompt and placeholder writer."""

import json
import time

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import policy, prepare, txn
from jarvis.chat import turn_state as ts
from jarvis.chat.collaboration_bindings import require_live
from jarvis.chat.collaboration_context import require_prompt_budget
from jarvis.chat.collaboration_runtime import configured_skills_root, history_watermark, resolve_runtime
from jarvis.chat.collaboration_turn import binding_for_run, turn_context
from jarvis.chat.collaboration_usage import UsagePending
from jarvis.exceptions import AgentUnreachableError


def _fail_prepare(binding, run_id, version, assistant, error, *, retry_allowed=False):
	# Roll back abandoned reads/claims before the final fenced error transaction.
	uncertain = frappe.db.get_value(c.BINDING, binding.name, "state") == "Uncertain"
	frappe.db.rollback()
	frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
	row = frappe.db.get_value(ts.TURN, run_id, ["state", "version"], as_dict=True, for_update=True)
	if uncertain:
		frappe.db.set_value(c.BINDING, binding.name, "state", "Uncertain")
	if row and version is None and row.state == "queued":
		if ts.cancel_queued(run_id, int(row.version)):
			# Persist the refused queued preparation before this job returns.
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
	elif row and row.state == "preparing" and int(row.version) == version:
		frappe.db.set_value(
			ts.TURN, run_id, "collaboration_retry_allowed", int(retry_allowed and not uncertain)
		)
		prepare._prepare_error(
			run_id,
			version,
			assistant,
			binding.conversation,
			binding.actor,
			error,
			code="permission" if "access" in error else "validation",
		)
	else:
		# Persist the retained uncertainty fence even when no preparation can proceed.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {"ok": False, "reason": "shared_prepare_refused"}


def prepare_shared(run_id: str, relay_target_id: str | None = None) -> dict:
	binding = binding_for_run(run_id)
	if binding is None:
		c._deny()
	if not c.writes_ready():
		# A queued job delivered after the operator pause stays queued. Its
		# reservation can expire/reclaim; the normal queue-age limit still holds.
		return {"ok": True, "skipped": "collaboration_paused"}
	version, assistant = None, None
	try:

		def claim():
			require_live(binding)
			return prepare._claim_preparing_unit(run_id)

		claimed = txn.replay_on_conflict(claim, label="collaboration.prepare", fresh=True)
		if not claimed.get("claimed"):
			return {"ok": True, "skipped": claimed.get("skipped")}
		version = int(claimed["turn"]["version"]) + 1
		with turn_context(binding, run_id):
			won, assistant = txn.replay_on_conflict(
				lambda: prepare._attach_placeholder_unit(binding.conversation, run_id, version),
				label="collaboration.placeholder",
				fresh=True,
			)
			if not won:
				return {"ok": True, "skipped": "claim_lost"}
			payload = build_payload(binding, run_id, claimed["turn"]["seed_message"])

			def ready():
				require_live(binding)
				if not ts.store_dispatch_payload(run_id, version, json.dumps(payload)) or not ts.mark_ready(
					run_id, version
				):
					frappe.db.rollback()
					return False
				frappe.db.commit()
				return True

			if not txn.replay_on_conflict(ready, label="collaboration.ready", fresh=True):
				return {"ok": True, "skipped": "claim_lost"}
			from jarvis.chat import pump

			target = relay_target_id or frappe.db.get_value(ts.TURN, run_id, "relay_target_id")
			pump.ensure_pump(target)
			pump.lpush_wake(target, run_id)
			return {"ok": True, "ready": True}
	except frappe.PermissionError:
		return _fail_prepare(
			binding,
			run_id,
			version,
			assistant,
			"Your shared-chat access or isolated session changed. Keep your draft and check the request before retrying.",
		)
	except UsagePending as exc:
		return _fail_prepare(binding, run_id, version, assistant, str(exc), retry_allowed=True)
	except frappe.ValidationError as exc:
		return _fail_prepare(binding, run_id, version, assistant, str(exc))
	except AgentUnreachableError:
		return _fail_prepare(
			binding,
			run_id,
			version,
			assistant,
			"The connection was temporarily unavailable. Your message is kept; retry this request shortly.",
			retry_allowed=True,
		)
	except Exception:
		# Do not log a raw prompt, gateway transcript or another actor's data.
		return _fail_prepare(
			binding,
			run_id,
			version,
			assistant,
			"This shared request could not be prepared. Your message is kept; try again after checking access and the connection.",
		)


def build_payload(binding, run_id: str, seed_message: str) -> dict:
	"""One actor prompt/runtime/budget builder for both approved transports."""
	from jarvis.chat import agent_session_pool, turn_handler

	conv = frappe.get_doc(c.CONVERSATION, binding.conversation)
	attachments = prepare._load_attachments(run_id)
	ap = turn_handler.assemble_prompt(
		conv,
		message_id=seed_message,
		conversation_id=binding.conversation,
		context=None,
		attachments=attachments,
		user=binding.actor,
		binding=binding,
		run_id=run_id,
	)
	managed = turn_handler._to_managed_attachments(ap.vision_parts) if ap.vision_parts else None
	# Permission/history reads take locks; no gateway RPC runs under them.
	# Release permission and history locks before gateway preparation RPCs.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	root = configured_skills_root()
	with agent_session_pool.checkout(ap.settings.agent_url or "") as sess:
		from jarvis.chat.collaboration_usage import require_previous_usage

		require_previous_usage(binding, run_id, sess)
		runtime = resolve_runtime(sess, binding, skills_root=root)
		# End runtime-verification reads before requesting the native history watermark.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		watermark = history_watermark(sess, binding)
	if ap.shared_parts is not None:
		from jarvis.chat.collaboration_context import fit_prompt_history

		ap.user_message = fit_prompt_history(
			ap.shared_parts,
			attachments=managed or [],
			context_tokens=runtime.context_tokens,
			runtime_reserve=runtime.runtime_reserve,
		)
	require_prompt_budget(
		ap.user_message,
		attachments=managed or [],
		context_tokens=runtime.context_tokens,
		runtime_reserve=runtime.runtime_reserve,
	)
	ok, reason = policy.validate_can_send(binding.actor, model=runtime.policy.model_ref.split("/", 1)[1])
	if not ok:
		frappe.throw("This shared request cannot start: " + (reason or "usage limit"))
	payload = {
		"session_key": binding.session_key,
		"message": ap.user_message,
		"chat_user": binding.actor,
		"thinking": None,
		"attachments": managed,
		"attachments_raw": attachments,
		"context": None,
		"drained_note_ids": ap.drained_ids,
		"shared_history_watermark": watermark,
		"turn_start_ms": int(time.time() * 1000),
		# The ack window the pump waits after the frame is written, sized from this
		# actor's assembled prompt exactly as personal prepare does (#773).
		"ack_timeout_s": turn_handler._ack_timeout_s(len(managed or []), ap.inlined_prompt_chars),
		"collaboration_runtime": {
			"skills_root": runtime.policy.skills_root,
			"model_ref": runtime.policy.model_ref,
			"physical_id": runtime.physical_id,
			"context_tokens": runtime.context_tokens,
			"runtime_reserve": runtime.runtime_reserve,
		},
	}
	return payload
