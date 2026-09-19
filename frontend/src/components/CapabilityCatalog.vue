<script setup>
// "What can I ask Jarvis?" - a collapsed catalog on the empty-chat welcome
// surface that lists what Jarvis can do, each entry carrying a safety badge
// derived server-side from the enforcing gate (see jarvis.api._gating_badge).
// Tapping a card FILLS the composer (via the parent's @select), never sends.
import { computed, ref } from "vue";
import { Badge } from "frappe-ui";
import { getCapabilityCatalog, logCapabilityPick } from "@/api";
import { BADGE_META, CATALOG_LEGEND } from "@/lib/capabilityBadges";

const emit = defineEmits(["select"]);

const open = ref(false);
const state = ref("idle"); // idle | loading | ready | empty | error
const cards = ref([]);

// Group the flat card list by its "group" field, first-seen order preserved.
const groups = computed(() => {
	const out = [];
	const byName = new Map();
	for (const c of cards.value) {
		let g = byName.get(c.group);
		if (!g) {
			g = { name: c.group, cards: [] };
			byName.set(c.group, g);
			out.push(g);
		}
		g.cards.push(c);
	}
	return out;
});

async function load() {
	state.value = "loading";
	try {
		const r = await getCapabilityCatalog();
		const list = (r && r.data && r.data.cards) || [];
		cards.value = Array.isArray(list) ? list : [];
		state.value = cards.value.length ? "ready" : "empty";
	} catch {
		// Never a silent blank: fall to an explicit, retryable error state.
		state.value = "error";
	}
}

function toggle() {
	open.value = !open.value;
	// Lazy: fetch only on first open (and on retry from the error state).
	if (open.value && (state.value === "idle" || state.value === "error")) load();
}

function pick(card) {
	emit("select", { title: card.title, prompt: card.prompt }); // fills composer; never sends
	logCapabilityPick(card.title).catch(() => {}); // telemetry is best-effort
}

const meta = (tier) => BADGE_META[tier] || { theme: "gray", label: tier };
</script>

<template>
	<div class="jv-catalog">
		<button type="button" class="jv-catalog-toggle" :aria-expanded="open" @click="toggle">
			What can I ask Jarvis?
			<span class="jv-catalog-chev" :class="{ 'is-open': open }" aria-hidden="true">›</span>
		</button>

		<div v-if="open" class="jv-catalog-body">
			<p v-if="state === 'loading'" class="jv-catalog-note">Loading…</p>

			<div v-else-if="state === 'error'" class="jv-catalog-error">
				<span>Couldn't load what Jarvis can do.</span>
				<button type="button" class="jv-catalog-retry" @click="load">Try again</button>
			</div>

			<p v-else-if="state === 'empty'" class="jv-catalog-empty">Nothing to show here yet.</p>

			<template v-else-if="state === 'ready'">
				<p class="jv-catalog-legend">{{ CATALOG_LEGEND }}</p>
				<div v-for="g in groups" :key="g.name" class="jv-catalog-group">
					<h4 class="jv-catalog-group-h">{{ g.name }}</h4>
					<button
						v-for="c in g.cards"
						:key="c.title"
						type="button"
						class="jv-cap-card"
						@click="pick(c)"
					>
						<span class="jv-cap-main">
							<span class="jv-cap-title">{{ c.title }}</span>
							<span class="jv-cap-prompt">{{ c.prompt }}</span>
						</span>
						<Badge
							:theme="meta(c.badge).theme"
							:label="meta(c.badge).label"
							variant="subtle"
							size="sm"
						/>
					</button>
				</div>
				<p class="jv-catalog-hint">Tap one to start — you can edit before sending.</p>
			</template>
		</div>
	</div>
</template>

<style scoped>
.jv-catalog {
	margin-top: 18px;
	width: 100%;
}
.jv-catalog-toggle {
	display: flex;
	align-items: center;
	gap: 6px;
	background: none;
	border: none;
	padding: 6px 2px;
	font: inherit;
	font-weight: 600;
	color: inherit;
	cursor: pointer;
}
.jv-catalog-chev {
	display: inline-block;
	transition: transform 0.15s;
}
.jv-catalog-chev.is-open {
	transform: rotate(90deg);
}
.jv-catalog-body {
	margin-top: 8px;
}
.jv-catalog-legend {
	margin: 0 0 12px;
	font-size: 12px;
	line-height: 1.4;
	opacity: 0.7;
}
.jv-catalog-group {
	margin-bottom: 14px;
}
.jv-catalog-group-h {
	margin: 0 0 6px;
	font-size: 11px;
	font-weight: 700;
	text-transform: uppercase;
	letter-spacing: 0.04em;
	opacity: 0.6;
}
.jv-cap-card {
	display: flex;
	align-items: center;
	gap: 10px;
	width: 100%;
	margin-bottom: 7px;
	padding: 11px 13px;
	text-align: left;
	background: var(--surface);
	border: 1px solid var(--border);
	border-radius: 10px;
	font: inherit;
	color: inherit;
	cursor: pointer;
}
.jv-cap-main {
	display: flex;
	flex: 1;
	min-width: 0;
	flex-direction: column;
	gap: 2px;
}
.jv-cap-title {
	font-size: 13.5px;
	font-weight: 600;
}
.jv-cap-prompt {
	font-size: 12.5px;
	opacity: 0.65;
}
.jv-catalog-error {
	display: flex;
	align-items: center;
	gap: 10px;
	font-size: 13px;
}
.jv-catalog-retry {
	padding: 4px 10px;
	background: none;
	border: 1px solid var(--border);
	border-radius: 8px;
	font: inherit;
	color: inherit;
	cursor: pointer;
}
.jv-catalog-empty,
.jv-catalog-note {
	font-size: 13px;
	opacity: 0.7;
}
.jv-catalog-hint {
	margin: 4px 0 0;
	font-size: 11px;
	opacity: 0.55;
}
</style>
