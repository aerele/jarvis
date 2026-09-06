<template>
	<div class="flex flex-col gap-4">
		<FormControl
			type="text"
			placeholder="Search apps"
			:modelValue="query"
			@update:modelValue="(v) => (query = v)"
		/>

		<!-- Option-chip idiom (TriggerDetail.vue's ACTION_TYPES row): plain Buttons
		     toggling solid/subtle, not TabButtons - a segmented control reads wrong
		     once there are this many options, and this app has no pill-chip
		     component of its own to reach for instead. -->
		<div v-if="categories.length" class="flex flex-wrap gap-2">
			<Button
				v-for="c in categories"
				:key="c.value"
				:label="c.label"
				size="sm"
				:variant="activeCategory === c.value ? 'solid' : 'subtle'"
				@click="activeCategory = c.value"
			/>
		</div>

		<p v-if="!filtered.length" class="py-8 text-center text-p-sm text-ink-gray-5">
			No apps match.
		</p>
		<div v-else class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
			<div
				v-for="entry in filtered"
				:key="entry.name"
				class="flex flex-col gap-2 rounded-lg border p-3 transition-colors hover:border-outline-gray-2 hover:shadow-sm"
			>
				<div class="flex items-center gap-2">
					<ConnectorLogo
						:preset="entry.name"
						:size="24"
						class="shrink-0 text-ink-gray-5"
					/>
					<span class="truncate text-sm font-medium text-ink-gray-9">{{
						entry.name
					}}</span>
				</div>
				<p class="line-clamp-2 min-h-8 text-xs text-ink-gray-5">{{ entry.description }}</p>
				<div class="mt-1 flex items-center justify-between gap-2">
					<Badge
						variant="subtle"
						size="sm"
						:theme="authBadge(entry).theme"
						:label="authBadge(entry).label"
					/>
					<span
						v-if="isAdded(entry)"
						class="inline-flex items-center gap-1 text-xs text-ink-green-3"
					>
						<FeatherIcon name="check" class="size-3.5" />
						Added
					</span>
					<Button
						v-else
						variant="subtle"
						size="sm"
						iconLeft="plus"
						label="Add"
						@click="emit('add', entry.name)"
					/>
				</div>
			</div>
		</div>

		<div v-if="allowCustomUrls" class="flex items-center justify-between gap-3 border-t pt-3">
			<span class="text-xs text-ink-gray-5">Have a server we do not list?</span>
			<Button
				variant="ghost"
				size="sm"
				iconLeft="link"
				label="Add custom URL"
				@click="emit('add-custom')"
			/>
		</div>
	</div>
</template>

<script setup>
// Browse tab (Option B, Directory.dc.html) - a searchable, categorized catalog
// grid, one card per preset, replacing the old "pick from a Select" step
// inside AddConnectorDialog. Pressing a card's Add (or the footer's Add
// custom URL) doesn't connect anything itself - it just tells ConnectorsPane
// which preset to open AddConnectorDialog against; every actual connect
// action still lives in that dialog.
import { computed, ref } from "vue";
import { Badge, Button, FeatherIcon, FormControl } from "frappe-ui";
import ConnectorLogo from "@/components/settings/ConnectorLogo.vue";

const props = defineProps({
	// listConnectors()'s catalog: [{ name, key, auth, category, description,
	// logo, help_url, hint, token_hint, token_help_url }], enabled providers
	// in catalog order.
	catalog: { type: Array, default: () => [] },
	// Every row visible to the current viewer (Shared + their own Mine, exactly
	// what ConnectorsPane's Installed tab renders) - drives the "Added" badge
	// below without a second round-trip. isAdded further filters this to rows
	// the viewer can actually USE (F9): "mine" is by definition the viewer's
	// own Personal rows, but a Shared row an admin hasn't finished setting up
	// (needs_static_client) doesn't count for a plain user, who would
	// otherwise see "Added" for something they can't sign in to yet.
	installedRows: { type: Array, default: () => [] },
	allowCustomUrls: { type: Boolean, default: true },
});
const emit = defineEmits(["add", "add-custom"]);

const query = ref("");
const activeCategory = ref("all");

// Fixed display order (catalog.py's own category grouping order), label text
// per the design's copy. A category with no enabled catalog entry never shows
// up as a chip - "Web" earns a chip only once something in it ships.
const CATEGORY_LABELS = {
	payments: "Payments",
	work: "Work",
	files: "Files",
	design: "Design",
	support: "Support",
	data: "Data",
	web: "Web",
	automation: "Automation",
	docs: "Docs",
	dev: "Developer",
};

const categories = computed(() => {
	const present = new Set(props.catalog.map((e) => e.category));
	const chips = Object.keys(CATEGORY_LABELS)
		.filter((key) => present.has(key))
		.map((key) => ({ value: key, label: CATEGORY_LABELS[key] }));
	return chips.length ? [{ value: "all", label: "All" }, ...chips] : [];
});

const filtered = computed(() => {
	const q = query.value.trim().toLowerCase();
	return props.catalog.filter((entry) => {
		if (activeCategory.value !== "all" && entry.category !== activeCategory.value)
			return false;
		if (!q) return true;
		return (
			entry.name.toLowerCase().includes(q) ||
			(entry.description || "").toLowerCase().includes(q)
		);
	});
});

// F9: a Personal row is always the viewer's own, so it always counts; a
// Shared row only counts once it's past needs_static_client - a plain user
// browsing sees Add (not a false "Added") for an admin's half-set-up row and
// can still add their own.
function isAdded(entry) {
	return props.installedRows.some(
		(row) =>
			row.preset === entry.name && (row.scope === "Personal" || !row.needs_static_client)
	);
}

// Matches the design's four-bucket AUTH_LABEL map exactly (Directory.dc.html):
// dcr signs in directly, static needs the viewer's own registered app first
// (called out in theme orange, same as a Setup-needed row), token pastes a
// key, open needs nothing at all.
const AUTH_BADGE = {
	dcr: { label: "Sign in", theme: "gray" },
	static: { label: "Your app", theme: "orange" },
	token: { label: "Key", theme: "gray" },
	open: { label: "No sign-in", theme: "gray" },
};
function authBadge(entry) {
	return AUTH_BADGE[entry.auth] || AUTH_BADGE.token;
}
</script>
