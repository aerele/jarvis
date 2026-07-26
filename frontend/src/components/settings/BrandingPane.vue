<template>
	<!-- The dialog shell stopped rendering a shared header (design.md §4.1: each
	     pane owns its own), so this pane uses the shared SettingsPane frame like
	     every other migrated pane. -->
	<SettingsPane
		title="Branding"
		description="Your assistant's name, logo and favicon."
		:error="error"
	>
		<div v-if="loading" class="text-p-sm text-ink-gray-6">Loading…</div>
		<template v-else>
			<FormControl
				type="text"
				label="Assistant name"
				v-model="name"
				maxlength="40"
				placeholder="Jarvis"
				description="Shown in the chat header, the browser tab, notifications, and in the assistant's own replies. Leave blank to use “Jarvis”. Up to 40 characters."
			/>

			<!-- Logo -->
			<div class="mt-6 flex flex-col gap-1.5">
				<span class="text-xs text-ink-gray-5">Logo</span>
				<div class="flex items-center gap-3">
					<img
						v-if="logoUrl"
						:src="logoUrl"
						class="size-11 shrink-0 rounded-lg object-cover"
						alt="Logo preview"
					/>
					<!-- No logo yet: the same brand-mark glyph JarvisMark renders
					     elsewhere (onboarding, chat avatars). Kept local rather than
					     reusing <JarvisMark> because that component reads the
					     committed brandLogoUrl, not this pane's unsaved draft - after
					     "Remove" the preview must go blank immediately, before Save. -->
					<span
						v-else
						class="flex size-11 shrink-0 items-center justify-center rounded-lg"
						style="
							background: var(
								--brand-grad,
								linear-gradient(135deg, #6e8bff, #8b5cf6)
							);
						"
						aria-hidden="true"
					>
						<svg width="26" height="26" viewBox="0 0 24 24" fill="#fff">
							<path
								d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
							/>
						</svg>
					</span>
					<Button
						variant="subtle"
						size="sm"
						:label="uploadingLogo ? 'Uploading…' : logoUrl ? 'Replace' : 'Upload'"
						:loading="uploadingLogo"
						@click="logoInput.click()"
					/>
					<Button
						v-if="logoUrl"
						variant="subtle"
						size="sm"
						label="Remove"
						:disabled="uploadingLogo"
						@click="logoUrl = ''"
					/>
					<input
						ref="logoInput"
						type="file"
						accept="image/*"
						hidden
						@change="onPick($event, 'logo')"
					/>
				</div>
				<span class="text-p-xs text-ink-gray-5">
					Used as the assistant avatar and brand mark. A square image works best.
				</span>
			</div>

			<!-- Favicon -->
			<div class="mt-6 flex flex-col gap-1.5">
				<span class="text-xs text-ink-gray-5">Favicon</span>
				<div class="flex items-center gap-3">
					<img
						v-if="faviconUrl"
						:src="faviconUrl"
						class="size-6 shrink-0 rounded-sm border object-cover"
						alt="Favicon preview"
					/>
					<span
						v-else
						class="size-6 shrink-0 rounded-sm border bg-surface-gray-2"
						aria-hidden="true"
					/>
					<Button
						variant="subtle"
						size="sm"
						:label="
							uploadingFavicon ? 'Uploading…' : faviconUrl ? 'Replace' : 'Upload'
						"
						:loading="uploadingFavicon"
						@click="faviconInput.click()"
					/>
					<Button
						v-if="faviconUrl"
						variant="subtle"
						size="sm"
						label="Remove"
						:disabled="uploadingFavicon"
						@click="faviconUrl = ''"
					/>
					<input
						ref="faviconInput"
						type="file"
						accept="image/png,image/x-icon,image/svg+xml,image/*"
						hidden
						@change="onPick($event, 'favicon')"
					/>
				</div>
				<span class="text-p-xs text-ink-gray-5">
					The browser-tab icon. A square PNG (or .ico) works best.
				</span>
			</div>

			<!-- Save -->
			<div class="mt-6">
				<Button
					variant="solid"
					label="Save branding"
					:loading="saving"
					:disabled="!dirty"
					@click="save"
				/>
			</div>
		</template>
	</SettingsPane>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import { Button, FormControl, toast } from "frappe-ui";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import * as api from "@/api";

const loading = ref(true);
const saving = ref(false);
const uploadingLogo = ref(false);
const uploadingFavicon = ref(false);
const error = ref("");

const name = ref("");
const logoUrl = ref("");
const faviconUrl = ref("");
let original = { name: "", logoUrl: "", faviconUrl: "" };

const logoInput = ref(null);
const faviconInput = ref(null);

const dirty = computed(
	() =>
		name.value.trim() !== original.name ||
		logoUrl.value !== original.logoUrl ||
		faviconUrl.value !== original.faviconUrl
);

onMounted(async () => {
	try {
		const res = await api.getBranding();
		const d = (res && res.data) || {};
		name.value = d.agent_name || "";
		logoUrl.value = d.brand_logo_url || "";
		faviconUrl.value = d.brand_favicon_url || "";
		original = { name: name.value, logoUrl: logoUrl.value, faviconUrl: faviconUrl.value };
	} catch (e) {
		error.value = "Couldn't load branding.";
	} finally {
		loading.value = false;
	}
});

async function onPick(ev, which) {
	const file = ev.target.files && ev.target.files[0];
	ev.target.value = ""; // allow re-picking the same file
	if (!file) return;
	error.value = "";
	const flag = which === "logo" ? uploadingLogo : uploadingFavicon;
	flag.value = true;
	try {
		const { file_url } = await api.uploadBrandAsset(file);
		if (which === "logo") logoUrl.value = file_url;
		else faviconUrl.value = file_url;
	} catch (e) {
		error.value = "Upload failed. Try a smaller image.";
	} finally {
		flag.value = false;
	}
}

async function save() {
	error.value = "";
	saving.value = true;
	try {
		await api.updateBranding({
			agent_name: name.value.trim(),
			logo_url: logoUrl.value,
			favicon_url: faviconUrl.value,
		});
		original = {
			name: name.value.trim(),
			logoUrl: logoUrl.value,
			faviconUrl: faviconUrl.value,
		};
		name.value = original.name;
		// Success flows through toast, not a bespoke inline green node
		// (design.md §5 anti-pattern 16) - see SettingsPane's own doc comment.
		toast.success("Saved. Refresh to apply across the app.");
	} catch (e) {
		error.value = (e && e.message) || "Save failed.";
	} finally {
		saving.value = false;
	}
}
</script>
