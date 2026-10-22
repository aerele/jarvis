import { computed, reactive } from "vue";
import { sendSharedMessage, uploadSharedFile } from "@/api/chatCollaboration";
import { __ } from "@/lib/i18n";
import { errMessage } from "@/lib/errors";
import { newSendRequestId } from "@/lib/sendDelivery";
import { sharedResultMessage } from "@/lib/sharedActionResult";

// State lives only in this mounted view. Each asynchronous operation owns its
// captured chat/state; navigating cannot apply its result to a different draft.
export function useSharedComposer({ id, canChat, refresh }) {
	const states = new Map();
	const stateFor = (key) => {
		if (!states.has(key))
			states.set(
				key,
				reactive({
					draft: "",
					files: [],
					sending: false,
					error: "",
					attempt: null,
					accepted: null,
					stopped: null,
					failed: null,
				})
			);
		return states.get(key);
	};
	const state = computed(() => stateFor(id.value));
	const draft = computed({
		get: () => state.value.draft,
		set: (value) => {
			state.value.draft = value;
		},
	});
	const files = computed(() => state.value.files);
	const sending = computed(() => state.value.sending);
	const error = computed(() => state.value.error);
	const fingerprintOf = (text, files) =>
		JSON.stringify([text, files.map((f) => ({ file_url: f.file_url }))]);
	const canSend = computed(
		() =>
			canChat.value &&
			!state.value.sending &&
			!state.value.accepted &&
			(!state.value.stopped ||
				fingerprintOf(state.value.draft, state.value.files) !==
					state.value.stopped.attempt.fingerprint) &&
			!state.value.files.some((f) => f.uploading || f.failed) &&
			(Boolean(state.value.draft.trim()) || state.value.files.length > 0)
	);
	async function send(previous = null) {
		const key = id.value,
			s = stateFor(key);
		if (!canChat.value || s.sending || s.accepted || (!previous && !canSend.value)) return;
		const body = previous || { text: s.draft, files: s.files.map((f) => ({ ...f })) };
		if (new TextEncoder().encode(body.text).length > 16384) {
			s.error = __("This message is too long for a shared chat. Shorten it and try again.");
			return;
		}
		const attachments = body.files.map((f) => ({ file_url: f.file_url }));
		const fingerprint = fingerprintOf(body.text, body.files);
		const attempt = previous ||
			[s.attempt, s.failed].find((saved) => saved?.fingerprint === fingerprint) || {
				...body,
				fingerprint,
				nonce: newSendRequestId(),
			};
		s.attempt = attempt;
		s.sending = true;
		s.error = "";
		try {
			const result = await sendSharedMessage(key, attempt.text, attempt.nonce, attachments);
			if (!result?.ok || !result.run_id) {
				s.error = sharedResultMessage(result);
				return;
			}
			s.accepted = { run_id: result.run_id, attempt };
			s.stopped = null;
			s.failed = null;
			if (id.value === key) await refresh();
		} catch (exception) {
			if (exception?.status >= 400 && exception.status < 500) {
				s.failed = null;
				s.error = errMessage(exception);
				if (id.value === key) refresh();
				return;
			}
			s.failed = attempt;
			s.error =
				__(
					"The send was not confirmed. Your draft is kept. Retry sends the same request once."
				) +
				" " +
				errMessage(exception, "");
			if (id.value === key) refresh();
		} finally {
			s.sending = false;
		}
	}
	function reconcile(turns) {
		const s = state.value;
		if (!s.accepted && s.stopped) {
			const resumed = turns.find((t) => t.run_id === s.stopped.run_id);
			if (resumed && !["errored", "cancelled"].includes(resumed.state))
				adopt(resumed.run_id);
		}
		if (!s.accepted) return;
		const turn = turns.find((t) => t.run_id === s.accepted.run_id);
		if (!turn) return;
		if (["errored", "cancelled"].includes(turn.state)) {
			s.error =
				(turn.error_headline ? turn.error_headline + " " : "") +
				__(
					"The request stopped. Use Retry for this request, or edit your draft to send a new request after checking its outcome."
				);
			s.stopped = s.accepted;
			s.accepted = null;
			s.attempt = s.stopped.attempt;
		} else if (["streaming", "terminal_observed", "finalizing", "done"].includes(turn.state)) {
			const sent = s.accepted.attempt;
			if (s.draft === sent.text) s.draft = "";
			const keys = new Set(sent.files.map((f) => f.key));
			s.files = s.files.filter((f) => !keys.has(f.key));
			s.stopped = null;
			s.accepted = null;
			s.attempt = null;
		}
	}
	function adopt(runId) {
		const s = state.value;
		if (s.stopped?.run_id !== runId) return;
		s.accepted = s.stopped;
		s.attempt = s.stopped.attempt;
		s.error = "";
	}
	async function addFiles(picked) {
		const key = id.value,
			s = stateFor(key);
		if (!canChat.value || s.sending || s.accepted) return;
		if (s.files.length + picked.length > 10) {
			s.error = __("Choose at most ten attachments.");
			return;
		}
		await Promise.all(
			Array.from(picked).map(async (file) => {
				const item = reactive({
					key: newSendRequestId(),
					file_name: file.name,
					uploading: true,
					removable: true,
				});
				s.files.push(item);
				try {
					Object.assign(item, await uploadSharedFile(key, file));
				} catch (exception) {
					item.failed = true;
					item.error = errMessage(
						exception,
						__("Upload failed. Remove this file and attach it again.")
					);
				} finally {
					item.uploading = false;
				}
			})
		);
	}
	function removeFile(index) {
		if (!state.value.sending && !state.value.accepted) state.value.files.splice(index, 1);
	}
	return {
		draft,
		files,
		sending,
		error,
		canSend,
		send,
		reconcile,
		adopt,
		acceptedRun: computed(() => state.value.accepted?.run_id || null),
		addFiles,
		removeFile,
		waiting: computed(() => Boolean(state.value.accepted)),
		failed: computed(() => Boolean(state.value.failed)),
		retryPrevious: () => state.value.failed && send(state.value.failed),
	};
}
