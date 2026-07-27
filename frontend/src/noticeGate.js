// Release notice, delivered by the www/jarvis.py boot payload as
// window.release_notice = {active, version, message}.
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

// Hard gate: no dismiss. It lifts only when the control plane stops serving the
// notice (this tenant updated, or the operator retired it).
export const showNotice = computed(() => notice.active && !cleared.value);

// Boot reads a mirror that may predate the tenant's update, and an open tab never
// re-reads it at all, so the gate re-pulls from admin itself.
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
