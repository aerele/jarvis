<template>
	<!-- Reviewer-only lane (review-before-landing): a File Box run's wiki note is
	     HELD as a proposal a Jarvis reviewer — NOT the dropper (separation of
	     duties) — approves before it lands. Self-gating: the reviewer-gated
	     endpoint 403s for non-reviewers (panel hidden) and the panel only shows
	     when there is something Pending. -->
	<div v-if="show" class="border-b bg-surface-gray-1">
		<button
			class="flex w-full items-center gap-2 px-5 py-2.5 text-left hover:bg-surface-gray-2"
			@click="collapsed = !collapsed"
		>
			<FeatherIcon
				:name="collapsed ? 'chevron-right' : 'chevron-down'"
				class="size-4 shrink-0 text-ink-gray-5"
			/>
			<span class="text-sm font-medium text-ink-gray-8">Wiki write proposals</span>
			<Badge variant="subtle" theme="orange" :label="String(rows.length)" />
			<span class="text-xs text-ink-gray-5">awaiting your review</span>
		</button>

		<div v-if="!collapsed" class="flex flex-col divide-y border-t">
			<div v-if="error" class="px-5 py-3 text-sm text-ink-red-5">{{ error }}</div>
			<div v-for="p in rows" :key="p.name" class="px-5 py-3">
				<div class="flex items-start justify-between gap-3">
					<div class="min-w-0 flex-1">
						<div class="truncate text-base font-medium text-ink-gray-9">
							{{ p.title || headline(p) }}
						</div>
						<div class="mt-0.5 truncate text-xs text-ink-gray-5">
							{{ headline(p) }} · dropped by {{ dropper(p) }}
						</div>
						<pre
							v-if="excerpt(p)"
							class="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded border bg-surface-white p-2 text-sm text-ink-gray-7"
							>{{ excerpt(p) }}</pre
						>
						<div v-if="!p.can_approve" class="mt-1.5 text-xs text-ink-gray-5">
							You dropped this file — a different reviewer must approve it.
						</div>
					</div>
					<div class="flex shrink-0 items-center gap-2">
						<Button
							variant="solid"
							theme="green"
							label="Approve"
							:loading="busy === p.name"
							:disabled="!p.can_approve || busy !== null"
							@click="approve(p)"
						/>
						<Button
							variant="subtle"
							theme="red"
							label="Reject"
							:loading="busy === p.name"
							:disabled="busy !== null"
							@click="reject(p)"
						/>
					</div>
				</div>
			</div>
		</div>
	</div>
</template>

<script setup>
// Self-contained reviewer affordance mounted at the top of the Approval Board.
// All governance is enforced server-side (approvals_api); this is the surface
// through which a reviewer reads the proposed page and approves/rejects it.
import { ref, computed, onMounted } from "vue";
import { Badge, Button, FeatherIcon, toast } from "frappe-ui";
import * as api from "@/api";
import { errMessage as errMsg } from "@/lib/errors";
import {
	isPermissionDenied,
	proposalHeadline,
	proposalExcerpt,
	dropperLabel,
} from "@/lib/wikiReview";

const rows = ref([]);
const allowed = ref(true); // flipped off when the endpoint 403s (not a reviewer)
const error = ref("");
const busy = ref(null); // name of the row being acted on (one at a time)
const collapsed = ref(false);

const show = computed(() => allowed.value && rows.value.length > 0);

const headline = proposalHeadline;
const excerpt = proposalExcerpt;
const dropper = dropperLabel;

async function load() {
	try {
		const res = (await api.listWikiWriteProposals({ status: "Pending" })) || {};
		rows.value = Array.isArray(res.rows) ? res.rows : [];
		error.value = "";
	} catch (e) {
		// A 403 means the caller is not a reviewer — hide the panel entirely
		// rather than surfacing a permission error to an ordinary user.
		if (isPermissionDenied(e)) {
			allowed.value = false;
			rows.value = [];
		} else {
			error.value = errMsg(e);
		}
	}
}

async function approve(p) {
	if (busy.value) return;
	busy.value = p.name;
	try {
		const r = await api.approveWikiWrite(p.name);
		if (r && r.applied) toast.success("Wiki note approved and recorded");
		else toast.warning("Approved, but the write did not land — retry it from the desk");
		await load();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		busy.value = null;
	}
}

async function reject(p) {
	if (busy.value) return;
	busy.value = p.name;
	try {
		await api.rejectWikiWrite(p.name);
		toast.success("Wiki note rejected — nothing was written");
		await load();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		busy.value = null;
	}
}

onMounted(load);
defineExpose({ load });
</script>
