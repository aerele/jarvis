<template>
	<div class="flex h-full min-h-0 flex-col">
		<LayoutHeader>
			<template #left-header
				><Breadcrumbs
					:items="[
						{ label: 'Agents', route: { name: 'AgentsList' } },
						{ label: 'Reorder reviews' },
					]"
			/></template>
			<template #right-header
				><Button label="Refresh" :loading="loading" :disabled="!!saving" @click="load()"
			/></template>
		</LayoutHeader>
		<main class="min-h-0 flex-1 overflow-auto p-5">
			<h1 class="text-xl font-semibold">Reorder reviews</h1>
			<p class="mt-2 text-sm text-ink-gray-6">
				Check the suggested quantity in stock units. Approval records your review only.
				Create any Material Request through ERPNext. Check current stock and open requests
				before you create one.
			</p>
			<p v-if="error" role="alert" class="mt-4 text-sm text-ink-red-4">{{ error }}</p>
			<p v-if="loading && !runs.length" role="status" class="mt-5">Loading reviews…</p>
			<p v-else-if="!runs.length && !error" class="mt-5">
				No Reorder reviews are available on this page.
			</p>
			<section v-for="run in runs" :key="run.run" class="mt-5 rounded border p-4">
				<h2 class="break-words font-semibold">
					{{ run.company || run.run }}
					<span class="ml-2 text-sm font-normal text-ink-gray-6">{{
						run.mode === "shadow" ? "Preview" : "Buyer review"
					}}</span>
				</h2>
				<p v-if="run.as_of" class="mt-1 text-sm text-ink-gray-6">As of {{ run.as_of }}</p>
				<p v-if="run.current === false" class="mt-2 text-sm">
					This review is from an earlier day. Start a new review before you approve a
					suggestion.
				</p>
				<p class="mt-2 break-words text-sm text-ink-gray-6">
					Executed as {{ run.execution_user }} · Reviewer {{ run.reviewer }}
				</p>
				<p class="mt-1 text-sm text-ink-gray-5">Run {{ run.run }}</p>
				<p v-if="!run.rows.length" class="mt-4 text-sm">
					No saved policy produced a suggestion.
				</p>
				<div v-for="row in run.rows" :key="row.id" class="mt-4 border-t pt-4">
					<div class="flex flex-wrap justify-between gap-2">
						<strong class="break-words">{{ row.item }}</strong
						><span>{{ statusText(row) }}</span>
					</div>
					<p class="mt-1 break-words text-sm text-ink-gray-6">{{ row.warehouse }}</p>
					<dl class="mt-3 grid grid-cols-1 gap-2 text-sm sm:grid-cols-3">
						<div>
							<dt class="text-ink-gray-5">Captured projected stock</dt>
							<dd>
								{{
									row.projected_qty === null
										? "Manual scope check"
										: row.projected_qty + " " + row.stock_uom
								}}
							</dd>
						</div>
						<div>
							<dt class="text-ink-gray-5">Saved reorder level</dt>
							<dd>{{ row.reorder_level }} {{ row.stock_uom }}</dd>
						</div>
						<div>
							<dt class="text-ink-gray-5">Suggested purchase</dt>
							<dd>
								{{
									row.proposed_qty === null
										? "Manual action needed"
										: row.proposed_qty + " " + row.stock_uom
								}}
							</dd>
						</div>
					</dl>
					<p v-if="row.reason" class="mt-3 text-sm">{{ reasonText(row.reason) }}</p>
					<div
						v-if="run.mode === 'live' && row.status === 'Pending'"
						class="mt-4 flex flex-wrap gap-2"
					>
						<Button
							label="Approve suggestion"
							v-if="run.current !== false"
							variant="solid"
							:disabled="!!saving"
							:loading="saving === run.run + row.id"
							@click="decide(run, row, 1)"
						/>
						<Button
							label="Reject suggestion"
							:disabled="!!saving"
							@click="decide(run, row, 0)"
						/>
					</div>
				</div>
			</section>
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
const endpoint = "jarvis.chat.reorder_review.";
const runs = ref([]),
	loading = ref(false),
	saving = ref(""),
	error = ref(""),
	start = ref(0),
	hasMore = ref(false);
function statusText(row) {
	return (
		{
			Pending: "Awaiting your review",
			Approved: "Approved suggestion",
			Rejected: "Rejected",
			Held: "Manual action needed",
		}[row.status] || "Review unavailable"
	);
}
function reasonText(reason) {
	return (
		{
			draft_supply_exists:
				"A draft request or order already exists. Check that draft before you request more stock.",
			conflicting_policy:
				"More than one policy exists for this item and warehouse. Check the saved policies.",
			group_or_variant_policy: "This grouped or variant policy needs a manual stock check.",
			non_purchase_policy:
				"This policy is not a purchase policy. Use its normal ERPNext flow.",
		}[reason] || "Check the saved policy before you create a request."
	);
}
async function load(append = false) {
	if (loading.value || saving.value) return;
	loading.value = true;
	error.value = "";
	try {
		const result = await call(endpoint + "list_reviews", { start: append ? start.value : 0 });
		runs.value = append ? [...runs.value, ...result.rows] : result.rows;
		start.value = result.next_start;
		hasMore.value = result.has_more;
	} catch {
		error.value = "Could not load reviews. Select Refresh to try again.";
	} finally {
		loading.value = false;
	}
}
async function decide(run, row, approve) {
	if (saving.value || loading.value) return;
	saving.value = run.run + row.id;
	error.value = "";
	try {
		const result = await call(endpoint + "decide", { name: run.run, row_id: row.id, approve });
		if (result.ok) row.status = result.status;
		else error.value = "The review was blocked. Refresh and check the source.";
	} catch {
		error.value =
			"Could not confirm the recorded result. Refresh before you retry. Changed evidence needs a new review.";
	} finally {
		saving.value = "";
	}
}
onMounted(() => load());
</script>
