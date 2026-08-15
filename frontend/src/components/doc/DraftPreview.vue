<script setup>
import { ref, computed, onBeforeUnmount } from "vue";

const props = defineProps({
	model: { type: Object, required: true },
	headline: { type: String, default: "" },
	disabled: { type: Boolean, default: false },
});
const emit = defineEmits(["close", "edit", "confirm"]);

const isUpdate = computed(() => props.model.verb === "update");
const badge = computed(() => (isUpdate.value ? "Pending change" : "Draft - not saved"));
const docTitle = computed(() => {
	const m = props.model;
	return `${isUpdate.value ? "Update" : "New"} ${m.doctype}${
		m.docName ? " · " + m.docName : ""
	}`;
});
// Read-only fields to show: create -> the proposed (non-empty) fields; update ->
// populated or changed fields (so the change shows in context).
const fields = computed(() =>
	(props.model.fields || []).filter((f) => {
		const set = String(f.value ?? "").trim() !== "";
		return isUpdate.value ? set || f.changed : set;
	})
);
const tables = computed(() => (props.model.tables || []).filter((t) => (t.rows || []).length));

// Resizable width - drag the left (inner) edge. Mirrors Resizer.vue's behaviour:
// clamp to [min, max], snap to the default within +/-10px, persist to localStorage.
const DEFAULT_WIDTH = 720;
const MIN_WIDTH = 440;
const STORAGE_KEY = "jarvis-draftpreview-width";
const maxWidth = () => Math.min(1100, Math.round(window.innerWidth * 0.92));
const clampWidth = (w) => Math.min(maxWidth(), Math.max(MIN_WIDTH, w));

const stored = Number(localStorage.getItem(STORAGE_KEY));
const panelWidth = ref(clampWidth(stored || DEFAULT_WIDTH));
const resizing = ref(false);
const panelEl = ref(null);
let panelRight = 0;

function onMove(e) {
	resizing.value = true;
	let w = panelRight - e.clientX;
	if (w > DEFAULT_WIDTH - 10 && w < DEFAULT_WIDTH + 10) w = DEFAULT_WIDTH;
	panelWidth.value = clampWidth(w);
}
function onUp() {
	document.body.classList.remove("dp-resizing-body");
	localStorage.setItem(STORAGE_KEY, String(panelWidth.value));
	resizing.value = false;
	document.removeEventListener("mousemove", onMove);
	document.removeEventListener("mouseup", onUp);
}
function startResize() {
	// capture the panel's right edge so the width is correct regardless of any
	// horizontal offset of the overlay from the window edge
	panelRight = panelEl.value ? panelEl.value.getBoundingClientRect().right : window.innerWidth;
	document.body.classList.add("dp-resizing-body");
	document.addEventListener("mousemove", onMove);
	document.addEventListener("mouseup", onUp);
}

// Backdrop-click dismiss must ignore the tail of an in-panel drag. Stretching
// the panel starts the mousedown on the resize handle (inside the panel) and
// releases the mouse over the dimmed backdrop, so the browser fires the `click`
// on their common ancestor — the overlay. A bare @click.self would read that
// drag-release as an outside-click and close the panel. Only close when the
// press ALSO started on the backdrop.
let pressOnBackdrop = false;
function onOverlayDown(e) {
	pressOnBackdrop = e.target === e.currentTarget;
}
function onOverlayClick() {
	if (pressOnBackdrop) emit("close");
}
onBeforeUnmount(() => {
	document.removeEventListener("mousemove", onMove);
	document.removeEventListener("mouseup", onUp);
});
</script>

<template>
	<transition name="dp-slide" appear>
		<div class="dp-overlay" @mousedown="onOverlayDown" @click.self="onOverlayClick">
			<aside
				ref="panelEl"
				class="dp-panel"
				:class="{ 'dp-panel-resizing': resizing }"
				:style="{ width: panelWidth + 'px' }"
				tabindex="-1"
			>
				<div
					class="dp-resizer"
					:class="{ 'dp-resizer-active': resizing }"
					title="Drag to resize"
					@mousedown.prevent="startResize"
				/>
				<div class="dp-head">
					<svg
						width="15"
						height="15"
						viewBox="0 0 24 24"
						fill="none"
						stroke="var(--text-3)"
						stroke-width="1.8"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<rect x="3" y="4" width="18" height="16" rx="2" />
						<path d="M3 10h18M9 4v16" />
					</svg>
					<span class="dp-title">{{ docTitle }}</span>
					<span class="dp-badge" :class="{ 'dp-badge-upd': isUpdate }">{{ badge }}</span>
					<button
						class="dp-close"
						@click="emit('close')"
						title="Close"
						aria-label="Close"
					>
						<svg
							width="17"
							height="17"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.9"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M18 6 6 18M6 6l12 12" />
						</svg>
					</button>
				</div>
				<div class="dp-body">
					<div v-if="headline" class="dp-headline">{{ headline }}</div>
					<dl v-if="fields.length" class="dp-fields">
						<template v-for="f in fields" :key="f.fieldname">
							<dt>{{ f.label }}</dt>
							<dd :class="{ 'dp-changed': f.changed }">
								<template v-if="f.changed"
									><span class="dp-old">{{ f.orig || "(empty)" }}</span>
									<span class="dp-arrow">-&gt;</span>
									<span class="dp-new">{{
										f.value || "(empty)"
									}}</span></template
								>
								<template v-else>{{ f.value }}</template>
							</dd>
						</template>
					</dl>
					<div v-for="t in tables" :key="t.fieldname" class="dp-table">
						<div class="dp-table-title">{{ t.label }} ({{ t.rows.length }})</div>
						<div class="dp-gridwrap">
							<table class="dp-grid">
								<thead>
									<tr>
										<th v-for="c in t.columns" :key="c.fieldname">
											{{ c.label }}
										</th>
									</tr>
								</thead>
								<tbody>
									<tr v-for="(r, ri) in t.rows" :key="ri">
										<td v-for="c in t.columns" :key="c.fieldname">
											{{ r[c.fieldname] ?? "" }}
										</td>
									</tr>
								</tbody>
							</table>
						</div>
					</div>
					<div v-if="!fields.length && !tables.length" class="dp-empty">
						No values proposed yet.
					</div>
				</div>
				<div class="dp-foot">
					<button
						class="dp-btn dp-btn-primary"
						:disabled="disabled"
						@click="emit('confirm')"
					>
						{{ isUpdate ? "Confirm update" : "Confirm create" }}
					</button>
					<button class="dp-btn" @click="emit('edit')">Edit</button>
					<button class="dp-btn" @click="emit('close')">Close</button>
				</div>
			</aside>
		</div>
	</transition>
</template>

<style scoped>
.dp-overlay {
	position: absolute;
	inset: 0;
	z-index: 61;
	background: rgba(15, 15, 22, 0.32);
	display: flex;
	justify-content: flex-end;
}
.jv-dark .dp-overlay {
	background: rgba(0, 0, 0, 0.5);
}
.dp-panel {
	position: relative;
	max-width: 92vw;
	height: 100%;
	background: var(--surface);
	border-left: 1px solid var(--border);
	display: flex;
	flex-direction: column;
	box-shadow: -14px 0 44px rgba(20, 20, 30, 0.14);
}
.dp-panel:focus {
	outline: none;
}
/* Drag handle on the inner (left) edge; 1px rule that lights up on hover/drag. */
.dp-resizer {
	position: absolute;
	left: 0;
	top: 0;
	z-index: 3;
	height: 100%;
	width: 8px;
	margin-left: -4px;
	cursor: col-resize;
}
.dp-resizer::before {
	content: "";
	position: absolute;
	left: 4px;
	top: 0;
	height: 100%;
	width: 1px;
	background: var(--cta);
	opacity: 0;
	transition: opacity 0.15s ease;
}
.dp-resizer:hover::before,
.dp-resizer-active::before {
	opacity: 1;
}
.dp-panel-resizing {
	user-select: none;
}
:global(body.dp-resizing-body) {
	cursor: col-resize;
	user-select: none;
}
.dp-head {
	display: flex;
	align-items: center;
	gap: 9px;
	padding: 11px 12px 11px 14px;
	border-bottom: 1px solid var(--border);
	flex: none;
}
.dp-head svg {
	flex: none;
}
.dp-title {
	font-size: 14px;
	font-weight: 600;
	color: var(--text);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
	flex: 1;
	min-width: 0;
}
.dp-badge {
	font-size: 10px;
	font-weight: 650;
	letter-spacing: 0.08em;
	text-transform: uppercase;
	color: var(--amber);
	background: var(--amber-bg);
	border: 1px solid var(--amber-bd);
	border-radius: 99px;
	padding: 3px 9px;
	flex: none;
}
.dp-badge-upd {
	color: var(--cta);
	background: var(--cta-bg);
	border-color: var(--cta-bd);
}
.dp-close {
	background: none;
	border: none;
	color: var(--text-3);
	cursor: pointer;
	padding: 4px;
	border-radius: 6px;
	display: flex;
}
.dp-close:hover {
	background: var(--surface-2);
	color: var(--text);
}
.dp-body {
	flex: 1;
	overflow-y: auto;
	padding: 14px 16px;
	display: flex;
	flex-direction: column;
	gap: 14px;
}
.dp-headline {
	font-size: 13.5px;
	font-weight: 600;
	color: var(--text);
}
.dp-fields {
	display: grid;
	grid-template-columns: max-content 1fr;
	gap: 6px 16px;
	margin: 0;
}
.dp-fields dt {
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.06em;
	text-transform: uppercase;
	color: var(--text-3);
	align-self: center;
}
.dp-fields dd {
	margin: 0;
	font-size: 13.5px;
	color: var(--text);
}
.dp-changed .dp-old {
	color: var(--text-3);
	text-decoration: line-through;
}
.dp-changed .dp-arrow {
	color: var(--text-3);
}
.dp-changed .dp-new {
	color: var(--green);
	font-weight: 600;
}
.dp-table {
	border: 1px solid var(--border);
	border-radius: 8px;
	overflow: hidden;
}
.dp-table-title {
	padding: 7px 10px;
	background: var(--surface-2);
	font-size: 12px;
	font-weight: 650;
	color: var(--text-2);
}
.dp-gridwrap {
	overflow-x: auto;
}
.dp-grid {
	width: 100%;
	border-collapse: collapse;
	font-size: 12.5px;
}
.dp-grid th {
	text-align: left;
	padding: 6px 10px;
	color: var(--text-3);
	font-weight: 600;
	border-bottom: 1px solid var(--border);
	white-space: nowrap;
}
.dp-grid td {
	padding: 5px 10px;
	color: var(--text);
	border-bottom: 1px solid var(--border);
	white-space: nowrap;
}
.dp-grid tbody tr:last-child td {
	border-bottom: none;
}
.dp-empty {
	font-size: 12.5px;
	color: var(--text-3);
}
.dp-foot {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 11px 14px;
	border-top: 1px solid var(--border);
	background: var(--surface-1);
	flex: none;
}
.dp-btn {
	padding: 8px 14px;
	border-radius: 8px;
	font-size: 13px;
	font-weight: 600;
	border: 1px solid var(--border);
	background: var(--surface);
	color: var(--text);
	cursor: pointer;
}
.dp-btn:hover {
	background: var(--surface-2);
}
.dp-btn-primary {
	background: var(--cta);
	border-color: var(--cta);
	color: var(--cta-fg);
}
.dp-btn-primary:hover {
	filter: brightness(1.05);
}
.dp-btn-primary:disabled {
	opacity: 0.55;
	cursor: not-allowed;
}
.dp-slide-enter-active,
.dp-slide-leave-active {
	transition: opacity 0.18s ease;
}
.dp-slide-enter-active .dp-panel,
.dp-slide-leave-active .dp-panel {
	transition: transform 0.22s cubic-bezier(0.4, 0, 0.2, 1);
}
.dp-slide-enter-from,
.dp-slide-leave-to {
	opacity: 0;
}
.dp-slide-enter-from .dp-panel,
.dp-slide-leave-to .dp-panel {
	transform: translateX(100%);
}
</style>
