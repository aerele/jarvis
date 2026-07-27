// Release notice for the mobile PWA, delivered by jarvis_mobile.py boot as
// window.release_notice = {active, version, message}. Mirrors src/noticeGate.js.
import { computed, ref } from "vue";
import { call } from "frappe-ui";

const n = window.release_notice || {};

export const notice = {
	active: !!n.active,
	version: (n.version || "").trim(),
	message: n.message || "",
};

const cleared = ref(false);
export const checking = ref(false);

// Hard gate: no dismiss. It lifts only when the control plane stops serving it.
export const showNotice = computed(() => notice.active && !cleared.value);

// The PWA never calls the chat-readiness gate, so without this it would stay
// blocked until the daily sync even after the tenant updated.
export async function recheck() {
	if (checking.value) return;
	checking.value = true;
	try {
		const fresh = await call("jarvis.release_notice.check");
		if (fresh && !fresh.active) {
			cleared.value = true;
			window.location.reload();
		}
	} catch (e) {
		/* offline or admin unreachable - keep the gate up and retry later */
	} finally {
		checking.value = false;
	}
}
