import { computed, onBeforeUnmount, ref } from "vue";
import * as userAgentsApi from "@/api/userAgents";
import { errMessage } from "@/lib/errors";
import { isSyncing } from "@/lib/userAgents";

// The My agents list and its flag. A sync runs after every definition change, so a
// row that is Pending with no reason yet is re-read every few seconds until the
// server settles it (AgentsList's apply-status poll, same 3 s); the poll stops on
// its own after POLL_LIMIT reads so a runtime that never answers cannot keep the tab
// hitting the server for ever.
const POLL_MS = 3000;
const POLL_LIMIT = 60;

export function useUserAgents() {
	// `enabled` stays false until list_user_agents says otherwise, and after a failed
	// probe: the tab is hidden then, never shown half-working.
	const enabled = ref(false);
	const maxAgents = ref(5);
	const agents = ref([]);
	const loading = ref(false);
	const error = ref("");
	let timer = null;
	let polls = 0;
	// Whether a read ever answered. Until then a failure hides the tab; after it, a
	// failed reload keeps the tab and the list that is on screen.
	let loaded = false;

	const atCap = computed(() => agents.value.length >= maxAgents.value);

	async function load({ quiet = false } = {}) {
		if (!quiet) loading.value = true;
		try {
			const res = (await userAgentsApi.listUserAgents()) || {};
			enabled.value = !!res.enabled;
			if (res.max_agents) maxAgents.value = res.max_agents;
			agents.value = res.agents || [];
			error.value = "";
			loaded = true;
		} catch (e) {
			if (!loaded) {
				enabled.value = false;
				agents.value = [];
			}
			error.value = errMessage(e);
		} finally {
			if (!quiet) loading.value = false;
		}
		schedulePoll();
	}

	function schedulePoll() {
		const needed = agents.value.some(isSyncing);
		if (!needed) {
			stopPoll();
			polls = 0;
			return;
		}
		if (timer || polls >= POLL_LIMIT) return;
		timer = setTimeout(async () => {
			timer = null;
			polls += 1;
			await load({ quiet: true });
		}, POLL_MS);
	}

	function stopPoll() {
		if (timer) clearTimeout(timer);
		timer = null;
	}

	// A write returned the agent: put it in the list without another read.
	function upsert(agent) {
		const i = agents.value.findIndex((a) => a.name === agent.name);
		if (i >= 0) agents.value.splice(i, 1, agent);
		else agents.value.push(agent);
		polls = 0;
		schedulePoll();
	}

	function remove(name) {
		agents.value = agents.value.filter((a) => a.name !== name);
	}

	onBeforeUnmount(stopPoll);

	return { enabled, maxAgents, agents, loading, error, atCap, load, upsert, remove };
}
