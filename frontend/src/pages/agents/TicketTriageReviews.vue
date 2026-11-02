<template>
	<div class="flex h-full min-h-0 flex-col">
		<LayoutHeader>
			<template #left-header
				><Breadcrumbs
					:items="[
						{ label: 'Agents', route: { name: 'AgentsList' } },
						{ label: 'Ticket Triage reviews' },
					]"
			/></template>
			<template #right-header
				><Button label="Refresh" :loading="loading" :disabled="!!saving" @click="load()"
			/></template>
		</LayoutHeader>
		<main class="min-h-0 flex-1 overflow-auto p-5">
			<h1 class="text-xl font-semibold">Ticket Triage reviews</h1>
			<p class="mt-2 text-sm text-ink-gray-6">
				Confirm one type or priority update at a time. Open the ticket to check its
				contents. Jarvis checks the source again before each change.
			</p>
			<p v-if="error" role="alert" class="mt-4 text-sm text-ink-red-4">{{ error }}</p>
			<p v-if="loading && !rows.length" role="status" class="mt-6">Loading reviews…</p>
			<p v-else-if="!rows.length && !error" class="mt-6">
				No Ticket Triage proposals are available to you.
			</p>
			<div v-for="row in rows" :key="row.name" class="mt-4 rounded border p-4">
				<div class="flex flex-wrap items-center justify-between gap-3">
					<a
						:href="'/helpdesk/tickets/' + encodeURIComponent(row.ticket)"
						target="_blank"
						rel="noopener noreferrer"
						class="font-medium underline"
						>Ticket {{ row.ticket }}</a
					>
					<span class="text-sm">{{ statusText(row) }}</span>
				</div>
				<p class="mt-2 break-words text-sm text-ink-gray-6">
					Executed as {{ row.execution_user }} · Reviewer {{ row.reviewer }}
				</p>
				<dl class="mt-3 grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
					<div>
						<dt class="text-ink-gray-5">Type</dt>
						<dd>
							{{ changeText(row.current.ticket_type, row.proposed.ticket_type) }}
						</dd>
					</div>
					<div>
						<dt class="text-ink-gray-5">Priority</dt>
						<dd>
							{{ changeText(row.current.priority, row.proposed.priority) }}
						</dd>
					</div>
				</dl>
				<div v-if="row.status === 'Pending'" class="mt-4 flex gap-2">
					<Button
						label="Confirm change"
						variant="solid"
						:disabled="!!saving || !row.can_confirm"
						:loading="saving === row.name"
						@click="decide(row, true)"
					/>
					<Button
						label="Reject proposal"
						:disabled="!!saving"
						@click="decide(row, false)"
					/>
				</div>
			</div>
			<Button
				v-if="hasMore"
				class="mt-4"
				label="Load more"
				:loading="loading"
				:disabled="!!saving"
				@click="load(true)"
			/>
		</main>
	</div>
</template>

<script setup>
import { onMounted, ref } from "vue";
import { Breadcrumbs, Button, call } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
const endpoint = "jarvis.chat.ticket_triage.";
const rows = ref([]);
const loading = ref(false);
const saving = ref("");
const error = ref("");
const start = ref(0);
const hasMore = ref(false);
function statusText(row) {
	if (row.apply_status === "Applied") return "Change applied";
	if (row.status === "Rejected") return "Rejected";
	if (row.apply_status === "Failed") return "Blocked — start a new review";
	return "Awaiting your review";
}
function changeText(current, proposed) {
	return proposed && proposed !== current
		? `${current || "Unset"} → ${proposed}`
		: `${current || "Unset"} (no change)`;
}
async function load(append = false) {
	if (loading.value || saving.value) return;
	loading.value = true;
	error.value = "";
	try {
		const data = await call(endpoint + "list_ticket_triage_proposals", {
			start: append ? start.value : 0,
		});
		rows.value = append ? [...rows.value, ...data.rows] : data.rows;
		start.value = data.next_start;
		hasMore.value = data.has_more;
	} catch {
		error.value = "Could not load reviews. Select Refresh to try again.";
	} finally {
		loading.value = false;
	}
}
async function decide(row, approve) {
	if (saving.value || loading.value) return;
	saving.value = row.name;
	error.value = "";
	try {
		const result = await call(
			endpoint +
				(approve ? "confirm_ticket_triage_proposal" : "reject_ticket_triage_proposal"),
			{ name: row.name }
		);
		if (!result.ok)
			error.value = "The change was blocked. Open the ticket and start a new review.";
		else row.status = result.status;
		if (result.ok && approve) row.apply_status = "Applied";
	} catch {
		error.value =
			"Could not confirm the recorded result. Refresh before you retry. A changed source needs a new review.";
	} finally {
		saving.value = "";
	}
}
onMounted(() => load());
</script>
