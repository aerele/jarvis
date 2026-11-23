<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: dialogTitle, size: 'lg' }"
		@update:modelValue="(v) => emit('update:modelValue', v)"
	>
		<template #body-content>
			<div class="flex max-h-[65vh] flex-col gap-5 overflow-y-auto overflow-x-hidden pr-1">
				<div>
					<FormControl
						type="text"
						label="Name"
						required
						placeholder="e.g. Overdue invoices"
						:modelValue="form.agent_name"
						@update:modelValue="(v) => (form.agent_name = v)"
					/>
					<ErrorMessage :message="errors.agent_name" class="mt-1" />
				</div>

				<div>
					<FormControl
						type="textarea"
						label="Description"
						:rows="2"
						placeholder="What this agent is for (optional)"
						:modelValue="form.description"
						@update:modelValue="(v) => (form.description = v)"
					/>
					<ErrorMessage :message="errors.description" class="mt-1" />
				</div>

				<div>
					<FormControl
						type="textarea"
						label="Instructions"
						required
						:rows="6"
						placeholder="Tell the agent what to do and how to answer."
						:modelValue="form.instructions"
						@update:modelValue="(v) => (form.instructions = v)"
					/>
					<p v-if="nearLimit" class="mt-1 text-p-sm text-ink-gray-5">
						{{ form.instructions.length.toLocaleString("en-US") }} of
						{{ MAX_INSTRUCTIONS.toLocaleString("en-US") }} characters
					</p>
					<ErrorMessage :message="errors.instructions" class="mt-1" />
				</div>

				<div>
					<UserAgentToolPicker
						v-model="form.tools"
						:groups="groups"
						:max-tools="maxTools"
						:loading="toolsLoading"
					/>
					<ErrorMessage :message="toolsError" class="mt-1" />
					<ErrorMessage :message="errors.tools" class="mt-1" />
				</div>

				<div class="flex flex-col gap-1">
					<Switch
						label="Can run code and work with files"
						:modelValue="form.allow_shell"
						@update:modelValue="(v) => (form.allow_shell = v)"
					/>
					<p class="text-p-sm text-ink-gray-5">
						The agent gets its own private workspace where it can run commands and keep
						files.
					</p>
				</div>

				<div v-if="browser.enabled" class="flex flex-col gap-3">
					<div class="flex flex-col gap-1">
						<Switch
							label="Can use a web browser"
							:disabled="browserCapped"
							:modelValue="form.allow_browser"
							@update:modelValue="(v) => (form.allow_browser = v)"
						/>
						<p class="text-p-sm text-ink-gray-5">
							{{
								browserCapped
									? `Only ${browser.max} agents per workspace can have a browser.`
									: "The agent gets its own private browser. Logins stay saved for this agent only."
							}}
						</p>
					</div>
					<template v-if="form.allow_browser">
						<TabButtons
							:buttons="BROWSER_MODES"
							:modelValue="form.browser_mode"
							@update:modelValue="(v) => (form.browser_mode = v)"
						/>
						<div v-if="form.browser_mode === 'listed'">
							<FormControl
								type="textarea"
								label="Sites, one per line"
								:rows="4"
								placeholder="example.com"
								:modelValue="form.browser_sites"
								@update:modelValue="(v) => (form.browser_sites = v)"
							/>
							<p v-if="nearSites" class="mt-1 text-p-sm text-ink-gray-5">
								{{ siteCount }} of {{ MAX_BROWSER_SITES }} sites
							</p>
						</div>
						<div
							v-else
							class="flex items-start gap-2 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
						>
							<FeatherIcon name="alert-triangle" class="size-4 shrink-0" />
							<span>
								A web page can trick the agent into sending data it has read to any
								site. Use listed sites unless you need this.
							</span>
						</div>
					</template>
				</div>

				<section class="flex flex-col gap-4">
					<div class="text-base font-medium text-ink-gray-9">Schedule</div>
					<Switch
						label="Run automatically"
						:modelValue="form.schedule_enabled"
						@update:modelValue="(v) => (form.schedule_enabled = v)"
					/>
					<div
						v-if="form.schedule_enabled"
						:class="[
							'grid gap-4',
							form.frequency === 'daily' ? 'grid-cols-2' : 'grid-cols-3',
						]"
					>
						<FormControl
							type="select"
							label="Frequency"
							:options="FREQUENCY_OPTIONS"
							:modelValue="form.frequency"
							@update:modelValue="setFrequency"
						/>
						<FormControl
							type="time"
							label="Time"
							:modelValue="form.time"
							@update:modelValue="(v) => (form.time = v)"
						/>
						<FormControl
							v-if="form.frequency !== 'daily'"
							type="select"
							label="Day"
							:options="dayOptions"
							:modelValue="form.day"
							@update:modelValue="(v) => (form.day = v)"
						/>
					</div>
					<ErrorMessage :message="errors.time || errors.day" />
					<div>
						<FormControl
							type="textarea"
							label="Prompt for each run"
							:rows="3"
							placeholder="e.g. List invoices more than 30 days overdue."
							:modelValue="form.prompt"
							@update:modelValue="(v) => (form.prompt = v)"
						/>
						<p class="mt-1 text-p-sm text-ink-gray-5">
							Sent to the agent on each scheduled run, and by Run now.
						</p>
						<ErrorMessage :message="errors.prompt" class="mt-1" />
					</div>
				</section>

				<!-- The sync runs after the save, so its failure arrives later, on the
				     row: shown here from the live agent, with the way to try again. -->
				<div v-if="syncText" class="flex flex-col items-start gap-2">
					<ErrorMessage :message="syncText" />
					<Button
						v-if="retryable"
						label="Retry sync"
						:loading="retrying"
						@click="retrySync"
					/>
				</div>

				<ErrorMessage :message="error" />
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
// Create / edit dialog of the My agents page. Save runs the two calls the server
// splits it into: create_user_agent or update_user_agent for the definition (which
// starts a sync), then set_user_agent_schedule when the schedule changed (which does
// not). Each call that lands is reported through `saved` so the list is current even
// when the next call fails; after a create the host passes the new agent back as
// `agent`, which turns the dialog into an edit, so a second Save can never create a
// duplicate.
import { computed, reactive, ref, watch } from "vue";
import {
	Button,
	Dialog,
	ErrorMessage,
	FeatherIcon,
	FormControl,
	Switch,
	TabButtons,
	toast,
} from "frappe-ui";
import UserAgentToolPicker from "./UserAgentToolPicker.vue";
import * as userAgentsApi from "@/api/userAgents";
import { errMessage as errMsg, errHtml } from "@/lib/errors";
import { DAY_OF_MONTH_OPTIONS, WEEKDAY_OPTIONS } from "@/lib/scheduleAnchor";
import {
	BROWSER_MODES,
	FREQUENCY_OPTIONS,
	MAX_BROWSER_SITES,
	MAX_INSTRUCTIONS,
	NEAR_BROWSER_SITES,
	NEAR_INSTRUCTIONS,
	canRetrySync,
	cleanSites,
	definitionChanged,
	definitionPayload,
	emptyForm,
	formFromAgent,
	scheduleChanged,
	schedulePayload,
	syncMessage,
	validateForm,
} from "@/lib/userAgents";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	// The live agent being edited; null for a new one.
	agent: { type: Object, default: null },
	// The workspace's web browser option: {enabled, max, inUse}. Hidden while not enabled.
	browser: { type: Object, default: () => ({ enabled: false, max: 3, inUse: 0 }) },
});
const emit = defineEmits(["update:modelValue", "saved"]);

const form = reactive(emptyForm());
const errors = reactive({});
const error = ref("");
const saving = ref(false);
const retrying = ref(false);

const groups = ref([]);
const maxTools = ref(64);
const toolsLoading = ref(false);
const toolsError = ref("");
let toolsLoaded = false;

const isEdit = computed(() => !!props.agent);
const dialogTitle = computed(() => (isEdit.value ? "Edit agent" : "New agent"));
const saveLabel = computed(() => (isEdit.value ? "Save changes" : "Create agent"));
const nearLimit = computed(() => form.instructions.length >= NEAR_INSTRUCTIONS);
const dayOptions = computed(() =>
	form.frequency === "weekly" ? WEEKDAY_OPTIONS : DAY_OF_MONTH_OPTIONS
);
const syncText = computed(() => (props.agent ? syncMessage(props.agent) : ""));
// Shown with a stored sentence only: a sync that just started is not worth a button here
// (the row's menu still offers Retry sync for one that never settles).
// The browser option is capped across the workspace; an agent that already has one keeps it.
const browserCapped = computed(
	() =>
		props.browser.inUse >= props.browser.max &&
		!(props.agent && props.agent.browser && props.agent.browser.enabled)
);
const siteCount = computed(() => cleanSites(form.browser_sites).length);
const nearSites = computed(() => siteCount.value >= NEAR_BROWSER_SITES);
const retryable = computed(() => !!props.agent && !!syncText.value && canRetrySync(props.agent));

// Seeded when the dialog opens, never when the host hands back the agent a Save just
// created: that would wipe what the user is still looking at.
watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		Object.assign(form, props.agent ? formFromAgent(props.agent) : emptyForm());
		for (const k of Object.keys(errors)) delete errors[k];
		error.value = "";
		loadTools();
	},
	{ immediate: true }
);

async function loadTools() {
	if (toolsLoaded || toolsLoading.value) return;
	toolsLoading.value = true;
	toolsError.value = "";
	try {
		const res = (await userAgentsApi.listPickableTools()) || {};
		groups.value = res.groups || [];
		maxTools.value = res.max_tools || 64;
		toolsLoaded = true;
	} catch (e) {
		toolsError.value = `${errMsg(e)} Close this and open it again to retry.`;
	} finally {
		toolsLoading.value = false;
	}
}

// Weekly needs a weekday and monthly a day, so a change of frequency picks the first
// one when the current day does not belong to the new frequency (AgentDetail's rule).
function setFrequency(v) {
	form.frequency = v;
	const options = v === "weekly" ? WEEKDAY_OPTIONS : v === "monthly" ? DAY_OF_MONTH_OPTIONS : [];
	if (options.length && !options.some((o) => o.value === form.day)) form.day = options[0].value;
}

async function save() {
	if (saving.value) return;
	error.value = "";
	for (const k of Object.keys(errors)) delete errors[k];
	Object.assign(errors, validateForm(form, { maxTools: maxTools.value }));
	if (Object.keys(errors).length) return;
	saving.value = true;
	try {
		const withBrowser = { browser: !!props.browser.enabled };
		let current = props.agent;
		if (!current) {
			current = await userAgentsApi.createUserAgent(definitionPayload(form, withBrowser));
			emit("saved", current);
		} else if (definitionChanged(form, current, withBrowser)) {
			current = await userAgentsApi.updateUserAgent(
				current.name,
				definitionPayload(form, withBrowser)
			);
			emit("saved", current);
		}
		if (scheduleChanged(form, current)) {
			current = await userAgentsApi.setUserAgentSchedule(
				current.name,
				schedulePayload(form)
			);
			emit("saved", current);
		}
		toast.success("Agent saved");
		emit("update:modelValue", false);
	} catch (e) {
		error.value = errMsg(e);
	} finally {
		saving.value = false;
	}
}

async function retrySync() {
	if (!props.agent || retrying.value) return;
	retrying.value = true;
	try {
		emit("saved", await userAgentsApi.retryUserAgentSync(props.agent.name));
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		retrying.value = false;
	}
}
</script>
