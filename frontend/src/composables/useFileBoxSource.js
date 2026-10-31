import { computed, onScopeDispose, ref, watch } from "vue";
import { getFileBoxSourceState, setFileBoxSource } from "@/api/fileboxSource";

// This control owns no transcript, composer or approval state.
export function useFileBoxSource(props) {
	const state = ref(null);
	const loading = ref(false);
	const saving = ref(false);
	const error = ref("");
	let generation = 0;
	let alive = true;
	const current = computed(() => state.value?.source?.file_url === props.attachment.file_url);
	const valid = (id) => alive && id === generation;
	const session = () => ({
		conversation: props.conversation,
		message: props.message,
		item: props.attachment.name,
		file_url: props.attachment.file_url,
	});
	const read = ({ conversation, message, item, file_url }) =>
		getFileBoxSourceState(conversation, { message, item, file_url });
	async function load() {
		const id = ++generation;
		loading.value = true;
		error.value = "";
		try {
			const response = await read(session());
			if (valid(id)) state.value = response;
		} catch {
			if (valid(id)) error.value = "Could not check the source file. Try again.";
		} finally {
			if (valid(id)) loading.value = false;
		}
	}
	async function select() {
		if (
			loading.value ||
			saving.value ||
			!state.value ||
			current.value ||
			state.value.candidate_available === false
		)
			return;
		const id = generation;
		const selection = { ...session(), expected: state.value.version };
		saving.value = true;
		error.value = "";
		try {
			const response = await setFileBoxSource(selection);
			if (!valid(id)) return;
			state.value = response;
			if (!response.ok)
				error.value =
					response.reason === "busy"
						? "Wait for the current operation to finish, then try again."
						: "The source changed. Check it and try again.";
		} catch {
			// The write may have succeeded. A read can confirm it without replay.
			try {
				const response = await read(selection);
				if (!valid(id)) return;
				state.value = response;
				if (!current.value) error.value = "Could not select this source. Try again.";
			} catch {
				if (valid(id)) error.value = "Could not confirm the source. Check it again.";
			}
		} finally {
			if (valid(id)) saving.value = false;
		}
	}
	watch(
		() => [
			props.conversation,
			props.message,
			props.attachment.name,
			props.attachment.file_url,
		],
		() => {
			state.value = null;
			saving.value = false;
			load();
		},
		{ immediate: true, flush: "sync" }
	);
	onScopeDispose(() => {
		alive = false;
		generation++;
	});
	return { state, loading, saving, error, current, load, select };
}
