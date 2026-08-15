<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ size }"
		@update:modelValue="(v) => emit('update:modelValue', v)"
	>
		<template #body>
			<!-- header: file name · open in new tab · close -->
			<div class="flex items-center gap-2 border-b px-4 py-3 sm:px-5">
				<FeatherIcon name="file-text" class="size-4 shrink-0 text-ink-gray-5" />
				<div
					class="min-w-0 flex-1 truncate text-lg font-semibold text-ink-gray-9"
					:title="title"
				>
					{{ title }}
				</div>
				<Button
					v-if="fileUrl"
					variant="ghost"
					icon="external-link"
					:link="fileUrl"
					:tooltip="'Open in new tab'"
				/>
				<Button
					variant="ghost"
					icon="x"
					:tooltip="'Close'"
					@click="emit('update:modelValue', false)"
				/>
			</div>

			<!-- body: one renderer per kind (same routing as the chat artifact panel) -->
			<div class="bg-surface-gray-1">
				<iframe
					v-if="view.kind === 'pdf'"
					:src="fileUrl"
					class="h-[70vh] w-full bg-surface-white"
					title="PDF preview"
				/>

				<div
					v-else-if="view.kind === 'image'"
					class="grid min-h-40 place-items-center p-6"
				>
					<img :src="fileUrl" :alt="title" class="max-h-[65vh] max-w-full rounded" />
				</div>

				<iframe
					v-else-if="view.kind === 'html' || view.kind === 'svg'"
					:srcdoc="view.content"
					sandbox="allow-scripts"
					class="h-[70vh] w-full bg-surface-white"
					title="File preview"
				/>

				<template v-else-if="view.kind === 'table'">
					<div
						v-if="view.sheets.length > 1"
						class="flex items-center gap-1 overflow-x-auto border-b px-3 py-2"
					>
						<Button
							v-for="(sh, si) in view.sheets"
							:key="si"
							size="sm"
							:variant="si === sheetIdx ? 'subtle' : 'ghost'"
							:label="sh.name || `Sheet ${si + 1}`"
							@click="sheetIdx = si"
						/>
					</div>
					<div class="max-h-[65vh] overflow-auto">
						<table class="w-full border-collapse text-sm">
							<thead
								v-if="curSheet.rows.length"
								class="sticky top-0 bg-surface-gray-2"
							>
								<tr>
									<th
										v-for="(c, ci) in curSheet.rows[0]"
										:key="ci"
										class="whitespace-nowrap border-b px-3 py-2 text-left font-medium text-ink-gray-7"
									>
										{{ c }}
									</th>
								</tr>
							</thead>
							<tbody>
								<tr v-for="(r, ri) in curSheet.rows.slice(1)" :key="ri">
									<td
										v-for="(c, ci) in r"
										:key="ci"
										class="whitespace-nowrap border-b px-3 py-1.5 text-ink-gray-8"
									>
										{{ c }}
									</td>
								</tr>
							</tbody>
						</table>
					</div>
				</template>

				<pre
					v-else-if="view.kind === 'text'"
					class="max-h-[65vh] overflow-auto whitespace-pre-wrap break-words px-5 py-4 font-mono text-sm text-ink-gray-8"
					>{{ view.text }}</pre
				>

				<div
					v-else-if="view.kind === 'loading'"
					class="grid h-40 place-items-center text-base text-ink-gray-5"
				>
					Loading preview…
				</div>

				<div
					v-else
					class="flex h-40 flex-col items-center justify-center gap-2 px-6 text-center"
				>
					<FeatherIcon name="file" class="size-6 text-ink-gray-4" />
					<div class="text-base text-ink-gray-6">
						Preview not available for this file.
					</div>
					<a
						v-if="fileUrl"
						:href="fileUrl"
						:download="title"
						class="text-base font-medium text-ink-gray-8 underline"
					>
						Download {{ title }}
					</a>
				</div>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// FilePreview - reusable file preview dialog (extracted from ChatView's
// artifact panel, rebuilt in the current token system). Routing:
// pdf → same-origin iframe, image → <img>, html/svg → sandboxed srcdoc
// iframe (raw file fetched over the session cookie), everything else →
// api.previewFile (xlsx/csv → sheets table, txt/code → text), and a
// download-only fallback when nothing can render.
import { ref, computed, watch } from "vue";
import { Dialog, Button, FeatherIcon } from "frappe-ui";
import * as api from "@/api";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	fileUrl: { type: String, default: "" },
	fileName: { type: String, default: "" },
	// extension ("pdf", "xlsx", …) or mime type ("application/pdf", …)
	fileType: { type: String, default: "" },
	// spec asks for "lg", but frappe-ui's lg is max-w-lg (32rem) - too narrow
	// for PDFs and sheets, so the default is 4xl; pass size="lg" to shrink it.
	size: { type: String, default: "4xl" },
});

const emit = defineEmits(["update:modelValue"]);

const title = computed(
	() =>
		props.fileName ||
		decodeURIComponent((props.fileUrl || "").split("?")[0].split("/").pop() || "File")
);

// {kind: 'pdf'|'image'|'html'|'svg'|'table'|'text'|'loading'|'none', content?, sheets?, text?}
const view = ref({ kind: "loading" });
const sheetIdx = ref(0);
const curSheet = computed(() => {
	const v = view.value;
	if (v.kind !== "table" || !v.sheets?.length) return { rows: [] };
	return v.sheets[sheetIdx.value] || { rows: [] };
});

const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "avif", "ico"]);
function detectKind() {
	const t = (props.fileType || "").trim().toLowerCase();
	if (t.includes("/")) {
		// mime type
		if (t === "application/pdf") return "pdf";
		if (t === "image/svg+xml") return "svg";
		if (t.startsWith("image/")) return "image";
		if (t === "text/html") return "html";
		return "other";
	}
	const ext =
		t ||
		(
			(props.fileName || props.fileUrl || "").split("?")[0].split(".").pop() || ""
		).toLowerCase();
	if (ext === "pdf") return "pdf";
	if (ext === "svg") return "svg";
	if (IMAGE_EXT.has(ext)) return "image";
	if (ext === "html" || ext === "htm") return "html";
	return "other";
}

// seq guards a stale async load from clobbering a newer one (reopen on
// another file while a preview fetch is still in flight)
let loadSeq = 0;
async function load() {
	const seq = ++loadSeq;
	sheetIdx.value = 0;
	if (!props.fileUrl) {
		view.value = { kind: "none" };
		return;
	}
	const kind = detectKind();
	if (kind === "pdf" || kind === "image") {
		view.value = { kind };
		return;
	}
	view.value = { kind: "loading" };
	if (kind === "html" || kind === "svg") {
		try {
			const res = await fetch(props.fileUrl); // same-origin → session cookie
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const content = await res.text();
			if (seq === loadSeq) view.value = { kind, content };
		} catch (e) {
			if (seq === loadSeq) view.value = { kind: "none" };
		}
		return;
	}
	// everything else → backend preview (xlsx/csv → table, txt/code → text)
	try {
		const r = await api.previewFile(props.fileUrl);
		if (seq !== loadSeq) return;
		if (r && r.kind === "table" && Array.isArray(r.sheets) && r.sheets.length) {
			view.value = { kind: "table", sheets: r.sheets };
			return;
		}
		if (r && r.kind === "text") {
			view.value = { kind: "text", text: r.text || "" };
			return;
		}
	} catch (e) {
		/* fall through to download-only */
	}
	if (seq === loadSeq) view.value = { kind: "none" };
}

// fire on open AND on fileUrl changes while open (reusing the dialog for
// another file must not show the previous file's preview); load() itself
// handles the empty-url case and loadSeq guards stale fetches.
watch(
	() => [props.modelValue, props.fileUrl],
	([open]) => {
		if (open) load();
	}
);
</script>
