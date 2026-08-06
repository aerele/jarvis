<template>
	<div v-if="meta" class="flex flex-col">
		<!-- 1. docname row: click-to-copy + like heart (D25) -->
		<div
			class="flex h-[45px] shrink-0 items-center justify-between border-b px-5 text-lg font-medium text-ink-gray-9"
		>
			<Tooltip text="Click to copy">
				<button class="cursor-copy truncate" @click="copyName">{{ docName }}</button>
			</Tooltip>
			<Button
				variant="ghost"
				:tooltip="meta.liked ? 'Unlike' : 'Like'"
				@click="docmeta.toggleLike()"
			>
				<template #icon>
					<FeatherIcon
						name="heart"
						class="h-4 w-4"
						:class="meta.liked ? 'fill-red-500' : 'fill-transparent stroke-current'"
					/>
				</template>
			</Button>
		</div>

		<div class="divide-y">
			<!-- 2. assignees - not offered on skills (§14 DA-09) -->
			<div v-if="showAssignees" class="px-5 py-4">
				<div class="flex items-center justify-between">
					<div class="text-sm text-ink-gray-5">Assignees</div>
					<Popover v-if="canWrite" placement="bottom-end">
						<template #target="{ togglePopover }">
							<Button
								variant="ghost"
								icon="plus"
								:tooltip="'Add assignee'"
								@click="togglePopover()"
							/>
						</template>
						<template #body>
							<div
								class="my-2 w-[320px] rounded-lg bg-surface-modal p-3 shadow-2xl ring-1 ring-black ring-opacity-5"
							>
								<div v-if="assignees.length" class="mb-2 flex flex-wrap gap-1.5">
									<div
										v-for="a in assignees"
										:key="a.user"
										class="flex h-6 items-center gap-1 rounded bg-surface-gray-2 px-2 text-sm text-ink-gray-8"
									>
										<span class="truncate">{{ a.full_name || a.user }}</span>
										<Button
											variant="ghost"
											icon="x"
											class="!h-4 !w-4"
											@click="docmeta.unassign(a.user)"
										/>
									</div>
								</div>
								<Autocomplete
									:options="assignableOptions"
									:modelValue="null"
									placeholder="Add assignee…"
									@update:modelValue="(opt) => opt && docmeta.assign(opt.value)"
								/>
							</div>
						</template>
					</Popover>
				</div>
				<div v-if="assignees.length" class="mt-2 flex items-center">
					<Tooltip v-for="a in assignees" :key="a.user" :text="a.full_name || a.user">
						<Avatar
							size="md"
							:image="a.image"
							:label="a.full_name || a.user"
							class="-mr-1.5 ring-2 ring-white hover:z-10 hover:scale-110"
						/>
					</Tooltip>
				</div>
				<div v-else class="mt-2 text-sm text-ink-gray-4">No assignees</div>
			</div>

			<!-- 3. attachments -->
			<div class="px-5 py-4">
				<div class="flex items-center justify-between">
					<div class="text-sm text-ink-gray-5">Attachments</div>
					<FileUploader
						v-if="canWrite"
						:upload-args="{ doctype: docmeta.doctype, docname: docName, private: 1 }"
						@success="(f) => docmeta.afterUpload(f)"
						@failure="onUploadError"
					>
						<template #default="{ openFileSelector, uploading }">
							<Button
								variant="ghost"
								icon="paperclip"
								:loading="uploading"
								:tooltip="'Attach file'"
								@click="openFileSelector()"
							/>
						</template>
					</FileUploader>
				</div>
				<div v-if="attachments.length" class="mt-2 flex flex-col gap-1">
					<div
						v-for="f in attachments"
						:key="f.name"
						class="flex items-center justify-between gap-2 rounded p-1.5 text-base hover:bg-surface-gray-1"
					>
						<div class="flex min-w-0 items-center gap-2">
							<FeatherIcon
								name="file-text"
								class="size-4 shrink-0 text-ink-gray-5"
							/>
							<a
								:href="f.file_url"
								target="_blank"
								rel="noopener"
								class="truncate text-ink-gray-8 hover:underline"
							>
								{{ f.file_name || f.file_url }}
							</a>
						</div>
						<div class="flex shrink-0 items-center gap-1">
							<span class="text-sm text-ink-gray-5">{{
								convertSize(f.file_size)
							}}</span>
							<Button
								v-if="canWrite"
								variant="ghost"
								icon="x"
								class="!h-5 !w-5"
								@click="confirmDeleteAttachment(f)"
							/>
						</div>
					</div>
				</div>
				<div v-else class="mt-2 text-sm text-ink-gray-4">No attachments</div>
				<!-- §6.1 caveat, shown once an upload is rejected (O3) -->
				<div v-if="uploadHint" class="mt-2 text-xs text-ink-gray-5">{{ uploadHint }}</div>
			</div>

			<!-- 4. shared with (§14 F1) - DocShare block for Macro/Approval/Agent
			     Installation; skills replace it with their child-table block via #extra -->
			<div v-if="showShares" class="px-5 py-4">
				<div class="flex items-center justify-between">
					<div class="text-sm text-ink-gray-5">Shared with</div>
					<Popover v-if="canWrite" placement="bottom-end">
						<template #target="{ togglePopover }">
							<Button
								variant="ghost"
								icon="plus"
								:tooltip="'Share with a user'"
								@click="togglePopover()"
							/>
						</template>
						<template #body>
							<div
								class="my-2 w-[320px] rounded-lg bg-surface-modal p-3 shadow-2xl ring-1 ring-black ring-opacity-5"
							>
								<div v-if="shares.length" class="mb-2 flex flex-wrap gap-1.5">
									<div
										v-for="s in shares"
										:key="s.user"
										class="flex h-6 items-center gap-1 rounded bg-surface-gray-2 px-2 text-sm text-ink-gray-8"
									>
										<span class="truncate">{{ s.full_name || s.user }}</span>
										<Button
											variant="ghost"
											icon="x"
											class="!h-4 !w-4"
											@click="docmeta.toggleShare(s.user, 'remove')"
										/>
									</div>
								</div>
								<Autocomplete
									:options="shareableOptions"
									:modelValue="null"
									placeholder="Share with…"
									@update:modelValue="
										(opt) => opt && docmeta.toggleShare(opt.value, 'add')
									"
								/>
							</div>
						</template>
					</Popover>
				</div>
				<div v-if="shares.length" class="mt-2 flex items-center">
					<Tooltip v-for="s in shares" :key="s.user" :text="s.full_name || s.user">
						<Avatar
							size="md"
							:image="s.image"
							:label="s.full_name || s.user"
							class="-mr-1.5 ring-2 ring-white hover:z-10 hover:scale-110"
						/>
					</Tooltip>
				</div>
				<div v-else class="mt-2 text-sm text-ink-gray-4">Not shared</div>
			</div>

			<!-- 5. per-page extra block (skill share block lives here, §6.2) -->
			<slot name="extra" />

			<!-- 6. byline -->
			<div class="px-5 py-4 text-sm text-ink-gray-5">
				<div v-if="meta.created">
					Created by {{ meta.created.full_name || meta.created.owner }} ·
					{{ timeAgo(meta.created.creation) }}
				</div>
				<div v-if="meta.modified" class="mt-1">
					Modified {{ timeAgo(meta.modified.modified) }}
				</div>
			</div>
		</div>
	</div>
</template>

<script setup>
// DocMetaPanel - the right-panel doctype blocks (DESIGN-V3 §6.1 + §14 F1/DA-09):
// docname/like row · assignees (hidden on skills) · attachments · shared-with
// (Macro/Approval/Agent Installation) · #extra · byline. All mutations go
// through the shared useDocmeta object passed in by the page.
import { ref, computed, watch } from "vue";
import {
	Avatar,
	Autocomplete,
	Button,
	FeatherIcon,
	FileUploader,
	Popover,
	Tooltip,
	confirmDialog,
	toast,
} from "frappe-ui";
import { listShareableUsers } from "@/api";
import { timeAgo } from "@/utils/datetime";
import { errMessage } from "@/lib/errors";

const props = defineProps({
	docmeta: { type: Object, required: true }, // useDocmeta() object
	canWrite: { type: Boolean, default: false },
});

const SHARE_DOCTYPES = ["Jarvis Macro", "Jarvis Approval Request", "Jarvis Agent Installation"];

const meta = computed(() => props.docmeta.meta);
const docName = computed(() => props.docmeta.name);
const assignees = computed(() => (meta.value && meta.value.assignees) || []);
const attachments = computed(() => (meta.value && meta.value.attachments) || []);
const shares = computed(() => (meta.value && meta.value.shares) || []);

// §14 DA-09: skills keep the child-table share model; no assignees block
const showAssignees = computed(() => props.docmeta.doctype !== "Jarvis Custom Skill");
const showShares = computed(() => SHARE_DOCTYPES.includes(props.docmeta.doctype));

// ── people pickers (one fetch, lazily once the panel is writable) ─────────────
const users = ref([]);
let usersLoaded = false;
watch(
	() => props.canWrite,
	async (v) => {
		if (!v || usersLoaded) return;
		usersLoaded = true;
		try {
			users.value = (await listShareableUsers()) || [];
		} catch (e) {
			usersLoaded = false; // retry on next write-mode entry
		}
	},
	{ immediate: true }
);

const assignableOptions = computed(() => {
	const taken = new Set(assignees.value.map((a) => a.user));
	return users.value
		.filter((u) => !taken.has(u.name))
		.map((u) => ({ label: u.full_name || u.name, value: u.name }));
});
const shareableOptions = computed(() => {
	const taken = new Set(shares.value.map((s) => s.user));
	if (meta.value && meta.value.created) taken.add(meta.value.created.owner);
	return users.value
		.filter((u) => !taken.has(u.name))
		.map((u) => ({ label: u.full_name || u.name, value: u.name }));
});

// ── docname copy ──────────────────────────────────────────────────────────────
async function copyName() {
	try {
		await navigator.clipboard.writeText(docName.value);
		toast.success("Copied");
	} catch (e) {
		toast.error("Couldn't copy to clipboard");
	}
}

// ── attachments ───────────────────────────────────────────────────────────────
function confirmDeleteAttachment(f) {
	confirmDialog({
		title: "Delete attachment?",
		message: `Delete "${esc(f.file_name || f.file_url)}"? This can't be undone.`,
		onConfirm: async ({ hideDialog }) => {
			await props.docmeta.deleteAttachment(f.name);
			hideDialog();
		},
	});
}

// O3: stock upload_file can reject (mimetype allowlist for website users,
// if_owner write on approvals) - surface the server message as a toast and
// show the allowed-types caveat as an inline hint.
const uploadHint = ref("");
function onUploadError(e) {
	toast.error(uploadErrMsg(e));
	uploadHint.value =
		"Portal accounts can attach JPG, PNG, GIF, PDF, TXT, CSV and MS Office files only.";
}
function uploadErrMsg(e) {
	if (e && e._server_messages) {
		try {
			return JSON.parse(JSON.parse(e._server_messages)[0]).message;
		} catch (err) {
			/* fall through */
		}
	}
	const msg = errMessage(e);
	return msg === "Something went wrong. Please try again." ? "Upload failed" : msg;
}

// ── helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
	return String(s).replace(
		/[&<>"]/g,
		(c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
	);
}
function convertSize(bytes) {
	const n = Number(bytes);
	if (!n || n <= 0) return "";
	const units = ["B", "KB", "MB", "GB"];
	let v = n;
	let i = 0;
	while (v >= 1024 && i < units.length - 1) {
		v /= 1024;
		i++;
	}
	return `${i === 0 || v >= 10 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}
</script>
