<template>
	<!-- Mirrors BrandingPane: the SettingsPane frame, an admin-gated write, the
	     shared api client. Everyone sees the gallery (the built-in looks, then the
	     workspace's own templates, each drawn as a small page thumbnail so the
	     looks are tellable apart before opening one). Admins also get the editor
	     below it: grouped fields on the left, the live preview pinned on the right.
	     The workspace default is marked with a filled star; every other enabled
	     template carries an outline star that sets it, so "make default" is one
	     click from the gallery and never depends on the editor's state. -->
	<SettingsPane
		title="PDF Templates"
		description="The look Jarvis applies to the PDFs it generates. Pick the workspace default here or build your own; anyone can reformat a single document from its download card."
		:error="error"
	>
		<template v-if="isAdmin && loaded" #actions>
			<Button variant="solid" iconLeft="plus" label="New template" @click="newTemplate" />
		</template>

		<div v-if="loading" class="text-p-sm text-ink-gray-6">Loading…</div>

		<template v-else>
			<!-- ═══════════ gallery: built in, then the workspace's own ═══════════ -->
			<section v-for="group in galleryGroups" :key="group.key" class="mb-7">
				<div class="mb-2.5 flex items-baseline justify-between gap-3">
					<h3 class="text-sm font-medium text-ink-gray-8">{{ group.title }}</h3>
					<span class="text-p-xs text-ink-gray-5">{{ group.hint }}</span>
				</div>

				<div class="grid grid-cols-3 gap-3 sm:grid-cols-5">
					<div
						v-for="t in group.items"
						:key="t.key"
						class="group relative flex flex-col gap-2 rounded-lg border p-2 text-left outline-none transition-colors"
						:class="cardClass(t)"
						:role="isAdmin ? 'button' : undefined"
						:tabindex="isAdmin ? 0 : undefined"
						:aria-pressed="isAdmin ? isCurrentRow(t) : undefined"
						@click="isAdmin && selectTemplate(t)"
						@keydown.enter.prevent="isAdmin && selectTemplate(t)"
						@keydown.space.prevent="isAdmin && selectTemplate(t)"
					>
						<!-- Page thumbnail, drawn from the summary spec alone: the title
						     glyph in the accent + title face, aligned like the masthead,
						     a dark panel when the look opens on a cover page. -->
						<div
							class="relative aspect-[3/4] w-full overflow-hidden rounded-sm border border-outline-gray-2 bg-white"
							:class="t.enabled ? '' : 'opacity-40'"
							aria-hidden="true"
						>
							<template v-if="t.cover === 'on'">
								<div
									class="absolute inset-x-0 top-0 flex h-[46%] items-center justify-center"
									:style="coverStyle(t)"
								>
									<span
										class="text-[13px] font-bold leading-none text-white"
										:style="{ fontFamily: fontStack(t.display_font) }"
									>
										Aa
									</span>
								</div>
								<div class="absolute inset-x-2 top-[56%] flex flex-col gap-[3px]">
									<span
										v-for="(w, i) in BODY_LINES"
										:key="i"
										class="h-[3px] rounded-sm bg-[#dcdcdc]"
										:class="w"
									/>
								</div>
							</template>
							<template v-else>
								<div
									class="absolute inset-x-2 top-2 flex flex-col gap-1"
									:class="
										t.masthead === 'center' ? 'items-center' : 'items-start'
									"
								>
									<span
										class="text-[14px] font-bold leading-none"
										:style="{
											color: t.accent,
											fontFamily: fontStack(t.display_font),
										}"
									>
										Aa
									</span>
									<span class="h-px w-full" :style="{ background: t.accent }" />
								</div>
								<div
									class="absolute inset-x-2 top-[42%] flex flex-col gap-[3px]"
									:class="
										t.masthead === 'center' ? 'items-center' : 'items-start'
									"
								>
									<span
										v-for="(w, i) in BODY_LINES"
										:key="i"
										class="h-[3px] rounded-sm bg-[#dcdcdc]"
										:class="w"
									/>
								</div>
							</template>
						</div>

						<div class="flex min-w-0 items-start justify-between gap-1">
							<div class="min-w-0">
								<div class="truncate text-p-sm font-medium text-ink-gray-9">
									{{ t.label }}
								</div>
								<div class="truncate text-p-xs text-ink-gray-5">
									{{ specLine(t) }}
								</div>
							</div>

							<!-- The default wears a filled star; any other enabled template
							     offers an outline star that makes it the default. Gated on
							     the row's SAVED enabled flag, which is what the server's
							     enabled-only lookup checks, not the editor's checkbox. -->
							<span
								v-if="t.key === defaultKey"
								class="flex size-6 shrink-0 items-center justify-center text-ink-gray-9"
								title="Workspace default"
							>
								<FeatherIcon name="star" class="size-4 fill-current" />
								<span class="sr-only">Workspace default</span>
							</span>
							<Tooltip v-else-if="isAdmin && t.enabled" text="Make default">
								<button
									type="button"
									class="flex size-6 shrink-0 items-center justify-center rounded text-ink-gray-4 transition-colors hover:bg-surface-gray-3 hover:text-ink-gray-8 focus-visible:ring-2 focus-visible:ring-outline-gray-3"
									:disabled="settingDefault"
									:aria-label="`Make ${t.label} the default`"
									@click.stop="setDefault(t.key)"
									@keydown.stop
								>
									<FeatherIcon name="star" class="size-4" />
								</button>
							</Tooltip>
							<Badge
								v-else-if="!t.enabled"
								label="Disabled"
								theme="orange"
								variant="subtle"
								size="sm"
							/>
						</div>
					</div>

					<!-- Admins: the "new" tile closes the custom row, so an empty
					     workspace still shows exactly where its first template goes. -->
					<button
						v-if="group.key === 'custom' && isAdmin"
						type="button"
						class="flex min-h-[132px] flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed p-2 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-outline-gray-3"
						:class="
							isCreating
								? 'border-outline-gray-5 bg-surface-gray-2 text-ink-gray-9'
								: 'border-outline-gray-3 text-ink-gray-6 hover:bg-surface-gray-1 hover:text-ink-gray-8'
						"
						@click="newTemplate"
					>
						<FeatherIcon name="plus" class="size-5" />
						<span class="text-p-sm">New template</span>
					</button>
				</div>

				<p
					v-if="group.key === 'custom' && !group.items.length && !isAdmin"
					class="text-p-sm text-ink-gray-5"
				>
					No custom templates yet.
				</p>
			</section>

			<p v-if="!isAdmin" class="text-p-xs text-ink-gray-5">
				Only an admin can change the workspace default or build custom templates. You can
				still reformat any single document from its download card.
			</p>

			<!-- ═══════════ admin editor: fields left, live preview right ═══════════ -->
			<section
				v-else-if="isCreating || selected"
				class="border-t border-outline-gray-2 pt-6"
				aria-labelledby="pdf-template-editor-title"
			>
				<div class="flex flex-wrap items-start justify-between gap-3">
					<div class="flex min-w-0 flex-col gap-1">
						<div class="flex flex-wrap items-center gap-2">
							<h3
								id="pdf-template-editor-title"
								class="text-base font-semibold text-ink-gray-9"
							>
								{{ isCreating ? "New template" : currentLabel }}
							</h3>
							<Badge
								v-if="selected && !selected.custom"
								label="Built in"
								theme="gray"
								variant="subtle"
								size="sm"
							/>
							<Badge
								v-if="isCurrentDefault"
								label="Default"
								theme="blue"
								variant="subtle"
								size="sm"
							/>
							<Badge
								v-if="selectedRowDisabled"
								label="Disabled"
								theme="orange"
								variant="subtle"
								size="sm"
							/>
						</div>
						<p v-if="editorHint" class="text-p-xs text-ink-gray-5">{{ editorHint }}</p>
					</div>
					<div class="flex flex-wrap items-center gap-2">
						<Button
							v-if="isEditableCustom && !isCreating"
							variant="subtle"
							theme="red"
							label="Delete"
							:loading="deleting"
							@click="confirmDeleteCurrent"
						/>
						<Button
							v-if="!isCurrentDefault"
							variant="subtle"
							iconLeft="star"
							label="Make default"
							:disabled="!canMakeDefault"
							:loading="settingDefault"
							@click="selected && setDefault(selected.key)"
						/>
						<Button
							v-if="isEditableCustom"
							variant="solid"
							label="Save"
							:loading="saving"
							:disabled="!dirty"
							@click="save"
						/>
					</div>
				</div>

				<div
					class="mt-5 grid grid-cols-1 gap-6"
					:class="previewLarge ? '' : 'sm:grid-cols-[minmax(0,1fr)_280px]'"
				>
					<!-- ── left: spec sheet (built in) or the grouped form (custom) ── -->
					<div class="min-w-0">
						<template v-if="selected && !selected.custom">
							<p class="text-p-sm text-ink-gray-6">
								Built-in looks can't be edited. Start a new template to change the
								colours, type or layout.
							</p>
							<div
								class="mt-3 divide-y divide-outline-gray-1 border-y border-outline-gray-2"
							>
								<KvRow label="Accent">
									<span class="inline-flex items-center gap-2">
										<span
											class="size-3 rounded-full border border-outline-gray-2"
											:style="{ background: selected.accent }"
										/>
										{{ selected.accent }}
									</span>
								</KvRow>
								<KvRow label="Body text" :value="fontLabel(selected.body_font)" />
								<KvRow
									label="Document title"
									:value="fontLabel(selected.display_font)"
								/>
								<KvRow
									label="Masthead"
									:value="
										selected.masthead === 'center'
											? 'Centered'
											: 'Left aligned'
									"
								/>
								<KvRow label="Cover page" :value="coverLabel(selected.cover)" />
							</div>
						</template>

						<div v-else-if="formLoading" class="text-p-sm text-ink-gray-6">
							Loading template…
						</div>

						<template v-else-if="form">
							<!-- Identity -->
							<section class="pb-5">
								<div class="mb-3">
									<h4 class="text-sm font-medium text-ink-gray-8">Identity</h4>
									<p class="text-p-xs text-ink-gray-5">
										How the template is named in the gallery and on download
										cards.
									</p>
								</div>
								<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
									<FormControl
										v-if="isCreating"
										type="text"
										label="Template key"
										v-model="form.template_key"
										placeholder="acme-invoice"
										description="Lowercase letters, digits and hyphens. Fixed after saving."
									/>
									<div v-else class="flex flex-col gap-1.5">
										<span class="text-xs text-ink-gray-5">Template key</span>
										<span class="text-p-sm text-ink-gray-7">{{
											form.template_key
										}}</span>
									</div>
									<FormControl
										type="text"
										label="Label"
										placeholder="Acme invoice"
										v-model="form.label"
									/>
									<FormControl
										type="textarea"
										label="Description"
										:rows="2"
										class="sm:col-span-2"
										v-model="form.description"
									/>
									<div
										class="flex items-center justify-between gap-4 sm:col-span-2"
									>
										<div class="flex flex-col">
											<span class="text-p-sm font-medium text-ink-gray-8"
												>Enabled</span
											>
											<span class="text-p-xs text-ink-gray-5">
												Listed for everyone and available as the workspace
												default.
											</span>
										</div>
										<Switch v-model="form.enabled" />
									</div>
								</div>
							</section>

							<!-- Typography -->
							<section class="border-t border-outline-gray-2 py-5">
								<div class="mb-3">
									<h4 class="text-sm font-medium text-ink-gray-8">Typography</h4>
									<p class="text-p-xs text-ink-gray-5">
										Serif reads as print and formal, sans as modern and plain.
										The title face applies to the document title; headings
										follow the body face.
									</p>
								</div>
								<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
									<div class="flex flex-col gap-1.5">
										<span class="text-xs text-ink-gray-5">Body text</span>
										<FormControl
											type="select"
											:options="fontSelectOptions"
											:model-value="form.body_font"
											@update:model-value="
												(v) => (form.body_font = v || 'sans')
											"
										/>
									</div>
									<div class="flex flex-col gap-1.5">
										<span class="text-xs text-ink-gray-5">Document title</span>
										<FormControl
											type="select"
											:options="fontSelectOptions"
											:model-value="form.display_font"
											@update:model-value="
												(v) => (form.display_font = v || 'sans')
											"
										/>
									</div>
									<!-- Instant specimen: type + colour without a round trip.
									     The iframe preview remains the faithful render. -->
									<div
										class="rounded-lg border border-outline-gray-2 bg-white px-4 py-3 sm:col-span-2"
										aria-hidden="true"
									>
										<div
											class="text-[19px] font-bold leading-tight"
											:style="{
												fontFamily: fontStack(form.display_font),
												color: form.dark_color || DEFAULT_DARK,
											}"
										>
											Quarterly review
										</div>
										<div
											class="mb-2 mt-1.5 h-px"
											:style="{
												background: form.accent_color || FALLBACK_SWATCH,
											}"
										/>
										<p
											class="text-[12px] leading-relaxed text-[#1a1a1a]"
											:style="{ fontFamily: fontStack(form.body_font) }"
										>
											Revenue grew 12% quarter on quarter, led by
											subscriptions. Figures like 1,234.56 sit in the body
											face and
											<span
												class="underline"
												:style="{
													color: form.accent_color || FALLBACK_SWATCH,
												}"
												>links</span
											>
											take the accent.
										</p>
									</div>
								</div>
							</section>

							<!-- Colour -->
							<section class="border-t border-outline-gray-2 py-5">
								<div class="mb-3">
									<h4 class="text-sm font-medium text-ink-gray-8">Colour</h4>
									<p class="text-p-xs text-ink-gray-5">
										Accent colours rules, links, chart bars and the accent bar.
										Dark colours headings and the cover page.
									</p>
								</div>
								<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
									<div class="flex flex-col gap-1.5">
										<span class="text-xs text-ink-gray-5">Accent</span>
										<div class="flex items-center gap-2">
											<input
												type="color"
												class="size-8 shrink-0 cursor-pointer rounded border border-outline-gray-2 bg-transparent p-0"
												aria-label="Pick the accent colour"
												:value="toColorInputValue(form.accent_color)"
												@input="form.accent_color = $event.target.value"
											/>
											<FormControl
												type="text"
												class="flex-1"
												placeholder="#1f4e79"
												v-model="form.accent_color"
											/>
										</div>
									</div>
									<div class="flex flex-col gap-1.5">
										<span class="text-xs text-ink-gray-5">Dark</span>
										<div class="flex items-center gap-2">
											<input
												type="color"
												class="size-8 shrink-0 cursor-pointer rounded border border-outline-gray-2 bg-transparent p-0"
												aria-label="Pick the dark colour"
												:value="
													toColorInputValue(
														form.dark_color,
														DEFAULT_DARK
													)
												"
												@input="form.dark_color = $event.target.value"
											/>
											<FormControl
												type="text"
												class="flex-1"
												placeholder="#132d47"
												v-model="form.dark_color"
											/>
										</div>
									</div>
								</div>
							</section>

							<!-- Layout -->
							<section class="border-t border-outline-gray-2 py-5">
								<div class="mb-3">
									<h4 class="text-sm font-medium text-ink-gray-8">Layout</h4>
									<p class="text-p-xs text-ink-gray-5">
										The masthead, the cover page and what repeats on every
										page.
									</p>
								</div>
								<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
									<div class="flex flex-col gap-1.5">
										<span class="text-xs text-ink-gray-5">Masthead</span>
										<TabButtons
											:buttons="MASTHEAD_OPTIONS"
											:model-value="form.masthead_align"
											@update:model-value="
												(v) => (form.masthead_align = v || 'left')
											"
										/>
									</div>
									<div class="flex flex-col gap-1.5">
										<span class="text-xs text-ink-gray-5">Cover page</span>
										<TabButtons
											:buttons="COVER_OPTIONS"
											:model-value="form.cover"
											@update:model-value="(v) => (form.cover = v || 'auto')"
										/>
										<span class="text-p-xs text-ink-gray-5">
											Auto adds a cover only to long documents.
										</span>
									</div>
									<FormControl
										type="text"
										label="Watermark"
										class="sm:col-span-2"
										placeholder="e.g. DRAFT"
										description="Repeats diagonally across every page. Leave blank for none."
										v-model="form.watermark"
									/>
									<div class="flex flex-col gap-2.5 sm:col-span-2">
										<Checkbox
											label="Show the company logo in the masthead"
											v-model="form.show_logo"
										/>
										<Checkbox
											label="Accent bar across the top of the first page"
											v-model="form.accent_bar"
										/>
									</div>
								</div>
							</section>

							<!-- Page -->
							<section class="border-t border-outline-gray-2 py-5">
								<div class="mb-3">
									<h4 class="text-sm font-medium text-ink-gray-8">Page</h4>
									<p class="text-p-xs text-ink-gray-5">
										Applies to the exported PDF. The preview always shows A4
										portrait.
									</p>
								</div>
								<div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
									<FormControl
										type="select"
										label="Size"
										:options="PAGE_SIZE_OPTIONS"
										v-model="form.page_size"
									/>
									<FormControl
										type="select"
										label="Orientation"
										:options="ORIENTATION_OPTIONS"
										v-model="form.orientation"
									/>
									<FormControl
										type="number"
										label="Margins (mm)"
										:min="MARGIN_MIN"
										:max="MARGIN_MAX"
										:step="1"
										description="8 to 40"
										v-model.number="form.margins_mm"
									/>
								</div>
							</section>

							<!-- Footer -->
							<section class="border-t border-outline-gray-2 py-5">
								<div class="mb-3 flex items-start justify-between gap-4">
									<div>
										<h4 class="text-sm font-medium text-ink-gray-8">Footer</h4>
										<p class="text-p-xs text-ink-gray-5">
											Print a company's Letter Head footer on every page,
											chosen per company.
										</p>
									</div>
									<Switch v-model="form.use_letterhead_footer" />
								</div>
								<div v-if="form.use_letterhead_footer" class="flex flex-col gap-2">
									<div
										v-for="(row, i) in form.company_letter_heads"
										:key="i"
										class="flex items-center gap-2"
									>
										<FormControl
											type="select"
											class="flex-1"
											:options="companyOptions"
											v-model="row.company"
										/>
										<FormControl
											type="select"
											class="flex-1"
											:options="letterHeadOptions"
											v-model="row.letter_head"
										/>
										<Button
											variant="ghost"
											theme="red"
											icon="x"
											:tooltip="'Remove'"
											@click="removeLetterheadRow(i)"
										/>
									</div>
									<Button
										variant="subtle"
										size="sm"
										iconLeft="plus"
										label="Add a company"
										class="self-start"
										@click="addLetterheadRow"
									/>
									<p
										v-if="!companies.length || !letterHeads.length"
										class="text-p-xs text-ink-gray-5"
									>
										Loading companies and letter heads…
									</p>
								</div>
							</section>
						</template>
					</div>

					<!-- ── right: live preview, pinned while the form scrolls ── -->
					<div class="min-w-0">
						<div :class="previewLarge ? '' : 'sm:sticky sm:top-0'">
							<div class="mb-1.5 flex items-center justify-between">
								<span class="text-xs text-ink-gray-5">
									Preview<template v-if="previewLoading && previewHtml"
										>, updating</template
									>
								</span>
								<Button
									variant="ghost"
									size="sm"
									:icon="previewLarge ? 'minimize-2' : 'maximize-2'"
									:tooltip="
										previewLarge ? 'Fit beside the form' : 'Show at full size'
									"
									@click="previewLarge = !previewLarge"
								/>
							</div>
							<!-- Deliberately its own error surface, next to the iframe it
							     explains, rather than SettingsPane's shared bottom slot: a bad
							     css value while editing is local to this panel. -->
							<ErrorMessage v-if="previewError" :message="previewError" />
							<div
								v-if="previewLoading && !previewHtml"
								class="grid place-items-center rounded-lg border border-outline-gray-2 text-p-sm text-ink-gray-5"
								:class="
									previewLarge
										? 'h-[560px] w-full'
										: 'h-[396px] w-full max-w-[280px]'
								"
							>
								Rendering preview…
							</div>
							<!-- Fit mode renders the page at 560px and scales the iframe to
							     half, so the preview reads as a page thumbnail with the true
							     measure and proportions; full size is the same iframe unscaled.
							     sandbox="" stays exactly as is (no scripts, no same-origin). -->
							<div
								v-else-if="previewHtml"
								class="overflow-hidden rounded-lg border border-outline-gray-2 bg-white"
								:class="
									previewLarge
										? 'h-[560px] w-full'
										: 'h-[396px] w-full max-w-[280px]'
								"
							>
								<iframe
									sandbox=""
									:srcdoc="previewHtml"
									title="Template preview"
									class="bg-white"
									:class="
										previewLarge
											? 'h-full w-full'
											: 'h-[792px] w-[560px] origin-top-left scale-50'
									"
								/>
							</div>
						</div>
					</div>
				</div>
			</section>
		</template>
	</SettingsPane>
</template>

<script setup>
// Admin CRUD + live preview for custom PDF templates, alongside the predefined
// set. The gallery (list/preview via `key`) and the custom editor (list/preview
// via `draft`, since a saved-but-DISABLED custom template is invisible to
// preview_pdf_template's enabled-only DB read - key-mode would silently fall
// back to classic mid-edit) share one selection model: `selected` names
// whichever template summary is on screen, `form` holds the editable copy for a
// custom one (null while a predefined template is selected, since there is
// nothing to edit).
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
	Badge,
	Button,
	Checkbox,
	ErrorMessage,
	FeatherIcon,
	FormControl,
	Switch,
	TabButtons,
	Tooltip,
	confirmDialog,
	toast,
} from "frappe-ui";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import KvRow from "@/components/settings/KvRow.vue";
import {
	deletePdfTemplate,
	getPdfTemplate,
	listPdfTemplates,
	pdfTemplateOptions,
	previewPdfTemplate,
	savePdfTemplate,
	setDefaultPdfTemplate,
} from "@/api";
import { errHtml, escapeHtml } from "@/lib/errors";

const MASTHEAD_OPTIONS = [
	{ label: "Left", value: "left" },
	{ label: "Centered", value: "center" },
];
const COVER_OPTIONS = [
	{ label: "Auto", value: "auto" },
	{ label: "On", value: "on" },
	{ label: "Off", value: "off" },
];
// Mirrors the Jarvis PDF Template doctype's Select options and the 8..40 mm
// margin rule its controller enforces; export_document accepts more sizes, but
// a custom template only offers what the doctype stores.
const PAGE_SIZE_OPTIONS = [
	{ label: "A4", value: "A4" },
	{ label: "Letter", value: "Letter" },
	{ label: "Legal", value: "Legal" },
];
const ORIENTATION_OPTIONS = [
	{ label: "Portrait", value: "portrait" },
	{ label: "Landscape", value: "landscape" },
];
const MARGIN_MIN = 8;
const MARGIN_MAX = 40;
const MARGIN_DEFAULT = 15;
// The classic primary / dark, used for the empty-colour swatch and the specimen.
const FALLBACK_SWATCH = "#1f4e79";
const DEFAULT_DARK = "#132d47";
// Screen stand-ins for theme.py's server-safe stacks, for the thumbnails and the
// typography specimen only; the iframe preview uses the real stacks.
const UI_FONT_STACKS = {
	sans: '"Helvetica Neue", Helvetica, Arial, sans-serif',
	serif: 'Georgia, "Times New Roman", serif',
};
// Widths of the grey "text" lines in a page thumbnail; a ragged right edge
// reads as prose, a full block reads as a swatch.
const BODY_LINES = ["w-full", "w-11/12", "w-full", "w-4/5", "w-full", "w-2/3"];
// Backend's own slug rule (jarvis_pdf_template.py _SLUG) - checked client-side
// only so a bad key surfaces before the round trip; the doctype controller is
// the real gate.
const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const PREVIEW_DEBOUNCE_MS = 400;

// Server re-checks every write below; this only gates the affordance (same as
// BrandingPane / ConnectorsPane's isAdmin).
const isAdmin = !!window.is_jarvis_admin;

const loading = ref(true);
const loaded = ref(false);
const error = ref("");
const templates = ref([]);
const defaultKey = ref("classic");

// ── selection / editor state ────────────────────────────────────────────────
const selected = ref(null); // the gallery summary currently shown, or null
const isCreating = ref(false);
const form = ref(null); // editable copy for a custom template; null for a predefined one
const formSnapshot = ref(""); // JSON.stringify(form) at last load/save, for dirty tracking
const formLoading = ref(false);
const saving = ref(false);
const deleting = ref(false);
const settingDefault = ref(false);

// ── live preview state (declared here, ahead of doSelect/doNewTemplate/
// confirmDeleteCurrent/onBeforeUnmount below, all of which reference it) ─────
const previewHtml = ref("");
const previewError = ref("");
const previewLoading = ref(false);
const previewLarge = ref(false);
let previewSeq = 0;
let debounceTimer = null;

// Options carry a blank placeholder entry so a freshly-added row (company: "",
// letter_head: "") actually renders as "unset" - without it a native <select>
// whose value matches no option falls back to displaying the FIRST option, so
// the admin would see e.g. "Acme Corp" on screen while the model is still ""
// (NotesView's KIND_OPTIONS/STATUS_OPTIONS use the same leading-blank idiom).
const companies = ref([]);
const letterHeads = ref([]);
const companyOptions = computed(() => [
	{ label: "Choose a company", value: "" },
	...companies.value.map((c) => ({ label: c, value: c })),
]);
const letterHeadOptions = computed(() => [
	{ label: "Choose a letter head", value: "" },
	...letterHeads.value.map((l) => ({ label: l, value: l })),
]);

// Built-in font catalog (from pdf_template_options): each {key, label, category}.
// Drives the body/title font pickers; the iframe preview renders the real face.
const fonts = ref([]);
const fontSelectOptions = computed(() =>
	fonts.value.map((f) => ({ label: f.label, value: f.key }))
);
const fontMeta = computed(() => Object.fromEntries(fonts.value.map((f) => [f.key, f])));

// ── gallery ──────────────────────────────────────────────────────────────────
const builtIn = computed(() => templates.value.filter((t) => !t.custom));
const custom = computed(() => templates.value.filter((t) => t.custom));
const customHint = computed(() => {
	if (custom.value.length) return `${custom.value.length} in this workspace`;
	return isAdmin ? "Start from a blank template" : "";
});
const galleryGroups = computed(() => [
	{
		key: "builtin",
		title: "Built in",
		hint: `${builtIn.value.length} looks that ship with Jarvis`,
		items: builtIn.value,
	},
	{ key: "custom", title: "Your templates", hint: customHint.value, items: custom.value },
]);

const currentLabel = computed(() => (selected.value && selected.value.label) || "");
// The gallery row for the selected template, as the server last reported it
// (admins get every custom template back, disabled ones tagged enabled: false).
const selectedRow = computed(
	() => (selected.value && templates.value.find((t) => t.key === selected.value.key)) || null
);
const selectedRowDisabled = computed(
	() =>
		!!selected.value &&
		selected.value.custom &&
		!!selectedRow.value &&
		!selectedRow.value.enabled
);
const isEditableCustom = computed(
	() => isCreating.value || (!!selected.value && selected.value.custom)
);
const isCurrentDefault = computed(
	() => !!selected.value && selected.value.key === defaultKey.value
);
const dirty = computed(() => !!form.value && JSON.stringify(form.value) !== formSnapshot.value);
// set_default_pdf_template resolves through the enabled-only lookup, so the
// editor's button follows the SAVED state (the gallery row), never the draft:
// an unsaved template, unsaved edits, or a disabled row all mean "save first".
const canMakeDefault = computed(() => {
	if (isCreating.value || !selected.value || isCurrentDefault.value) return false;
	if (dirty.value || selectedRowDisabled.value) return false;
	return true;
});
const editorHint = computed(() => {
	if (isCreating.value)
		return "Unsaved. Save it to list it in the gallery and make it the default.";
	if (!selected.value) return "";
	if (selectedRowDisabled.value) {
		return "Disabled templates stay out of everyone else's list and can't be the default. Turn Enabled on and save to bring it back.";
	}
	if (dirty.value) return "Unsaved changes. Save before making this the default.";
	return "";
});

function isCurrentRow(t) {
	return !isCreating.value && !!selected.value && selected.value.key === t.key;
}

function cardClass(t) {
	if (!isAdmin) {
		return t.key === defaultKey.value
			? "border-outline-gray-3 bg-surface-gray-2"
			: "border-outline-gray-2 bg-surface-white";
	}
	const base = "cursor-pointer focus-visible:ring-2 focus-visible:ring-outline-gray-3";
	if (isCurrentRow(t)) return `${base} border-outline-gray-5 bg-surface-gray-2`;
	return `${base} border-outline-gray-2 bg-surface-white hover:bg-surface-gray-1`;
}

// A catalog key -> a screen stand-in stack for the thumbnails / inline specimen
// (the bundled face itself only renders in the iframe preview, so approximate it by
// the font's category serif/sans).
function fontStack(key) {
	const category = fontMeta.value[key]?.category || (key === "serif" ? "serif" : "sans");
	return UI_FONT_STACKS[category] || UI_FONT_STACKS.sans;
}

function fontLabel(key) {
	return fontMeta.value[key]?.label || (key === "serif" ? "Serif" : "Sans");
}

function coverLabel(cover) {
	if (cover === "on") return "Always";
	if (cover === "off") return "Never";
	return "Long documents only";
}

// The real cover paints the template's dark tone, which the summary doesn't
// carry; deepening the accent lands close enough for a thumbnail. The plain
// accent comes first so a browser without color-mix still paints a panel.
function coverStyle(t) {
	return `background:${t.accent};background:color-mix(in srgb, ${t.accent} 60%, #000)`;
}

function specLine(t) {
	const parts = [fontLabel(t.body_font), t.masthead === "center" ? "centered" : "left aligned"];
	if (t.cover === "on") parts.push("cover page");
	if (t.cover === "off") parts.push("no cover");
	return parts.join(", ");
}

// ── load ─────────────────────────────────────────────────────────────────────
async function load() {
	error.value = "";
	try {
		const res = await listPdfTemplates();
		const d = (res && res.data) || {};
		templates.value = d.templates || [];
		defaultKey.value = d.default || "classic";
		loaded.value = true;
	} catch (e) {
		error.value = errHtml(e, "Couldn't load PDF templates.");
	}
}

async function loadOptions() {
	try {
		const res = await pdfTemplateOptions();
		const d = (res && res.data) || {};
		companies.value = d.companies || [];
		letterHeads.value = d.letter_heads || [];
		fonts.value = d.fonts || [];
	} catch (e) {
		// Non-fatal: the letter-head map editor just has nothing to pick from;
		// the rest of the pane still works.
	}
}

onMounted(async () => {
	await load();
	if (isAdmin) {
		await loadOptions();
		selectDefaultOrFirst();
	}
	loading.value = false;
});

onBeforeUnmount(() => {
	if (debounceTimer) clearTimeout(debounceTimer);
});

function blankForm() {
	return {
		template_key: "",
		label: "",
		description: "",
		accent_color: FALLBACK_SWATCH,
		dark_color: "",
		body_font: "sans",
		display_font: "sans",
		masthead_align: "left",
		cover: "auto",
		watermark: "",
		page_size: "A4",
		orientation: "portrait",
		margins_mm: MARGIN_DEFAULT,
		enabled: true,
		show_logo: true,
		accent_bar: false,
		use_letterhead_footer: false,
		company_letter_heads: [],
	};
}

// margins_mm is an Int on the doctype; keep it a number in the form so the
// JSON snapshot compare (dirty) doesn't flip on "15" vs 15 after a first edit.
function toMargin(v) {
	const n = Number(v);
	return Number.isFinite(n) && v !== "" && v !== null ? n : MARGIN_DEFAULT;
}

function normalizeFormFromApi(d) {
	return {
		template_key: d.template_key || "",
		label: d.label || "",
		description: d.description || "",
		accent_color: d.accent_color || "",
		dark_color: d.dark_color || "",
		body_font: d.body_font || "sans",
		display_font: d.display_font || "sans",
		masthead_align: d.masthead_align || "left",
		cover: d.cover || "auto",
		watermark: d.watermark || "",
		page_size: d.page_size || "A4",
		orientation: d.orientation || "portrait",
		margins_mm: toMargin(d.margins_mm),
		enabled: !!d.enabled,
		show_logo: !!d.show_logo,
		accent_bar: !!d.accent_bar,
		use_letterhead_footer: !!d.use_letterhead_footer,
		company_letter_heads: (d.company_letter_heads || []).map((r) => ({
			company: r.company || "",
			letter_head: r.letter_head || "",
		})),
	};
}

// ── selection ────────────────────────────────────────────────────────────────
function withDirtyGuard(action) {
	if (!dirty.value) {
		action();
		return;
	}
	confirmDialog({
		title: "Discard unsaved changes?",
		message: "Your edits to this template have not been saved.",
		onConfirm: ({ hideDialog }) => {
			hideDialog();
			action();
		},
	});
}

function selectTemplate(t) {
	if (isCurrentRow(t)) return;
	withDirtyGuard(() => doSelect(t));
}

function newTemplate() {
	if (isCreating.value) return;
	withDirtyGuard(doNewTemplate);
}

function doNewTemplate() {
	selected.value = null;
	isCreating.value = true;
	form.value = blankForm();
	formSnapshot.value = JSON.stringify(form.value);
	previewHtml.value = "";
	previewError.value = "";
	schedulePreviewFromForm();
}

async function doSelect(t) {
	error.value = "";
	isCreating.value = false;
	selected.value = t;
	if (!t.custom) {
		form.value = null;
		formSnapshot.value = "";
		previewByKey(t.key);
		return;
	}
	form.value = null;
	formLoading.value = true;
	try {
		const res = await getPdfTemplate(t.key);
		const d = (res && res.data) || {};
		form.value = normalizeFormFromApi(d);
		formSnapshot.value = JSON.stringify(form.value);
	} catch (e) {
		error.value = errHtml(e, "Couldn't load this template.");
		selected.value = null;
		return;
	} finally {
		formLoading.value = false;
	}
	schedulePreviewFromForm();
}

function selectDefaultOrFirst() {
	const target = templates.value.find((t) => t.key === defaultKey.value) || templates.value[0];
	if (target) doSelect(target);
}

// ── letter-head map rows ────────────────────────────────────────────────────
function addLetterheadRow() {
	form.value.company_letter_heads.push({ company: "", letter_head: "" });
}
function removeLetterheadRow(i) {
	form.value.company_letter_heads.splice(i, 1);
}

// ── save / delete / set default ─────────────────────────────────────────────
function payloadFromForm() {
	const f = form.value;
	const out = {
		label: f.label || "",
		description: f.description || "",
		accent_color: f.accent_color || "",
		dark_color: f.dark_color || "",
		body_font: f.body_font || "",
		display_font: f.display_font || "",
		masthead_align: f.masthead_align || "left",
		cover: f.cover || "auto",
		watermark: f.watermark || "",
		page_size: f.page_size || "A4",
		orientation: f.orientation || "portrait",
		// Always an integer: the resolver does int() on it, and a half-typed "12."
		// must not break the live preview mid-edit (save() validates the range).
		margins_mm: Math.round(toMargin(f.margins_mm)),
		enabled: !!f.enabled,
		show_logo: !!f.show_logo,
		accent_bar: !!f.accent_bar,
		use_letterhead_footer: !!f.use_letterhead_footer,
	};
	// Omit a blank key rather than sending "" - preview_pdf_template's
	// _draft_to_tpl only fills its "preview" placeholder key when the field is
	// ABSENT, and save_pdf_template rejects a blank key outright either way.
	const key = (f.template_key || "").trim().toLowerCase();
	if (key) out.template_key = key;
	if (out.use_letterhead_footer) {
		out.company_letter_heads = (f.company_letter_heads || []).filter(
			(r) => r.company && r.letter_head
		);
	}
	return out;
}

function validateForm() {
	const f = form.value;
	const key = (f.template_key || "").trim().toLowerCase();
	if (isCreating.value) {
		if (!key) return "Template key is required.";
		if (!SLUG_RE.test(key)) {
			return "Template key must be a lowercase slug (letters, digits, hyphens), e.g. acme-invoice.";
		}
	}
	const m = Number(f.margins_mm);
	if (!Number.isInteger(m) || m < MARGIN_MIN || m > MARGIN_MAX) {
		return `Margins must be a whole number between ${MARGIN_MIN} and ${MARGIN_MAX} mm.`;
	}
	return "";
}

async function save() {
	if (!form.value) return;
	error.value = validateForm();
	if (error.value) return;
	saving.value = true;
	const key = (form.value.template_key || "").trim().toLowerCase();
	let savedKey = key;
	try {
		const res = await savePdfTemplate(JSON.stringify(payloadFromForm()));
		savedKey = (res && res.data && res.data.key) || key;
		toast.success(isCreating.value ? "Template created." : "Template saved.");
		isCreating.value = false;
	} catch (e) {
		error.value = errHtml(e, "Couldn't save the template.");
		saving.value = false;
		return;
	}
	// The save itself succeeded (the toast already fired) - a failure from here
	// on is only "couldn't reload what we just saved", not a save failure, so it
	// gets its own message rather than reusing "Couldn't save the template."
	try {
		await load();
		// Re-fetch the saved doc directly rather than reusing the gallery row, so
		// the editor keeps the full field set open either way.
		const res2 = await getPdfTemplate(savedKey);
		const d = (res2 && res2.data) || {};
		form.value = normalizeFormFromApi(d);
		formSnapshot.value = JSON.stringify(form.value);
		selected.value = templates.value.find((t) => t.key === savedKey) || {
			key: savedKey,
			custom: true,
			label: d.label || savedKey,
		};
		schedulePreviewFromForm();
	} catch (e) {
		error.value = errHtml(e, "Saved, but couldn't reload it. Select it from the gallery.");
	} finally {
		saving.value = false;
	}
}

function confirmDeleteCurrent() {
	if (!selected.value) return;
	const key = selected.value.key;
	const label = selected.value.label || key;
	confirmDialog({
		title: "Delete this template?",
		// ConfirmDialog renders `message` via v-html (lib/errors.js's own doc
		// comment names it as a sink) - escape the admin-authored label.
		message: `"${escapeHtml(label)}" will be removed. This can't be undone.`,
		onConfirm: async ({ hideDialog }) => {
			deleting.value = true;
			try {
				await deletePdfTemplate(key);
				hideDialog();
				toast.success("Template deleted.");
				await load();
				selected.value = null;
				form.value = null;
				previewHtml.value = "";
				selectDefaultOrFirst();
			} catch (e) {
				toast.error(errHtml(e, "Couldn't delete the template."));
			} finally {
				deleting.value = false;
			}
		},
	});
}

async function setDefault(key) {
	if (!key || settingDefault.value) return;
	error.value = "";
	settingDefault.value = true;
	try {
		const res = await setDefaultPdfTemplate(key);
		defaultKey.value = (res && res.data && res.data.default) || key;
		toast.success("Default PDF template updated.");
	} catch (e) {
		error.value = errHtml(e, "Couldn't update the default.");
	} finally {
		settingDefault.value = false;
	}
}

// ── live preview ─────────────────────────────────────────────────────────────
async function runPreview(config) {
	const mySeq = ++previewSeq;
	previewLoading.value = true;
	previewError.value = "";
	try {
		const res = await previewPdfTemplate(JSON.stringify(config));
		if (mySeq !== previewSeq) return; // superseded by a later selection/edit
		previewHtml.value = (res && res.data && res.data.html) || "";
	} catch (e) {
		if (mySeq !== previewSeq) return;
		previewHtml.value = "";
		previewError.value = errHtml(e, "Couldn't render the preview.");
	} finally {
		if (mySeq === previewSeq) previewLoading.value = false;
	}
}

function previewByKey(key) {
	if (debounceTimer) clearTimeout(debounceTimer);
	runPreview({ key });
}

function schedulePreviewFromForm() {
	if (debounceTimer) clearTimeout(debounceTimer);
	debounceTimer = setTimeout(() => {
		if (!form.value) return;
		const rows = form.value.company_letter_heads || [];
		const firstMappedCompany = (rows.find((r) => r.company) || {}).company || undefined;
		runPreview({ draft: payloadFromForm(), company: firstMappedCompany });
	}, PREVIEW_DEBOUNCE_MS);
}

// Any edit to the open custom template's form re-renders the preview
// (debounced). Selection changes call previewByKey/schedulePreviewFromForm
// directly instead of relying on this watcher, so switching templates doesn't
// wait out a debounce it doesn't need.
watch(
	form,
	(v) => {
		if (v) schedulePreviewFromForm();
	},
	{ deep: true }
);

function toColorInputValue(hex, fallback = FALLBACK_SWATCH) {
	const v = (hex || "").trim();
	if (/^#[0-9a-fA-F]{6}$/.test(v)) return v;
	if (/^#[0-9a-fA-F]{3}$/.test(v)) {
		return (
			"#" +
			v
				.slice(1)
				.split("")
				.map((c) => c + c)
				.join("")
		);
	}
	return fallback;
}
</script>
