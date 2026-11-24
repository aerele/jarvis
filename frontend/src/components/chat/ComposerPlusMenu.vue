<!--
  The composer's "+" menu, after the Claude / ChatGPT one: the one place to add to the next
  message. Add files or photos (the host's own file picker), Use a skill (your own and
  shared skills, searchable), Connectors (focus the chat on one connected app) and Ground
  in wiki, a check item like Claude's memory toggle (one-shot: the next answer only).
  Whatever is on shows as a pill beside the button (SkillPill; × clears it) - the PWA,
  whose composer row is too narrow, turns these off and shows them above its input.

  Purely presentational: the host passes the lists and current picks, and acts on the
  events. A chosen skill rides on the next message as its "/skill_name" token, the token
  the server already resolves and checks (jarvis.chat.custom_skills.invoked_skill_slugs).
  The panel lines up with the left edge of the composer it sits in (.jv-composer) and
  opens above it. Shared by the desktop chat and the PWA (through @shared); colours fall
  back from the desktop palette to the PWA's.
-->
<template>
	<div ref="root" class="jv-plus">
		<button
			ref="trigger"
			type="button"
			class="jv-iconbtn jv-plus-btn"
			:class="{ on: open }"
			title="Add files, skills and more"
			aria-label="Add files, skills and more"
			aria-haspopup="menu"
			:aria-expanded="String(open)"
			:disabled="disabled"
			@click="toggle"
		>
			<svg
				width="18"
				height="18"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="1.8"
				stroke-linecap="round"
				aria-hidden="true"
			>
				<path d="M12 5v14M5 12h14" />
			</svg>
		</button>
		<template v-if="showPill">
			<SkillPill
				v-if="selectedSkill"
				:name="selectedSkill"
				:disabled="disabled"
				@clear="emit('skill', null)"
			/>
			<SkillPill
				v-if="wikiOn"
				kind="wiki"
				name="Wiki"
				:disabled="disabled"
				@clear="emit('wiki', false)"
			/>
			<SkillPill
				v-if="focus"
				kind="connector"
				:name="focus.label"
				:disabled="disabled"
				@clear="emit('connector', null)"
			/>
		</template>

		<div
			v-if="open"
			class="jv-plus-panel"
			role="menu"
			:aria-label="panelLabel"
			:style="panelStyle"
		>
			<!-- main list -->
			<template v-if="view === 'main'">
				<button
					type="button"
					role="menuitem"
					class="jv-plus-item"
					@click="choose('attach')"
				>
					<svg v-bind="iconAttrs" aria-hidden="true">
						<path
							d="M21.44 11.05l-9.19 9.19a5 5 0 0 1-7.07-7.07l9.19-9.19a3.5 3.5 0 0 1 4.95 4.95l-9.2 9.19a1.5 1.5 0 0 1-2.12-2.12l8.49-8.49"
						/>
					</svg>
					<span class="jv-plus-label">Add files or photos</span>
				</button>
				<button type="button" role="menuitem" class="jv-plus-item" @click="go('skills')">
					<svg v-bind="iconAttrs" aria-hidden="true">
						<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z" />
					</svg>
					<span class="jv-plus-label">Use a skill</span>
					<span v-if="selectedSkill" class="jv-plus-hint">{{ selectedSkill }}</span>
					<svg v-bind="iconAttrs" class="jv-plus-chev" aria-hidden="true">
						<path d="m9 18 6-6-6-6" />
					</svg>
				</button>
				<button
					v-if="showConnectors"
					type="button"
					role="menuitem"
					class="jv-plus-item"
					@click="go('connectors')"
				>
					<svg v-bind="iconAttrs" aria-hidden="true">
						<path
							d="M12 22v-5M9 8V2M15 8V2M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"
						/>
					</svg>
					<span class="jv-plus-label">Connectors</span>
					<span v-if="focus" class="jv-plus-hint">{{ focus.label }}</span>
					<svg v-bind="iconAttrs" class="jv-plus-chev" aria-hidden="true">
						<path d="m9 18 6-6-6-6" />
					</svg>
				</button>
				<template v-if="wikiAvailable || wikiEmpty">
					<div class="jv-plus-sep" role="separator" />
					<!-- a check item (Claude's memory toggle): ticked = the next answer is
					     grounded in the company wiki; an empty wiki shows why it can't be -->
					<button
						type="button"
						role="menuitemcheckbox"
						class="jv-plus-item jv-plus-item--two"
						:class="{ 'jv-plus-item--off': !wikiAvailable }"
						:aria-checked="String(wikiAvailable && wikiOn)"
						:aria-disabled="String(!wikiAvailable)"
						@click="wikiAvailable && choose('wiki')"
					>
						<svg v-bind="iconAttrs" aria-hidden="true">
							<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
							<path
								d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"
							/>
						</svg>
						<span class="jv-plus-two">
							<span class="jv-plus-label">Ground in wiki</span>
							<span class="jv-plus-desc">{{
								wikiAvailable
									? "Answer the next message from your wiki"
									: "No wiki pages yet"
							}}</span>
						</span>
						<svg
							v-if="wikiAvailable && wikiOn"
							v-bind="iconAttrs"
							class="jv-plus-check"
							aria-hidden="true"
						>
							<path d="M20 6 9 17l-5-5" />
						</svg>
					</button>
				</template>
			</template>

			<!-- skills -->
			<template v-else-if="view === 'skills'">
				<div class="jv-plus-head">
					<button
						type="button"
						class="jv-plus-back"
						title="Back"
						aria-label="Back"
						@click="go('main')"
					>
						<svg v-bind="iconAttrs" aria-hidden="true">
							<path d="m15 18-6-6 6-6" />
						</svg>
					</button>
					<span>Skills</span>
				</div>
				<input
					ref="search"
					v-model="query"
					class="jv-plus-search"
					type="search"
					placeholder="Search skills"
					aria-label="Search skills"
				/>
				<div class="jv-plus-list">
					<button
						v-for="s in shownSkills"
						:key="s.name || s.skill_name"
						type="button"
						role="menuitemradio"
						class="jv-plus-item jv-plus-item--two"
						:aria-checked="String(s.skill_name === selectedSkill)"
						@click="pickSkill(s)"
					>
						<span class="jv-plus-two">
							<span class="jv-plus-label">/{{ s.skill_name }}</span>
							<span v-if="s.description" class="jv-plus-desc">{{
								s.description
							}}</span>
							<span v-if="s.shared_by" class="jv-plus-desc"
								>Shared by {{ s.shared_by }}</span
							>
						</span>
						<svg
							v-if="s.skill_name === selectedSkill"
							v-bind="iconAttrs"
							class="jv-plus-check"
							aria-hidden="true"
						>
							<path d="M20 6 9 17l-5-5" />
						</svg>
					</button>
					<p v-if="!shownSkills.length" class="jv-plus-empty">
						{{
							usableSkills.length
								? "No skill matches that."
								: "No skills yet. Save a way of working as a skill and pick it here."
						}}
					</p>
				</div>
				<button
					v-if="canManageSkills"
					type="button"
					class="jv-plus-foot"
					@click="choose('manage-skills')"
				>
					Manage skills
				</button>
			</template>

			<!-- connectors -->
			<template v-else>
				<div class="jv-plus-head">
					<button
						type="button"
						class="jv-plus-back"
						title="Back"
						aria-label="Back"
						@click="go('main')"
					>
						<svg v-bind="iconAttrs" aria-hidden="true">
							<path d="m15 18-6-6 6-6" />
						</svg>
					</button>
					<span>Focus on one app</span>
				</div>
				<div class="jv-plus-list">
					<button
						v-if="focus"
						type="button"
						role="menuitem"
						class="jv-plus-item"
						@click="pickConnector(null)"
					>
						<svg v-bind="iconAttrs" aria-hidden="true">
							<path d="M18 6 6 18M6 6l12 12" />
						</svg>
						<span class="jv-plus-label">Clear focus</span>
					</button>
					<button
						v-for="c in connectors"
						:key="c.key"
						type="button"
						role="menuitemradio"
						class="jv-plus-item"
						:aria-checked="String(c.key === focus?.key)"
						@click="pickConnector(c)"
					>
						<!-- the app's own mark (connectorLogos.js, as ConnectorLogo draws it), else the plug -->
						<svg
							v-if="logoOf(c)"
							class="jv-plus-logo"
							:viewBox="logoOf(c).viewBox"
							width="16"
							height="16"
							:fill="logoOf(c).fill || 'currentColor'"
							aria-hidden="true"
						>
							<path :d="logoOf(c).path" />
						</svg>
						<svg v-else v-bind="iconAttrs" aria-hidden="true">
							<path
								d="M12 22v-5M9 8V2M15 8V2M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"
							/>
						</svg>
						<span class="jv-plus-label">{{ c.label }}</span>
						<svg
							v-if="c.key === focus?.key"
							v-bind="iconAttrs"
							class="jv-plus-check"
							aria-hidden="true"
						>
							<path d="M20 6 9 17l-5-5" />
						</svg>
					</button>
					<p v-if="!connectors.length" class="jv-plus-empty">No connected apps yet.</p>
				</div>
				<button
					v-if="canBrowseConnectors"
					type="button"
					class="jv-plus-foot"
					@click="choose('browse-connectors')"
				>
					Browse connectors
				</button>
			</template>
		</div>
	</div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { CONNECTOR_LOGOS } from "../settings/connectorLogos.js";
import SkillPill from "./SkillPill.vue";

const props = defineProps({
	disabled: { type: Boolean, default: false },
	// {name, skill_name, description, enabled, shared_by?}: list_custom_skills rows.
	skills: { type: Array, default: () => [] },
	// skill_name of the skill armed for the next message, or "".
	selectedSkill: { type: String, default: "" },
	// {key, label, preset}: connected apps this user can focus on.
	connectors: { type: Array, default: () => [] },
	// The chat's connector focus, {key, label}, or null.
	focus: { type: Object, default: null },
	showConnectors: { type: Boolean, default: true },
	// The wiki can ground answers (ui.wiki_state "ok"); empty: on, but no pages yet.
	wikiAvailable: { type: Boolean, default: false },
	wikiEmpty: { type: Boolean, default: false },
	wikiOn: { type: Boolean, default: false },
	canManageSkills: { type: Boolean, default: false },
	canBrowseConnectors: { type: Boolean, default: false },
	// The pills for what is on (skill, wiki, app) beside the button; a host with no room
	// there (the PWA's composer row) renders SkillPill itself and turns this off.
	showPill: { type: Boolean, default: true },
});
// open: the menu opened (the host refreshes its lists). attach | manage-skills |
// browse-connectors: plain actions. skill: a row, or null to clear. connector: a row, or
// null to clear the focus. wiki: the new on/off value.
const emit = defineEmits([
	"open",
	"attach",
	"skill",
	"connector",
	"wiki",
	"manage-skills",
	"browse-connectors",
]);

const iconAttrs = {
	width: 16,
	height: 16,
	viewBox: "0 0 24 24",
	fill: "none",
	stroke: "currentColor",
	"stroke-width": "1.7",
	"stroke-linecap": "round",
	"stroke-linejoin": "round",
};

const root = ref(null);
const trigger = ref(null);
const search = ref(null);
const open = ref(false);
const view = ref("main");
const query = ref("");

const panelLabel = computed(
	() => ({ main: "Add to message", skills: "Skills", connectors: "Connectors" }[view.value])
);
// Only skills the server would honour by "/slug": enabled ones (shared rows arrive
// enabled-only), with a slug the server's token pattern accepts.
const usableSkills = computed(() =>
	props.skills.filter(
		(s) => s && s.enabled !== 0 && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(s.skill_name || "")
	)
);
const shownSkills = computed(() => {
	const q = query.value.trim().toLowerCase();
	if (!q) return usableSkills.value;
	return usableSkills.value.filter((s) =>
		`${s.skill_name} ${s.description || ""}`.toLowerCase().includes(q)
	);
});
// Plain data, no frappe-ui (unlike ConnectorLogo's fallback), so the PWA can share this.
const logoOf = (c) => CONNECTOR_LOGOS[c.preset] || null;
// Line the panel up with the composer box it sits in (left edges flush) and open it just
// above that box, not over the text being typed. Measured on open; without a composer
// around it the panel simply opens above the button.
const panelStyle = ref({});
function placePanel() {
	const box = root.value?.closest(".jv-composer");
	if (!box) return (panelStyle.value = {});
	const me = root.value.getBoundingClientRect();
	const card = box.getBoundingClientRect();
	// never closer than 12px to the screen's edge (the PWA's composer is full-width)
	panelStyle.value = {
		left: `${Math.round(Math.max(card.left, 12) - me.left)}px`,
		bottom: `${Math.round(me.bottom - card.top) + 8}px`,
	};
}

function toggle() {
	if (props.disabled) return;
	open.value ? close() : show();
}
function show() {
	open.value = true;
	view.value = "main";
	placePanel();
	query.value = "";
	emit("open");
	document.addEventListener("pointerdown", onOutside, true);
	// On the document, not the panel: a sub-list replaces the row that had focus, so
	// focus can sit on <body> and the panel would never hear the key.
	document.addEventListener("keydown", onKey, true);
	focusFirst();
}
function close(refocus = false) {
	open.value = false;
	document.removeEventListener("pointerdown", onOutside, true);
	document.removeEventListener("keydown", onKey, true);
	if (refocus) nextTick(() => trigger.value?.focus());
}
// Keyboard users land in the list (the skill search when that list is up).
function focusFirst() {
	nextTick(() => {
		if (view.value === "skills" && search.value) return search.value.focus();
		root.value?.querySelector(".jv-plus-panel [role^='menuitem']")?.focus();
	});
}
function onOutside(e) {
	if (root.value && !root.value.contains(e.target)) close();
}
function go(next) {
	view.value = next;
	query.value = "";
	focusFirst();
}
function choose(action) {
	close();
	if (action === "wiki") emit("wiki", !props.wikiOn);
	else emit(action);
}
function pickSkill(s) {
	close();
	emit("skill", s);
}
function pickConnector(c) {
	close();
	emit("connector", c);
}
// Escape steps back out of a sub-list, then closes; arrow keys move between rows.
function onKey(e) {
	if (!open.value) return;
	if (e.key === "Escape") {
		e.preventDefault();
		e.stopPropagation();
		if (view.value !== "main") go("main");
		else close(true);
		return;
	}
	if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
	const rows = [...(root.value?.querySelectorAll(".jv-plus-panel [role^='menuitem']") || [])];
	if (!rows.length) return;
	e.preventDefault();
	const at = rows.indexOf(document.activeElement);
	const next =
		e.key === "ArrowDown" ? (at + 1) % rows.length : (at - 1 + rows.length) % rows.length;
	rows[next].focus();
}
watch(
	() => props.disabled,
	(d) => d && open.value && close()
);
onBeforeUnmount(() => {
	document.removeEventListener("pointerdown", onOutside, true);
	document.removeEventListener("keydown", onKey, true);
});
</script>

<style scoped>
.jv-plus {
	position: relative;
	display: inline-flex;
	align-items: center;
	gap: 4px;
	min-width: 0;
}
.jv-plus-btn {
	width: 30px;
	height: 30px;
	display: flex;
	align-items: center;
	justify-content: center;
	background: transparent;
	border: none;
	border-radius: 7px;
	cursor: pointer;
	color: var(--text-3, var(--ink5));
	flex: none;
}
.jv-plus-btn.on,
.jv-plus-btn:hover:not(:disabled) {
	background: var(--surface-2, var(--card2));
	color: var(--text, var(--ink9));
}
.jv-plus-btn:disabled {
	opacity: 0.55;
	cursor: not-allowed;
}
.jv-plus-panel {
	position: absolute;
	bottom: calc(100% + 8px);
	left: 0;
	z-index: 40;
	width: min(300px, calc(100vw - 32px));
	max-height: min(380px, 60vh);
	display: flex;
	flex-direction: column;
	padding: 6px;
	background: var(--surface, var(--card));
	border: 1px solid var(--border);
	border-radius: 12px;
	box-shadow: 0 10px 30px rgba(0, 0, 0, 0.14);
}
.jv-plus-item {
	display: flex;
	align-items: center;
	gap: 10px;
	width: 100%;
	min-height: 36px;
	padding: 7px 9px;
	border: none;
	border-radius: 8px;
	background: transparent;
	color: var(--text, var(--ink9));
	font-family: inherit;
	font-size: 13.5px;
	text-align: left;
	cursor: pointer;
}
.jv-plus-item > svg:first-child {
	flex: none;
	color: var(--text-2, var(--ink7));
}
.jv-plus-item:hover,
.jv-plus-item:focus-visible {
	background: var(--surface-2, var(--card2));
	outline: none;
}
.jv-plus-item--two {
	align-items: flex-start;
}
.jv-plus-label {
	flex: 1;
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-plus-two {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
	gap: 1px;
}
.jv-plus-desc {
	font-size: 12px;
	color: var(--text-3, var(--ink5));
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-plus-hint {
	max-width: 110px;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-size: 12px;
	color: var(--cta, var(--accent));
}
.jv-plus-chev {
	flex: none;
	color: var(--text-3, var(--ink5));
}
.jv-plus-check {
	flex: none;
	color: var(--cta, var(--accent));
}
.jv-plus-sep {
	height: 1px;
	margin: 4px 6px;
	background: var(--border);
}
/* the wiki row while the wiki has no pages: shown, so the option is known, but inert */
.jv-plus-item--off {
	cursor: default;
	opacity: 0.6;
}
.jv-plus-item--off:hover {
	background: transparent;
}
.jv-plus-head {
	display: flex;
	align-items: center;
	gap: 6px;
	padding: 2px 4px 6px;
	font-size: 13px;
	font-weight: 600;
	color: var(--text, var(--ink9));
}
.jv-plus-back {
	width: 26px;
	height: 26px;
	display: flex;
	align-items: center;
	justify-content: center;
	border: none;
	border-radius: 6px;
	background: transparent;
	color: var(--text-2, var(--ink7));
	cursor: pointer;
}
.jv-plus-back:hover {
	background: var(--surface-2, var(--card2));
}
.jv-plus-search {
	margin: 0 4px 6px;
	padding: 7px 10px;
	border: 1px solid var(--border);
	border-radius: 8px;
	background: var(--surface-1, var(--card));
	color: var(--text, var(--ink9));
	font-family: inherit;
	font-size: 13px;
	outline: none;
}
.jv-plus-search:focus {
	border-color: var(--cta, var(--accent));
}
.jv-plus-list {
	overflow-y: auto;
	min-height: 0;
}
.jv-plus-empty {
	margin: 6px 10px 10px;
	font-size: 12.5px;
	line-height: 1.4;
	color: var(--text-3, var(--ink5));
}
.jv-plus-foot {
	margin-top: 4px;
	padding: 8px 9px;
	border: none;
	border-top: 1px solid var(--border);
	background: transparent;
	color: var(--text-2, var(--ink7));
	font-family: inherit;
	font-size: 13px;
	text-align: left;
	cursor: pointer;
	border-radius: 0 0 8px 8px;
}
.jv-plus-foot:hover {
	color: var(--text, var(--ink9));
	background: var(--surface-2, var(--card2));
}
</style>
