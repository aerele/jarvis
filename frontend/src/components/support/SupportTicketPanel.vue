<template>
	<aside class="jv-suptp">
		<!-- Read-only "Ticket details" panel (Helpdesk TicketCustomerSidebar). Reads
		     store.thread.meta (carried by get_thread), so it works for deep-linked
		     tickets too. Every field falls back to "—" when meta is absent/loading.
		     (Comment kept inside the root to avoid a multi-root fragment.) -->
		<div class="jv-suptp-head">Ticket details</div>

		<div class="jv-suptp-sec jv-suptp-sec-b">
			<!-- Identity: the COUNTERPARTY (who you're talking to), not the requester
			     — the viewer IS the requester, so echoing their own email is noise. -->
			<div class="jv-suptp-id">
				<span class="jv-suptp-avatar" aria-hidden="true">
					<JarvisMark :size="26" :radius="7" />
				</span>
				<span class="jv-suptp-name">Aerele Support</span>
				<Button
					v-if="canReply"
					class="jv-suptp-reply"
					variant="subtle"
					size="sm"
					label="Reply"
					@click="emit('open')"
				/>
			</div>

			<div class="jv-suptp-row">
				<span class="jv-suptp-label">Ticket ID</span>
				<span class="jv-suptp-value" :class="{ 'jv-suptp-empty': !meta?.name }">
					{{ meta?.name || "—" }}
				</span>
			</div>
			<div class="jv-suptp-row">
				<span class="jv-suptp-label">Status</span>
				<span class="jv-suptp-value">
					<Badge
						v-if="meta?.status"
						variant="subtle"
						:theme="statusBadge.theme"
						:label="statusBadge.label"
					/>
					<span v-else class="jv-suptp-empty">—</span>
				</span>
			</div>
			<div v-for="s in slaRows" :key="s.title" class="jv-suptp-row">
				<span class="jv-suptp-label">{{ s.title }}</span>
				<span class="jv-suptp-value">
					<Badge
						v-if="s.badge"
						variant="subtle"
						:theme="s.badge.theme"
						:label="s.badge.label"
					/>
					<span v-else class="jv-suptp-empty">—</span>
				</span>
			</div>
		</div>

		<div class="jv-suptp-sec jv-suptp-scroll">
			<div v-for="f in fieldRows" :key="f.label" class="jv-suptp-row">
				<span class="jv-suptp-label">{{ f.label }}</span>
				<span class="jv-suptp-value" :class="{ 'jv-suptp-empty': !f.value }">
					{{ f.value || "—" }}
				</span>
			</div>
		</div>
	</aside>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { Badge, Button } from "frappe-ui";
import JarvisMark from "@/components/JarvisMark.vue";
import { useSupportStore } from "@/stores/support";
import { toLocalMs } from "@/utils/datetime";
import { firstResponseBadge, resolutionBadge } from "@/lib/supportSla";

const emit = defineEmits(["open"]);
const store = useSupportStore();

const meta = computed(() => store.thread.meta);

// "Due in …" ticks locally (no refetch) — recompute the badges every 60s. get_thread
// re-reads meta only on open/focus/after-send, which for an idle ticket is rare.
const nowMs = ref(Date.now());
let timer = null;
onMounted(() => {
	timer = setInterval(() => (nowMs.value = Date.now()), 60000);
});
onUnmounted(() => {
	if (timer) clearInterval(timer);
});

const canReply = computed(() => !!meta.value && !store.isClosed(meta.value.status));
const statusBadge = computed(() => store.badgeFor(meta.value && meta.value.status));

const slaRows = computed(() => {
	const m = meta.value;
	if (!m)
		return [
			{ title: "First Response", badge: null },
			{ title: "Resolution", badge: null },
		];
	return [
		{
			title: "First Response",
			badge: firstResponseBadge(
				{
					firstRespondedOn: toLocalMs(m.first_responded_on),
					responseBy: toLocalMs(m.response_by),
					creation: toLocalMs(m.creation),
				},
				nowMs.value
			),
		},
		{
			title: "Resolution",
			badge: resolutionBadge(
				{
					resolutionDate: toLocalMs(m.resolution_date),
					resolutionBy: toLocalMs(m.resolution_by),
					agreementStatus: m.agreement_status,
					resolutionTime: m.resolution_time, // seconds (a duration, not a datetime)
				},
				nowMs.value
			),
		},
	];
});

const fieldRows = computed(() => {
	const m = meta.value || {};
	return [
		{ label: "Subject", value: m.subject },
		{ label: "Team", value: m.agent_group },
		{ label: "Priority", value: m.priority },
	];
});
</script>

<style scoped>
.jv-suptp {
	display: flex;
	flex-direction: column;
	width: 320px;
	flex: 0 0 auto;
	min-height: 0;
	border-left: 1px solid var(--border);
	background: var(--surface);
	color: var(--text);
}
.jv-suptp-head {
	flex: 0 0 auto;
	padding: 14px 20px;
	font-size: 15px;
	font-weight: 600;
	border-bottom: 1px solid var(--border);
}
.jv-suptp-sec {
	display: flex;
	flex-direction: column;
	gap: 14px;
	padding: 16px 20px;
}
.jv-suptp-sec-b {
	border-bottom: 1px solid var(--border);
}
.jv-suptp-scroll {
	flex: 1;
	min-height: 0;
	overflow-y: auto;
}
.jv-suptp-id {
	display: flex;
	align-items: center;
	gap: 10px;
	margin-bottom: 2px;
}
.jv-suptp-avatar {
	display: flex;
	align-items: center;
	flex: 0 0 auto;
}
.jv-suptp-name {
	font-size: 15px;
	font-weight: 550;
	min-width: 0;
	flex: 1;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-suptp-reply {
	flex: 0 0 auto;
}
.jv-suptp-row {
	display: flex;
	align-items: center;
	font-size: 13px;
}
.jv-suptp-label {
	flex: 0 0 118px;
	color: var(--text-3);
}
.jv-suptp-value {
	flex: 1;
	min-width: 0;
	color: var(--text);
	word-break: break-word;
}
.jv-suptp-empty {
	color: var(--text-3);
}
</style>
