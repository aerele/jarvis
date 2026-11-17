<script setup>
// The full-screen sheet of a server draft card: the edit form (header, the shared form, a sticky
// footer) or the shared preview (its own header, body and footer: Create, Edit, Close). Everything
// it shows or does comes from `host` (lib/draftSheetHost.js makeDraftSheet); this file is the
// markup and the phone's look.
// The sheet is the phone's own Sheet (focus trap, Escape, scroll lock) at full height. The wrapper
// carries `jv-draft-tokens` so the shared components read the phone's colours (index.css); the
// teleported link menu takes the same values as its `palette`.
import { computed, nextTick, ref, watch } from "vue";
import DraftEditForm from "@shared/components/chat/DraftEditForm.vue";
import DraftPreview from "@shared/components/doc/DraftPreview.vue";
import { toolPrimaryLabel } from "@shared/lib/toolDraftPanel.js";
import { toolDeadText } from "@shared/lib/toolDraftHost.js";
import { draftPalette } from "../lib/draftTokens.js";
import { bannerVisible, errorLineOf, footerStatus, sheetTitle } from "../lib/draftSheetView.js";
import { scrollFocusedIntoView } from "../lib/viewportFit.js";
import Sheet from "./Sheet.vue";

const props = defineProps({ host: { type: Object, required: true } });
// The host's state, unwrapped by the template.
const {
	open,
	panel,
	preview,
	previewNote,
	previewOff,
	confirming,
	confirmingDiscard,
	dead,
	notices,
	footer,
	link,
	bodyEl,
	setBody,
} = props.host;
const { draftLink, draftLinkError, onDraftLink, pickDraftLink, closeDraftLink } = link;
const palette = draftPalette();

const keepButton = ref(null);
const keepDraftButton = ref(null);
const title = computed(() => sheetTitle(panel.value));
const errorLine = computed(() => errorLineOf(panel.value && panel.value.error));
const label = computed(() =>
	preview.value ? "Preview of the draft" : panel.value ? "Edit draft" : "Draft"
);
// The question shows in the footer: its safe answer takes focus.
watch(confirming, async (asking) => {
	if (!asking) return;
	await nextTick();
	if (keepButton.value) keepButton.value.focus();
});
// the footer's one line: a failed Save or a dead card, said where the thumb is
const status = computed(() =>
	footerStatus({
		errorLine: errorLine.value,
		dead: dead.value,
		deadText: toolDeadText(panel.value),
		marked: !!(panel.value && panel.value.error && (panel.value.error.fields || []).length),
	})
);
// The banner at the top of the body and the footer's line say the same thing: the footer says it only
// while the banner is out of sight (scrolled away). Measured after the message changes and whenever
// the body scrolls.
const bannerSeen = ref(false);
function measureBanner() {
	const area = bodyEl.value;
	const banner = area && area.querySelector ? area.querySelector(".jv-dsheet-notes") : null;
	bannerSeen.value =
		!!banner && bannerVisible(banner.getBoundingClientRect(), area.getBoundingClientRect());
}
watch(
	() => [notices.value.map((n) => n.text).join("|"), errorLine.value && errorLine.value.message],
	async () => {
		await nextTick();
		measureBanner();
	},
	{ flush: "post" }
);
watch(confirmingDiscard, async (asking) => {
	if (!asking) return;
	await nextTick();
	if (keepDraftButton.value) keepDraftButton.value.focus();
});
// A field focused behind the virtual keyboard is scrolled into view.
const onFocusIn = (event) => scrollFocusedIntoView(event.target);
</script>

<template>
	<Sheet :open="open" full :label="label" @close="host.requestClose()">
		<div class="jv-dsheet jv-draft-tokens" @focusin="onFocusIn">
			<!-- The shared preview, drawn without its side-panel chrome. Create is the card's Create. -->
			<p v-if="previewNote && !panel" class="jv-dsheet-note is-error" role="alert">
				{{ previewNote }}
			</p>
			<DraftPreview
				v-if="preview && !panel"
				surface="phone"
				:model="preview.model"
				:confirm-label="toolPrimaryLabel(preview.model)"
				:disabled="previewOff"
				@close="host.requestClose()"
				@edit="host.editFromPreview()"
				@confirm="host.createFromPreview()"
			/>
			<template v-else-if="panel">
				<header class="jv-dsheet-head">
					<div class="jv-dsheet-titles">
						<h2 class="jv-dsheet-title">{{ title }}</h2>
						<span class="jv-dsheet-badge">Draft - not saved</span>
					</div>
					<button
						type="button"
						class="jv-dsheet-close"
						aria-label="Close"
						@click="host.requestClose()"
					>
						<svg
							viewBox="0 0 24 24"
							width="20"
							height="20"
							fill="none"
							stroke="currentColor"
							stroke-width="1.9"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
						>
							<path d="M18 6 6 18M6 6l12 12" />
						</svg>
					</button>
				</header>
				<div :ref="setBody" class="jv-dsheet-body" @scroll.capture="measureBanner">
					<DraftEditForm
						surface="phone"
						:model="panel"
						:draft-link="draftLink"
						:palette="palette"
						:link-error="draftLinkError"
						@link-search="onDraftLink"
						@link-blur="closeDraftLink"
						@link-pick="pickDraftLink"
						@link-close="host.closeLinkMenu()"
					>
						<template #top>
							<div v-if="notices.length || errorLine" class="jv-dsheet-notes">
								<p
									v-for="n in notices"
									:key="n.text"
									class="jv-dsheet-note"
									:class="n.type === 'error' ? 'is-error' : 'is-warn'"
									:role="n.type === 'error' ? 'alert' : 'status'"
								>
									{{ n.text }}
								</p>
								<p v-if="errorLine" class="jv-dsheet-note is-error" role="alert">
									{{ errorLine.message }}
									<span v-if="errorLine.hint" class="jv-dsheet-hint">{{
										errorLine.hint
									}}</span>
								</p>
							</div>
						</template>
					</DraftEditForm>
				</div>
				<footer class="jv-dsheet-foot">
					<div
						v-if="confirming"
						class="jv-dsheet-ask"
						role="group"
						aria-label="Discard your changes?"
					>
						<p class="jv-dsheet-ask-text">Discard your changes?</p>
						<div class="jv-dsheet-actions">
							<button
								ref="keepButton"
								type="button"
								class="jv-btn is-primary"
								@click="host.keepEditing()"
							>
								Keep editing
							</button>
							<button
								type="button"
								class="jv-btn is-ghost"
								@click="host.discardChanges()"
							>
								Discard changes
							</button>
						</div>
					</div>
					<div
						v-else-if="confirmingDiscard"
						class="jv-dsheet-ask"
						role="group"
						aria-label="Discard this draft?"
					>
						<p class="jv-dsheet-ask-text">Discard this draft?</p>
						<div class="jv-dsheet-actions">
							<button
								ref="keepDraftButton"
								type="button"
								class="jv-btn is-primary"
								@click="host.keepDraft()"
							>
								Keep draft
							</button>
							<button
								type="button"
								class="jv-btn is-ghost"
								@click="host.discardDraft()"
							>
								Discard draft
							</button>
						</div>
					</div>
					<template v-else>
						<p v-if="footer.primaryTitle" class="jv-dsheet-wait" role="status">
							{{ footer.primaryTitle }}
						</p>
						<p
							v-else-if="status && !bannerSeen"
							class="jv-dsheet-wait is-error"
							role="status"
						>
							{{ status.text }}
						</p>
						<div class="jv-dsheet-actions">
							<button
								type="button"
								class="jv-btn is-primary"
								:disabled="footer.primaryDisabled"
								@click="host.save()"
							>
								{{ footer.primaryLabel }}
							</button>
							<button
								type="button"
								class="jv-btn is-ghost"
								:disabled="footer.discardDisabled"
								@click="host.askDiscardDraft()"
							>
								Discard draft
							</button>
						</div>
					</template>
				</footer>
			</template>
		</div>
	</Sheet>
</template>

<style scoped>
.jv-dsheet {
	flex: 1;
	min-height: 0;
	display: flex;
	flex-direction: column;
	background: var(--card);
	color: var(--ink9);
}
.jv-dsheet-head {
	flex: none;
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 6px 6px 6px 16px;
	border-bottom: 1px solid var(--border);
}
.jv-dsheet-titles {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 4px 10px;
}
.jv-dsheet-title {
	margin: 0;
	font-size: 16px;
	font-weight: 600;
	color: var(--ink9);
	overflow-wrap: anywhere;
}
.jv-dsheet-badge {
	flex: none;
	padding: 2px 8px;
	border: 1px solid var(--amber-bd);
	border-radius: 999px;
	background: var(--amber-bg);
	color: var(--amber);
	font-size: 11px;
	font-weight: 650;
	letter-spacing: 0.04em;
}
.jv-dsheet-close {
	flex: none;
	display: grid;
	place-items: center;
	width: 44px;
	height: 44px;
	border: 0;
	border-radius: 12px;
	background: none;
	color: var(--ink7);
	cursor: pointer;
}
/* The form's body scrolls inside the sheet; the header and the footer stay. */
.jv-dsheet-body {
	flex: 1;
	min-height: 0;
	display: flex;
	flex-direction: column;
}
.jv-dsheet-notes {
	display: flex;
	flex-direction: column;
	gap: 8px;
}
.jv-dsheet-note {
	margin: 0;
	padding: 10px 12px;
	border-radius: 10px;
	font-size: 14px;
	line-height: 1.4;
	overflow-wrap: anywhere;
}
.jv-dsheet-note.is-error {
	background: var(--red-bg);
	color: var(--red);
}
.jv-dsheet-note.is-warn {
	background: var(--amber-bg);
	color: var(--amber);
}
.jv-dsheet-hint {
	display: block;
	margin-top: 4px;
	font-size: 13px;
}
.jv-dsheet-foot {
	flex: none;
	padding: 10px 12px;
	border-top: 1px solid var(--border);
	background: var(--card);
}
.jv-dsheet-wait {
	margin: 0 0 8px;
	font-size: 13px;
	color: var(--ink7);
}
.jv-dsheet-wait.is-error {
	color: var(--red);
}
.jv-dsheet-ask-text {
	margin: 0 0 8px;
	font-size: 15px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-dsheet-actions {
	display: flex;
	gap: 8px;
}
.jv-btn {
	flex: 1 1 0;
	display: grid;
	place-items: center;
	min-height: 44px;
	padding: 0 12px;
	border: 0;
	border-radius: 10px;
	font: inherit;
	font-size: 15px;
	font-weight: 600;
	cursor: pointer;
}
/* the same fill as the Preview's primary (the draft token map's --cta pair), not white on the accent */
.jv-btn.is-primary {
	background: var(--cta);
	color: var(--cta-fg);
}
.jv-btn.is-ghost {
	border: 1px solid var(--border2);
	background: var(--card);
	color: var(--ink8);
}
.jv-btn:disabled {
	opacity: 0.6;
}
</style>
