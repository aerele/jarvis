<template>
	<!-- Reviewer-only lane (review-before-landing): a File Box run's wiki note is
	     HELD as a proposal a Jarvis reviewer — NOT the dropper (separation of
	     duties) — approves before it lands. Self-gating: the reviewer-gated
	     endpoint 403s for non-reviewers (panel hidden) and the panel only shows
	     when there is something actionable (a pending decision or a failed
	     landing to retry). -->
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
						<div
							v-if="p.preview && p.preview.summary"
							class="mt-1.5 text-sm text-ink-gray-6"
						>
							{{ p.preview.summary }}
						</div>
						<!-- FULL proposed body (never truncated): the reviewer must read
						     exactly what will land on the shared org wiki before approving. -->
						<pre
							v-if="body(p)"
							class="mt-2 max-h-96 overflow-auto whitespace-pre-wrap rounded border bg-surface-white p-2 text-sm text-ink-gray-7"
							>{{ body(p) }}</pre
						>
						<div v-else class="mt-2 text-xs italic text-ink-gray-5">
							(no body — a metadata-only page refresh)
						</div>
						<div v-if="long(p)" class="mt-1 text-2xs text-ink-amber-6">
							Long note — scroll the box above to review the full text before
							approving.
						</div>
						<div v-if="p.needs_retry" class="mt-1.5 text-xs text-ink-red-5">
							Approved, but the write did not land{{
								p.apply_reason ? ` (${p.apply_reason})` : ""
							}}. Retry to re-drive it.
						</div>
						<div v-else-if="!p.can_approve" class="mt-1.5 text-xs text-ink-gray-5">
							You dropped this file — a different reviewer must approve it.
						</div>
					</div>
					<div class="flex shrink-0 items-center gap-2">
						<Button
							v-if="p.needs_retry"
							variant="solid"
							theme="gray"
							label="Retry"
							:loading="busy === p.name"
							:disabled="busy !== null"
							@click="retry(p)"
						/>
						<template v-else>
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
						</template>
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
	proposalBody,
	isLongBody,
	dropperLabel,
} from "@/lib/wikiReview";

const rows = ref([]);
const allowed = ref(true); // flipped off when the endpoint 403s (not a reviewer)
const error = ref("");
const busy = ref(null); // name of the row being acted on (one at a time)
const collapsed = ref(false);

const show = computed(() => allowed.value && rows.value.length > 0);

const headline = proposalHeadline;
const body = proposalBody;
const long = isLongBody;
const dropper = dropperLabel;

async function load() {
	try {
		// Default filter = Actionable: rows awaiting a decision (Approve/Reject)
		// AND approved-but-unlanded rows that need a Retry.
		const res = (await api.listWikiWriteProposals()) || {};
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
		else toast.warning("Approved, but the write did not land — use Retry below");
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

async function retry(p) {
	if (busy.value) return;
	busy.value = p.name;
	try {
		const r = await api.retryWikiWrite(p.name);
		if (r && r.applied) toast.success("Wiki note recorded");
		else toast.warning("Still did not land — check the failure reason");
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
