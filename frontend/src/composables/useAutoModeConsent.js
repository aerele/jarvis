import { useShellStore } from "@/stores/shell";
import { useConfirm } from "@/composables/useConfirm";
import { AUTO_MODE_COPY } from "@/lib/autoMode";

// Shared first-time gate for turning auto mode on (composer toggle and the
// Settings default). Resolves true when the user may proceed: either they have
// already seen the warning (auto_mode_acknowledged), or they just confirmed it,
// which records the acknowledgement. Resolves false on Cancel. Turning auto mode
// off never goes through here.
export function useAutoModeConsent() {
	const store = useShellStore();
	const { confirm } = useConfirm();
	async function ensureAutoModeConsent({ save = true } = {}) {
		if (store.autoModeAcknowledged) return true;
		const ok = await confirm({
			title: AUTO_MODE_COPY.dialogTitle,
			message: AUTO_MODE_COPY.dialogMessage,
			warning: AUTO_MODE_COPY.dialogWarning,
			confirmLabel: AUTO_MODE_COPY.dialogConfirm,
			cancelLabel: AUTO_MODE_COPY.dialogCancel,
			danger: true,
		});
		if (!ok) return false;
		store.acknowledgeAutoMode({ save });
		return true;
	}
	return { ensureAutoModeConsent };
}
