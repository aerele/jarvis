import { onMounted, onActivated, onDeactivated } from "vue";
import { useShellStore } from "@/stores/shell";

// Staleness window for a pane that SettingsDialog keeps alive across tab switches.
const STALE_MS = 30000;

// Loads on mount, then refreshes in place (never blanking the pane) when the
// pane is re-shown and its data is older than STALE_MS, was never loaded
// successfully, or the AI models pane changed the config since (llmConfigVersion).
// `load` returns false when the load failed, so a failed one is retried on the
// next activation; a refresh is ignored while another is in flight.
export function useKeptAlive(load) {
	const store = useShellStore();
	let loadedAt = 0;
	let version = store.llmConfigVersion;
	let inflight = false;
	let active = true;

	async function refresh() {
		if (inflight) return;
		inflight = true;
		const v = store.llmConfigVersion;
		try {
			if ((await load()) !== false) {
				loadedAt = Date.now();
				version = v;
			}
		} finally {
			inflight = false;
		}
	}
	const isStale = () =>
		!loadedAt || version !== store.llmConfigVersion || Date.now() - loadedAt > STALE_MS;

	onMounted(refresh);
	onActivated(() => {
		active = true;
		if (isStale()) refresh();
	});
	onDeactivated(() => {
		active = false;
	});
	return {
		isActive: () => active,
		markStale: () => {
			loadedAt = 0;
		},
	};
}
