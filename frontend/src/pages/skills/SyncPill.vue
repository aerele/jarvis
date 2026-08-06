<template>
	<div v-if="pending || failed" class="inline-flex items-center gap-2">
		<Tooltip v-if="pending" text="Updating your assistant… (restarts briefly, ~30s)">
			<Badge theme="orange" variant="subtle" size="lg">
				<template #prefix>
					<JvSpinner />
				</template>
				Applying skills…
			</Badge>
		</Tooltip>
		<!-- the failure + Retry belong to whoever can act on it; to everyone
		     else an org-wide red "Apply failed" is an unfixable-looking error -->
		<template v-else-if="isSM">
			<Tooltip :text="failureReason || 'Applying skills to your assistant failed.'">
				<Badge theme="red" variant="subtle" label="Apply failed" />
			</Tooltip>
			<Button variant="ghost" label="Retry" :loading="applying" @click="apply" />
		</template>
		<template v-else>
			<Tooltip
				text="The last skills sync didn't complete. An administrator can retry it; your saved changes are safe."
			>
				<Badge theme="orange" variant="subtle" label="Skills sync delayed" />
			</Tooltip>
		</template>
	</div>
</template>

<script setup>
// SyncPill - shared skills-apply status pill (DESIGN-V3 §5.6 + §6.2): shown by
// the Skills list banner and the skill detail header. Polls
// getCustomSkillsSyncStatus every 3s while an apply is pending; a failed apply
// turns the pill red with a Retry ghost button (applyCustomSkills).
// Exposed API:
//   apply()    - trigger an apply then poll (save/delete flows; those
//                endpoints don't enqueue an apply on their own)
//   checkNow() - read the status and poll if pending (bulk delete - the
//                server already enqueued the apply, §8.3)
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { Badge, Button, Tooltip, toast } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import * as api from "@/api";
import { humaniseSyncStatus } from "@/lib/syncStatus";
import { errMessage as errMsg } from "@/lib/errors";

const status = ref(""); // last_sync_status: "", "pending: …", "ok …", "failed: …"
const pending = ref(false);
const applying = ref(false);
const isSM = !!window.is_system_manager;

// Prefix parsing is shared (@/lib/syncStatus) so this pill, the agents list and
// the AI models pane all read the same raw string the same way, and a reason long
// enough to be a traceback gets flattened before it reaches a tooltip.
const human = computed(() => humaniseSyncStatus(status.value));
const failed = computed(() => human.value.kind === "failed");
const failureReason = computed(() => human.value.detail);

let timer = null;
function startPoll() {
	if (timer) return;
	timer = setInterval(async () => {
		await load();
		if (!pending.value) stopPoll();
	}, 3000);
}
function stopPoll() {
	if (timer) {
		clearInterval(timer);
		timer = null;
	}
}

async function load() {
	try {
		const s = (await api.getCustomSkillsSyncStatus()) || {};
		status.value = s.last_sync_status || "";
		pending.value = !!s.pending;
	} catch (e) {
		// best-effort: a transient status failure must not break the page
	}
}

async function checkNow() {
	await load();
	if (pending.value) startPoll();
}

async function apply() {
	if (applying.value) return;
	applying.value = true;
	try {
		await api.applyCustomSkills();
		status.value = "pending: applying skills";
		pending.value = true;
		startPoll();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		applying.value = false;
	}
}

onMounted(() => checkNow());
onBeforeUnmount(() => stopPoll());

defineExpose({ apply, checkNow, pending });
</script>
