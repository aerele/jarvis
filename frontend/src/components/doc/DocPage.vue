<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs :items="breadcrumbs" />
			</template>
			<template #right-header>
				<slot name="actions" />
			</template>
		</LayoutHeader>

		<!-- error state (bad id / permission) -->
		<div v-if="error" class="flex flex-1 flex-col items-center justify-center gap-1 px-5">
			<div class="text-lg font-medium text-ink-gray-8">Document not found</div>
			<div class="text-center text-p-base text-ink-gray-6">{{ error }}</div>
			<Button
				v-if="listRoute"
				class="mt-3"
				label="Back to list"
				@click="router.push(listRoute)"
			/>
		</div>

		<!-- body: scrolling main column + resizable right panel -->
		<div v-else-if="!loading" class="flex flex-1 overflow-hidden">
			<div class="flex flex-1 flex-col overflow-hidden">
				<div class="flex-1 overflow-y-auto">
					<div class="mx-auto w-full max-w-3xl px-5 py-6">
						<div class="flex items-center gap-3">
							<h1 class="min-w-0 truncate text-2xl font-semibold text-ink-gray-9">
								{{ title }}
							</h1>
							<Badge
								v-if="statusBadge"
								variant="subtle"
								:theme="statusBadge.theme || 'gray'"
								:label="statusBadge.label"
							/>
							<Badge
								v-if="dirty"
								variant="subtle"
								theme="orange"
								label="Not Saved"
							/>
						</div>
						<div class="mt-4">
							<slot name="main" />
						</div>
					</div>
					<!-- comments live at the BOTTOM of the scroll (Desk-style), not a tab -->
					<div v-if="$slots.footer" class="border-t">
						<div class="mx-auto w-full max-w-3xl px-5 py-6">
							<slot name="footer" />
						</div>
					</div>
				</div>
			</div>
			<Resizer
				v-if="$slots.aside"
				side="right"
				class="flex flex-col overflow-y-auto border-l"
			>
				<slot name="aside" />
			</Resizer>
		</div>

		<!-- loading: nothing below the header (list-kit convention, R1 §8) -->
		<div v-else class="flex-1" />
	</div>
</template>

<script setup>
// DocPage - the detail-page frame every doc page uses (DESIGN-V3 §6.1):
// LayoutHeader (breadcrumbs | #actions) → title row (+ status / "Not Saved"
// badges) → #main sections → border-t → #footer comments, with the #aside
// panel in a CRM-style Resizer (352px default, 256-480).
import { computed } from "vue";
import { useRouter } from "vue-router";
import { Breadcrumbs, Badge, Button } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import Resizer from "@/components/doc/Resizer.vue";

const props = defineProps({
	breadcrumbs: { type: Array, default: () => [] }, // [{label, route?}]
	title: { type: String, default: "" },
	statusBadge: { type: Object, default: null }, // {label, theme}
	dirty: { type: Boolean, default: false },
	loading: { type: Boolean, default: false },
	error: { type: String, default: null },
});

const router = useRouter();

// "Back to list" returns through the breadcrumb root (D32)
const listRoute = computed(() => {
	const root = props.breadcrumbs && props.breadcrumbs[0];
	return (root && root.route) || null;
});
</script>
