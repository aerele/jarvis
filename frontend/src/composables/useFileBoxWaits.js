import { computed, onScopeDispose, ref, watch } from "vue";
import { humanizeFieldname } from "@/lib/chatAsk";

const normalize = (value) =>
	String(value || "")
		.replace(/\s+/g, " ")
		.trim();

// Keep the Board's authority and sheet editor. This state owns only the current
// chat's read model and the lifetime of its in-chat review.
export function useFileBoxWaits({ conversation, activeAsk, load, onChanged }) {
	const items = ref([]);
	const reviewSession = ref(null);
	let fileBox = false;
	let generation = 0;
	let sessionId = 0;
	let timer = null;
	let alive = true;
	const visibleItems = computed(() => {
		const label = normalize(
			(activeAsk.value?.questions || [])
				.map((q) => q.q || humanizeFieldname(q.fieldname))
				.join("\n")
		);
		return items.value.filter(
			(item) =>
				!label ||
				item.src !== "ar" ||
				item.source !== "Chat" ||
				normalize(item.question) !== label
		);
	});

	function reset() {
		generation++;
		clearTimeout(timer);
		timer = null;
		fileBox = false;
		items.value = [];
		reviewSession.value = null;
	}
	watch(conversation, reset, { flush: "sync" });
	onScopeDispose(() => {
		alive = false;
		reset();
	});

	async function refresh(id, isFileBox) {
		if (!alive || id !== conversation.value) return;
		const request = ++generation;
		fileBox = !!isFileBox;
		if (!id || !fileBox) {
			items.value = [];
			return;
		}
		try {
			const result = await load(id);
			if (alive && request === generation && conversation.value === id)
				items.value = result?.items || [];
		} catch {
			// Preserve the known list. The native decision API rejects stale actions.
		}
	}

	function onEvent(event) {
		if (!alive || !fileBox || !conversation.value) return;
		if (!["approval:new", "action:settled", "sheet:progress"].includes(event.kind)) return;
		if (event.kind === "sheet:progress" && event.state === "applying") return;
		const conv = event.conversation_id || event.conversation;
		if (
			conv !== conversation.value &&
			!(!conv && items.value.some((item) => item.name === event.name))
		)
			return;
		if (timer !== null) return;
		const id = conversation.value;
		timer = setTimeout(() => {
			timer = null;
			refresh(id, fileBox);
		}, 150);
	}

	function openReview(item) {
		if (!alive || !fileBox || item.src !== "sheet") return;
		reviewSession.value = {
			id: ++sessionId,
			conversation: conversation.value,
			name: item.name,
		};
	}
	function isCurrent(session) {
		return (
			alive && reviewSession.value === session && conversation.value === session.conversation
		);
	}
	function reviewChanged(session) {
		if (!isCurrent(session)) return;
		onChanged?.();
		refresh(session.conversation, fileBox);
	}
	function closeReview(session) {
		if (!isCurrent(session)) return;
		reviewChanged(session);
		reviewSession.value = null;
	}
	return {
		items,
		visibleItems,
		refresh,
		onEvent,
		reviewSession,
		openReview,
		reviewChanged,
		closeReview,
	};
}
