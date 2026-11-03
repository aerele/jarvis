<template>
	<fieldset :disabled="saving" class="space-y-4">
		<p class="text-sm text-ink-gray-6">
			Choose the exact Helpdesk scope. A named reviewer confirms each type or priority
			change. Runs use UTC timestamps and support manual review only.
		</p>
		<FormControl
			v-for="field in fields"
			:key="field.key"
			:label="field.label"
			:type="field.list ? 'textarea' : field.key === 'max_tickets' ? 'number' : 'text'"
			:description="field.list ? 'Enter one exact record name per line.' : field.help"
			v-model="form[field.key]"
		/>
		<p
			v-for="(message, key) in validationErrors"
			:key="key"
			class="text-sm text-ink-red-4"
			role="alert"
		>
			{{ message }}
		</p>
		<Button label="Save configuration" :loading="saving" @click="save" />
	</fieldset>
</template>

<script setup>
import { reactive, watch } from "vue";
import { Button, FormControl } from "frappe-ui";
const props = defineProps({
	config: { type: Object, default: () => ({}) },
	saving: Boolean,
	validationErrors: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["save", "dirty"]);
const fields = [
	{
		key: "policy_version",
		label: "Policy reference",
		help: "Use ticket-triage-v1 for this release.",
	},
	{ key: "from_at", label: "Modified from (UTC)", help: "Format: 2026-11-01T00:00:00Z" },
	{
		key: "to_at",
		label: "Modified through (UTC)",
		help: "Format: 2026-11-02T00:00:00Z. Use a past cutoff.",
	},
	{ key: "eligible_team_names", label: "Helpdesk teams", list: true },
	{ key: "eligible_status_names", label: "Open ticket statuses", list: true },
	{ key: "allowed_ticket_types", label: "Allowed ticket types", list: true },
	{ key: "allowed_priorities", label: "Allowed priorities", list: true },
	{
		key: "max_tickets",
		label: "Maximum tickets",
		help: "1 to 250. An over-limit scope is refused.",
	},
];
const form = reactive({});
let seeding = false;
watch(
	() => props.config,
	(value) => {
		seeding = true;
		for (const field of fields)
			form[field.key] = field.list
				? Array.isArray(value[field.key])
					? value[field.key].join("\n")
					: ""
				: String(
						value[field.key] ??
							(field.key === "policy_version"
								? "ticket-triage-v1"
								: field.key === "max_tickets"
								? 50
								: "")
				  );
		seeding = false;
		emit("dirty", false);
	},
	{ immediate: true, deep: true }
);
watch(
	form,
	() => {
		if (!seeding) emit("dirty", true);
	},
	{ flush: "sync" }
);
function save() {
	const value = { assignment_mode: "none" };
	for (const field of fields)
		value[field.key] = field.list
			? [
					...new Set(
						String(form[field.key])
							.split("\n")
							.map((v) => v.trim())
							.filter(Boolean)
					),
			  ].sort()
			: field.key === "max_tickets"
			? Number(form[field.key])
			: String(form[field.key]).trim();
	emit("save", value);
}
</script>
