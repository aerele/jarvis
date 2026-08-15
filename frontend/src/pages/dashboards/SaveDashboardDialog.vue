<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: dialogTitle, size: 'md' }"
		@update:modelValue="(v) => emit('update:modelValue', v)"
	>
		<template #body-content>
			<div class="flex flex-col gap-4">
				<template v-if="!shareOnly">
					<div>
						<FormControl
							type="text"
							label="Title"
							required
							placeholder="e.g. Monthly sales overview"
							:modelValue="form.dashboard_title"
							@update:modelValue="(v) => (form.dashboard_title = v)"
						/>
						<ErrorMessage :message="fieldErrors.title" class="mt-1" />
					</div>
					<FormControl
						type="textarea"
						label="Description"
						:rows="2"
						placeholder="What this dashboard shows (optional)"
						:modelValue="form.description"
						@update:modelValue="(v) => (form.description = v)"
					/>
				</template>

				<FormControl
					type="select"
					label="Who can see it"
					:options="scopeOptions"
					:modelValue="form.scope"
					@update:modelValue="(v) => (form.scope = v)"
				/>
				<div v-if="form.scope === 'Role'">
					<FormControl
						type="select"
						label="Role"
						:options="roleOptions"
						:modelValue="form.target_role"
						@update:modelValue="(v) => (form.target_role = v)"
					/>
					<ErrorMessage :message="fieldErrors.role" class="mt-1" />
				</div>

				<!-- read-only detected sources (parsed from the canvas html) -->
				<div v-if="!shareOnly && sources.length" class="flex flex-col gap-1.5">
					<span class="text-xs text-ink-gray-5">Data sources</span>
					<div
						v-for="s in sources"
						:key="s.source_name"
						class="flex items-center justify-between rounded-md border px-2.5 py-1.5"
					>
						<span class="truncate text-base text-ink-gray-8">{{ s.source_name }}</span>
						<Badge :label="s.tool" theme="gray" variant="subtle" />
					</div>
				</div>

				<ErrorMessage :message="error" />
				<!-- theme rejection is recoverable: hand the violations to the model,
				     so the user never relays CSS jargon (P0-2) -->
				<div v-if="themeError && !shareOnly" class="flex justify-start">
					<Button
						label="Ask the assistant to fix these"
						iconLeft="message-circle"
						@click="askAssistantToFix"
					/>
				</div>
			</div>
		</template>

		<template #actions>
			<div class="flex justify-end gap-2">
				<Button
					label="Cancel"
					:disabled="saving"
					@click="emit('update:modelValue', false)"
				/>
				<Button variant="solid" :label="saveLabel" :loading="saving" @click="save" />
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// SaveDashboardDialog - the builder's save/save-changes form (title,
// description, visibility scope, read-only detected sources) and, via
// `shareOnly`, the view page's "Share…" dialog (the same scope section with
// everything else hidden; the payload re-sends the loaded detail unchanged).
// Sources come from the parsed #jarvis-sources block (DashboardCanvas emit) -
// the same list the payload's `sources` key carries.
import { reactive, ref, computed, watch } from "vue";
import { Badge, Button, Dialog, ErrorMessage, FormControl } from "frappe-ui";
import { saveDashboard } from "@/api/dashboards";
import { themeLabel } from "@/lib/dashboardThemes";
import { errMessage as errMsg } from "@/lib/errors";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	caps: { type: Object, default: () => ({}) },
	html: { type: String, default: "" },
	sources: { type: Array, default: () => [] }, // [{source_name, tool, spec}]
	// existing detail when editing/sharing: {name, dashboard_title, description,
	// scope, target_role, source_conversation}
	editing: { type: Object, default: null },
	conversation: { type: String, default: "" }, // source_conversation for new saves
	shareOnly: { type: Boolean, default: false },
	// active render theme key (lib/dashboardThemes); persisted with the save
	theme: { type: String, default: "" },
});

const emit = defineEmits(["update:modelValue", "saved", "fix-in-chat"]);

// A theme-standard rejection (ThemeStandardError) is recoverable via the model:
// surface the human messages AND offer a one-click hand-off to the builder chat.
function isThemeError(e) {
	if (!e) return false;
	if (e.exc_type === "ThemeStandardError") return true;
	const m = (e.messages && e.messages[0]) || e.message || "";
	return /does not follow the selected theme/i.test(m);
}

const SCOPE_LABELS = { User: "Private", Role: "Shared with a role", Org: "Everyone" };

const scopeOptions = computed(() =>
	(props.caps.creatable_scopes || ["User"]).map((s) => ({
		label: SCOPE_LABELS[s] || s,
		value: s,
	}))
);
const roleOptions = computed(() => [
	{ label: "Select a role", value: "" },
	...(props.caps.manageable_roles || []).map((r) => ({ label: r, value: r })),
]);

const dialogTitle = computed(() =>
	props.shareOnly ? "Share dashboard" : props.editing ? "Save changes" : "Save dashboard"
);
const saveLabel = computed(() =>
	props.shareOnly ? "Update sharing" : props.editing ? "Save changes" : "Save dashboard"
);

const form = reactive({ dashboard_title: "", description: "", scope: "User", target_role: "" });
const saving = ref(false);
const error = ref("");
const themeError = ref(false);
const fieldErrors = reactive({ title: "", role: "" });

function seedForm() {
	const e = props.editing;
	form.dashboard_title = (e && e.dashboard_title) || "";
	form.description = (e && e.description) || "";
	const scopes = props.caps.creatable_scopes || ["User"];
	// private-first: even an admin (whose creatable_scopes lead with Org) must opt
	// INTO sharing, never share by default
	form.scope =
		e && e.scope && scopes.includes(e.scope)
			? e.scope
			: scopes.includes("User")
			? "User"
			: scopes[0] || "User";
	form.target_role = (e && e.target_role) || "";
}

// Reseed only when the underlying document IDENTITY changes (a different saved
// dashboard, "New dashboard", or the first save turning a draft into a saved
// doc) — NOT on every reopen. The new theme validator makes reject→fix→reopen the
// most common reason a NEW dashboard's dialog opens twice, so preserve whatever
// title/description/scope the user already typed across that loop (P1-5).
watch(
	() => (props.editing && props.editing.name) || "",
	() => seedForm(),
	{ immediate: true }
);

// On open, only clear transient error/field state — never the draft fields.
watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		error.value = "";
		themeError.value = false;
		fieldErrors.title = "";
		fieldErrors.role = "";
	}
);

async function save() {
	fieldErrors.title = "";
	fieldErrors.role = "";
	error.value = "";
	themeError.value = false;
	const e = props.editing;
	const title = props.shareOnly ? (e && e.dashboard_title) || "" : form.dashboard_title.trim();
	if (!title) {
		fieldErrors.title = "Give the dashboard a title.";
		return;
	}
	if (form.scope === "Role" && !form.target_role) {
		fieldErrors.role = "Pick the role to share with.";
		return;
	}
	// save_dashboard rejects unknown keys, so the payload carries exactly the
	// documented set; target_role only rides along for Role scope.
	const payload = {
		dashboard_title: title,
		description: props.shareOnly ? (e && e.description) || "" : form.description || "",
		html: props.html,
		scope: form.scope,
		sources: props.sources,
		source_conversation: (e && e.source_conversation) || props.conversation || "",
		// share-only re-sends the stored theme; the builder persists the picker's
		theme: props.shareOnly ? (e && e.theme) || "Jarvis" : themeLabel(props.theme),
	};
	if (e && e.name) payload.name = e.name;
	if (form.scope === "Role") payload.target_role = form.target_role;
	saving.value = true;
	try {
		const detail = await saveDashboard(payload);
		emit("saved", detail);
		emit("update:modelValue", false);
	} catch (err) {
		error.value = errMsg(err);
		themeError.value = isThemeError(err);
	} finally {
		saving.value = false;
	}
}

// Hand the theme violations to the builder chat so the model self-corrects — the
// user never has to relay CSS jargon. The dialog closes; the parent forwards the
// text to the chat pane (P0-2).
function askAssistantToFix() {
	emit("fix-in-chat", {
		text:
			"Saving this dashboard failed the theme check. Please fix these and " +
			"re-publish the full document:\n\n" +
			error.value,
	});
	emit("update:modelValue", false);
}
</script>
