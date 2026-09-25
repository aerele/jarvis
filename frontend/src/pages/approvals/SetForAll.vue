<template>
	<!-- SP-D1: one section's "Set <field> for all N": a main field two or more of its
	     records still need, set once into each record's edit. -->
	<div>
		<div
			v-for="g in groups"
			:key="g.fieldname"
			class="mt-2 rounded bg-surface-amber-1 px-3 py-2"
		>
			<div class="flex flex-wrap items-center gap-2">
				<span class="min-w-0 flex-1 text-sm text-ink-amber-3">{{
					__("{0} records need {1}.", [g.indexes.length, g.label])
				}}</span>
				<Button
					:id="idOf(g) + '-toggle'"
					variant="subtle"
					size="sm"
					:label="__('Set {0} for all {1}', [g.label, g.indexes.length])"
					:aria-expanded="openKey === g.fieldname ? 'true' : 'false'"
					:aria-controls="idOf(g)"
					@click="toggle(g)"
				/>
			</div>
			<div
				v-if="openKey === g.fieldname"
				:id="idOf(g)"
				class="mt-2 flex flex-wrap items-end gap-2"
				@keydown.esc.stop.prevent="close(g)"
			>
				<div class="flex min-w-48 flex-1 flex-col gap-1">
					<label :for="idOf(g) + '-input'" class="text-sm text-ink-gray-7">{{
						g.label
					}}</label>
					<FieldControl
						:id="idOf(g) + '-input'"
						:field="panelField(g.field, value)"
						:suggestions="suggestions"
						@update="(v) => onInput(g, v)"
					/>
				</div>
				<Button
					variant="solid"
					size="sm"
					:label="__('Set for {0} records', [g.indexes.length])"
					:disabled="!String(value).trim()"
					@click="set(g)"
				/>
			</div>
		</div>
		<p
			v-if="note"
			ref="noteEl"
			role="status"
			tabindex="-1"
			class="mt-2 text-sm text-ink-gray-6 outline-none"
		>
			{{ note }}
		</p>
	</div>
</template>

<script setup>
import { ref, nextTick, onBeforeUnmount } from "vue";
import { Button } from "frappe-ui";
import FieldControl from "@/components/forms/FieldControl.vue";
import { searchLink } from "@/api";
import { panelField } from "@/lib/docFields";
import { coerceOut } from "@/lib/draftApply";
import { __ } from "@/lib/i18n";

const props = defineProps({
	sheet: { type: String, required: true },
	section: { type: String, required: true },
	groups: { type: Array, required: true },
});
const emit = defineEmits(["set"]);

const openKey = ref("");
const value = ref("");
const suggestions = ref([]);
const note = ref("");
const noteEl = ref(null);

const idOf = (g) => `sheet-${props.sheet}-set-${props.section}-${g.fieldname}`;
const focusId = (id) => {
	const el = document.getElementById(id);
	if (el) el.focus();
};

async function toggle(g) {
	const opening = openKey.value !== g.fieldname;
	openKey.value = opening ? g.fieldname : "";
	value.value = "";
	suggestions.value = [];
	if (!opening) return;
	await nextTick();
	focusId(idOf(g) + "-input");
}
function close(g) {
	openKey.value = "";
	nextTick(() => focusId(idOf(g) + "-toggle"));
}

let timer = null;
function onInput(g, v) {
	value.value = v;
	const f = panelField(g.field, v);
	if (f.control !== "link" || !f.options) return;
	clearTimeout(timer);
	timer = setTimeout(async () => {
		try {
			const rows = (await searchLink(f.options, v, 10)) || [];
			suggestions.value = rows.map((r) => String(r.value));
		} catch {
			suggestions.value = [];
		}
	}, 250);
}

async function set(g) {
	const v = coerceOut(panelField(g.field, value.value));
	if (v === "" || v == null) return;
	emit("set", g, v);
	note.value = __("Set {0} on {1} records.", [g.label, g.indexes.length]);
	openKey.value = "";
	await nextTick();
	if (noteEl.value) noteEl.value.focus();
}

onBeforeUnmount(() => clearTimeout(timer));
</script>
