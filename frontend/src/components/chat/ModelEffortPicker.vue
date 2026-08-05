<template>
	<div ref="rootRef" class="mep">
		<!-- trigger pill: sits in the composer's bottom-left toolbar -->
		<button
			ref="triggerRef"
			class="mep-pill"
			type="button"
			:aria-expanded="open"
			title="Model and effort"
			@click="open = !open"
		>
			<svg
				width="13"
				height="13"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="1.7"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<ellipse cx="12" cy="5" rx="9" ry="3" />
				<path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5" />
				<path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3" />
			</svg>
			<span class="mep-model">{{ pillModel }}</span>
			<!-- kept in layout (visibility, not v-if) so toggling thinkingOverride
			     does not shift the Enter hint / Send button beside this pill -->
			<span class="mep-dot" :class="{ 'mep-hide': !thinkingOverride }">·</span>
			<span class="mep-effort" :class="{ 'mep-hide': !thinkingOverride }">{{
				effortLabel
			}}</span>
			<svg
				class="mep-caret"
				width="12"
				height="12"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="1.9"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="m6 9 6 6 6-6" />
			</svg>
		</button>

		<!-- dropdown: opens UPWARD (the pill lives at the bottom of the screen) -->
		<div v-if="open" class="mep-menu" role="menu">
			<!-- models -->
			<template v-for="(g, gi) in modelsByProvider" :key="g.provider">
				<div v-if="showProviders" class="mep-head">{{ g.provider }}</div>
				<button
					v-if="gi === 0"
					class="mep-item"
					role="menuitemradio"
					:aria-checked="!modelOverride"
					@click="onModel('')"
				>
					<span class="mep-item-body">
						<span class="mep-name">Auto</span>
						<span class="mep-desc"
							>Let {{ assistantName }} choose · {{ defaultModel || "default" }}</span
						>
					</span>
					<CheckMark v-if="!modelOverride" />
				</button>
				<button
					v-for="r in g.models"
					:key="g.provider + '/' + r.model"
					class="mep-item"
					role="menuitemradio"
					:aria-checked="r.model === modelOverride"
					@click="onModel(r.model)"
				>
					<span class="mep-item-body">
						<span class="mep-name">{{ r.model }}</span>
						<span v-if="r.tier" class="mep-desc">{{ r.tier }}</span>
					</span>
					<CheckMark v-if="r.model === modelOverride" />
				</button>
			</template>

			<div class="mep-div" />

			<!-- effort → side flyout (wrapper keeps the flyout a SIBLING of the row
			     button, never nested inside it — interactive-in-button is invalid) -->
			<div class="mep-sub" @mouseenter="cancelEffortClose" @mouseleave="scheduleEffortClose">
				<!-- click OPENS, it does not toggle. For a mouse user the pointer order
				     is mouseenter (opens) then click, so a toggling click would close
				     what the hover just opened and the flyout could never open by
				     click. Both intents here are "open"; closing is mouseleave (the
				     .mep-sub delay), outside-click / Escape (useDismissable), or
				     picking a level. Keyboard users open with Enter and close with
				     Escape. -->
				<button
					class="mep-item"
					:class="{ open: effortOpen }"
					aria-haspopup="menu"
					:aria-expanded="effortOpen"
					@click="openEffort()"
					@mouseenter="openEffort()"
				>
					<span class="mep-item-body"><span class="mep-name">Effort</span></span>
					<span class="mep-val">{{ effortLabel }}</span>
					<svg
						width="13"
						height="13"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.9"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="m9 18 6-6-6-6" />
					</svg>
				</button>

				<div v-if="effortOpen" class="mep-flyout">
					<div class="mep-fly-head">
						Higher effort means more thorough responses, but takes longer and uses your
						limits faster.
					</div>
					<button
						class="mep-item"
						role="menuitemradio"
						:aria-checked="!thinkingOverride"
						@click="onEffort('')"
					>
						<span class="mep-item-body"><span class="mep-name">Auto</span></span>
						<span class="mep-tag">Default</span>
						<CheckMark v-if="!thinkingOverride" />
					</button>
					<button
						v-for="lvl in thinkingLevels"
						:key="lvl"
						class="mep-item"
						role="menuitemradio"
						:aria-checked="lvl === thinkingOverride"
						@click="onEffort(lvl)"
					>
						<span class="mep-item-body"
							><span class="mep-name mep-cap">{{ lvl }}</span></span
						>
						<CheckMark v-if="lvl === thinkingOverride" />
					</button>
				</div>
			</div>

			<!-- persona → side flyout (same pattern as Effort). Only shown when the
			     fleet-wide persona switch is on. Reuses the standalone picker's
			     design via <PersonaOptions>, which owns the store I/O. -->
			<div
				v-if="personaEnabled"
				class="mep-sub"
				@mouseenter="cancelPersonaClose"
				@mouseleave="schedulePersonaClose"
			>
				<button
					class="mep-item"
					:class="{ open: personaOpen }"
					aria-haspopup="menu"
					:aria-expanded="personaOpen"
					@click="openPersona()"
					@mouseenter="openPersona()"
				>
					<span class="mep-item-body"><span class="mep-name">Persona</span></span>
					<span class="mep-val">{{ personaLabel }}</span>
					<svg
						width="13"
						height="13"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.9"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="m9 18 6-6-6-6" />
					</svg>
				</button>

				<div v-if="personaOpen" class="mep-flyout mep-flyout-persona">
					<PersonaOptions @pick="close()" />
				</div>
			</div>
		</div>
	</div>
</template>

<script setup>
// Model + effort picker for the composer (Claude-web style): a pill in the
// input's bottom-left toolbar that opens UPWARD into a model list plus an
// "Effort" side-flyout. Presentational + self-managed open state only — the
// host (ChatView) owns the data and the persistence, passed via props and
// select-model / select-thinking emits (mirrors how <Composer> was extracted).
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { agentName } from "@/branding";
import { useDismissable } from "@/composables/useDismissable";
import { useShellStore } from "@/stores/shell";
import CheckMark from "@/components/chat/CheckMark.vue";
import PersonaOptions from "@/components/chat/PersonaOptions.vue";

const props = defineProps({
	modelOverride: { type: String, default: "" },
	defaultModel: { type: String, default: "" },
	thinkingOverride: { type: String, default: "" },
	thinkingLevels: { type: Array, default: () => ["low", "medium", "high"] },
	modelsByProvider: { type: Array, default: () => [] },
	showProviders: { type: Boolean, default: false },
	personaEnabled: { type: Boolean, default: false },
});
const emit = defineEmits(["select-model", "select-thinking"]);

const store = useShellStore();
const assistantName = agentName;
const open = ref(false);
const effortOpen = ref(false);
const personaOpen = ref(false);
const rootRef = ref(null);
const triggerRef = ref(null);

const pillModel = computed(() => props.modelOverride || props.defaultModel || "Auto");
const effortLabel = computed(() => {
	const t = props.thinkingOverride;
	return t ? t.charAt(0).toUpperCase() + t.slice(1) : "Auto";
});
// Persona lives in the shell store (PersonaOptions writes it); the row only
// needs its current label. The two flyouts are mutually exclusive.
const personaLabel = computed(() => store.preferredPersona);
function openEffort() {
	personaOpen.value = false;
	effortOpen.value = true;
}
function openPersona() {
	effortOpen.value = false;
	personaOpen.value = true;
}

function onModel(m) {
	emit("select-model", m);
	close();
	triggerRef.value?.focus();
}
function onEffort(level) {
	emit("select-thinking", level);
	close();
	triggerRef.value?.focus();
}
function close() {
	open.value = false;
	effortOpen.value = false;
	personaOpen.value = false;
	cancelEffortClose();
	cancelPersonaClose();
}

// The 8px gap between the "Effort" row and its flyout (.mep-flyout, positioned
// to the side) is empty space outside .mep-sub's own box, so crossing it fired
// mouseleave and closed the flyout before a deliberate hover-then-click could
// ever land. A short close delay, cancelled on re-entry (the flyout re-enters
// .mep-sub too, since it is nested inside it), gives the pointer time to
// travel across.
let effortCloseTimer = null;
function scheduleEffortClose() {
	cancelEffortClose();
	effortCloseTimer = setTimeout(() => {
		effortOpen.value = false;
		effortCloseTimer = null;
	}, 180);
}
function cancelEffortClose() {
	if (effortCloseTimer) {
		clearTimeout(effortCloseTimer);
		effortCloseTimer = null;
	}
}

// Same hover-close delay for the persona flyout (see the effort note above).
let personaCloseTimer = null;
function schedulePersonaClose() {
	cancelPersonaClose();
	personaCloseTimer = setTimeout(() => {
		personaOpen.value = false;
		personaCloseTimer = null;
	}, 180);
}
function cancelPersonaClose() {
	if (personaCloseTimer) {
		clearTimeout(personaCloseTimer);
		personaCloseTimer = null;
	}
}
onBeforeUnmount(() => {
	cancelEffortClose();
	cancelPersonaClose();
});

// Outside-click / Escape dismissal via the shared composable; `close()` also
// collapses the effort flyout. The extra watch covers the pill-toggle path
// (open flipped false without going through close()).
useDismissable(rootRef, open, close, triggerRef);
watch(open, (isOpen) => {
	if (!isOpen) {
		effortOpen.value = false;
		personaOpen.value = false;
		cancelEffortClose();
		cancelPersonaClose();
	}
});
</script>

<style scoped>
.mep {
	position: relative;
	display: inline-flex;
}

/* ---- trigger pill (matches the composer's neutral chrome) ---- */
.mep-pill {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	height: 30px;
	padding: 0 10px;
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 999px;
	cursor: pointer;
	color: var(--text-2);
	font-family: inherit;
	font-size: 12px;
	font-weight: 500;
	transition: background-color 0.12s, border-color 0.12s;
}
.mep-pill:hover {
	background: var(--surface-2);
}
.mep-pill:focus-visible {
	outline: 2px solid var(--text);
	outline-offset: 2px;
}
.mep-model {
	max-width: 22ch;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.mep-dot {
	color: var(--text-3);
}
.mep-effort {
	color: var(--text-3);
	font-weight: 450;
}
.mep-hide {
	visibility: hidden;
}
.mep-caret {
	color: var(--text-3);
	margin-left: 1px;
}

/* ---- dropdown, opening upward ---- */
.mep-menu {
	position: absolute;
	bottom: calc(100% + 8px);
	right: 0;
	min-width: 250px;
	max-width: 320px;
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 12px;
	box-shadow: 0 12px 34px rgba(20, 20, 30, 0.18);
	padding: 5px;
	z-index: 60;
}
.mep-head {
	padding: 6px 9px 4px;
	font-size: 10px;
	font-weight: 600;
	text-transform: uppercase;
	letter-spacing: 0.03em;
	color: var(--text-3);
}
.mep-item {
	display: flex;
	align-items: center;
	gap: 8px;
	width: 100%;
	padding: 8px 9px;
	border: none;
	border-radius: 8px;
	background: transparent;
	color: var(--text);
	font-family: inherit;
	font-size: 13px;
	text-align: left;
	cursor: pointer;
}
.mep-item:hover,
.mep-item.open {
	background: var(--surface-1);
}
.mep-item-body {
	display: flex;
	flex-direction: column;
	gap: 2px;
	flex: 1;
	min-width: 0;
}
.mep-name {
	font-weight: 500;
	color: var(--text);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.mep-cap {
	text-transform: capitalize;
}
.mep-desc {
	font-size: 11.5px;
	font-weight: 450;
	color: var(--text-3);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.mep-div {
	height: 1px;
	background: var(--border);
	margin: 5px 4px;
}

/* ---- effort submenu row + flyout ---- */
.mep-sub {
	position: relative;
}
.mep-val {
	font-size: 12px;
	color: var(--text-3);
}
.mep-flyout {
	position: absolute;
	right: calc(100% + 8px);
	bottom: -6px;
	width: 232px;
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 12px;
	box-shadow: 0 12px 34px rgba(20, 20, 30, 0.18);
	padding: 5px;
	cursor: default;
}
/* the persona rows (orb + name + description) need a touch more room */
.mep-flyout-persona {
	width: 250px;
}
.mep-fly-head {
	padding: 8px 9px 10px;
	font-size: 11.5px;
	line-height: 1.4;
	color: var(--text-3);
}
.mep-tag {
	font-size: 10px;
	font-weight: 600;
	color: var(--text-3);
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 5px;
	padding: 1px 5px;
}

@media (max-width: 560px) {
	/* no room for a side flyout on narrow screens — drop it below the row */
	.mep-flyout {
		left: 0;
		right: 0;
		bottom: auto;
		top: calc(100% + 4px);
		width: auto;
	}
}
</style>
