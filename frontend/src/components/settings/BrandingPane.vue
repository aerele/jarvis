<template>
	<!-- The dialog shell stopped rendering a shared header (design.md §4.1: each
	     pane owns its own), so this pane supplies one like every other. Only the
	     header is migrated; the body below still uses the legacy jv-* markup
	     because the branding editor arrived on develop after this migration
	     branched and is deferred with AiModelsPane.

	     The outer flex-col also matters: .jv-settings-body's `flex:1;
	     overflow-y:auto` only becomes a scroll region inside a flex column. On a
	     plain block wrapper the asset pickers would clip with no scrollbar. -->
	<div class="flex h-full flex-col gap-6 px-10 py-8 text-ink-gray-8">
		<div class="flex flex-col gap-1">
			<h2 class="text-lg font-semibold text-ink-gray-8">Branding</h2>
			<p class="max-w-md text-p-sm text-ink-gray-6">
				Your assistant's name, logo and favicon.
			</p>
		</div>

		<div class="jv-settings-body jv-pane-fill min-h-0 flex-1">
			<div v-if="loading" class="jv-set-hint">Loading…</div>
			<template v-else>
				<!-- Assistant name -->
				<div class="jv-set-sec">Assistant name</div>
				<input
					class="jv-brand-input"
					type="text"
					maxlength="40"
					v-model="name"
					placeholder="Jarvis"
				/>
				<div class="jv-set-hint">
					Shown in the chat header, the browser tab, notifications, and in the
					assistant's own replies. Leave blank to use “Jarvis”. Up to 40 characters.
				</div>

				<!-- Logo -->
				<div class="jv-set-sec" style="margin-top: 20px">Logo</div>
				<div class="jv-brand-asset">
					<img
						v-if="logoUrl"
						:src="logoUrl"
						class="jv-brand-logo-prev"
						alt="Logo preview"
					/>
					<span
						v-else
						class="jv-brand-logo-prev jv-brand-logo-default"
						aria-hidden="true"
					>
						<svg width="26" height="26" viewBox="0 0 24 24" fill="#fff">
							<path
								d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
							/>
						</svg>
					</span>
					<div class="jv-brand-asset-actions">
						<button
							class="jv-btn jv-btn--sm jv-btn--ghost"
							:disabled="uploadingLogo"
							@click="logoInput.click()"
						>
							{{ uploadingLogo ? "Uploading…" : logoUrl ? "Replace" : "Upload" }}
						</button>
						<button
							v-if="logoUrl"
							class="jv-btn jv-btn--sm jv-btn--ghost"
							:disabled="uploadingLogo"
							@click="logoUrl = ''"
						>
							Remove
						</button>
					</div>
					<input
						ref="logoInput"
						type="file"
						accept="image/*"
						hidden
						@change="onPick($event, 'logo')"
					/>
				</div>
				<div class="jv-set-hint">
					Used as the assistant avatar and brand mark. A square image works best.
				</div>

				<!-- Favicon -->
				<div class="jv-set-sec" style="margin-top: 20px">Favicon</div>
				<div class="jv-brand-asset">
					<img
						v-if="faviconUrl"
						:src="faviconUrl"
						class="jv-brand-fav-prev"
						alt="Favicon preview"
					/>
					<span
						v-else
						class="jv-brand-fav-prev jv-brand-fav-default"
						aria-hidden="true"
					></span>
					<div class="jv-brand-asset-actions">
						<button
							class="jv-btn jv-btn--sm jv-btn--ghost"
							:disabled="uploadingFavicon"
							@click="faviconInput.click()"
						>
							{{
								uploadingFavicon ? "Uploading…" : faviconUrl ? "Replace" : "Upload"
							}}
						</button>
						<button
							v-if="faviconUrl"
							class="jv-btn jv-btn--sm jv-btn--ghost"
							:disabled="uploadingFavicon"
							@click="faviconUrl = ''"
						>
							Remove
						</button>
					</div>
					<input
						ref="faviconInput"
						type="file"
						accept="image/png,image/x-icon,image/svg+xml,image/*"
						hidden
						@change="onPick($event, 'favicon')"
					/>
				</div>
				<div class="jv-set-hint">
					The browser-tab icon. A square PNG (or .ico) works best.
				</div>

				<!-- Save -->
				<div class="jv-brand-foot">
					<button
						class="jv-btn jv-btn--sm jv-btn--primary"
						:disabled="!dirty || saving"
						@click="save"
					>
						{{ saving ? "Saving…" : "Save branding" }}
					</button>
					<span v-if="error" class="jv-brand-err">{{ error }}</span>
					<span v-else-if="savedMsg" class="jv-brand-ok">{{ savedMsg }}</span>
				</div>
			</template>
		</div>
	</div>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import * as api from "@/api";

const loading = ref(true);
const saving = ref(false);
const uploadingLogo = ref(false);
const uploadingFavicon = ref(false);
const error = ref("");
const savedMsg = ref("");

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
	savedMsg.value = "";
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
		savedMsg.value = "Saved. Refresh to apply across the app.";
	} catch (e) {
		error.value = (e && e.message) || "Save failed.";
	} finally {
		saving.value = false;
	}
}
</script>

<style scoped>
.jv-brand-input {
	width: 100%;
	max-width: 340px;
	padding: 8px 10px;
	border: 1px solid var(--border, #e3e3ea);
	border-radius: 8px;
	background: var(--surface, #fff);
	color: var(--text, #16161a);
	font-size: 13px;
	outline: none;
}
.jv-brand-input:focus {
	border-color: var(--brand-1, #6e8bff);
}
.jv-brand-asset {
	display: flex;
	align-items: center;
	gap: 12px;
	margin: 4px 0;
}
.jv-brand-asset-actions {
	display: flex;
	gap: 8px;
}
.jv-brand-logo-prev {
	width: 44px;
	height: 44px;
	border-radius: 11px;
	object-fit: cover;
	flex-shrink: 0;
}
.jv-brand-logo-default {
	display: grid;
	place-items: center;
	background: var(--brand-grad, linear-gradient(135deg, #6e8bff, #8b5cf6));
}
.jv-brand-fav-prev {
	width: 24px;
	height: 24px;
	border-radius: 5px;
	object-fit: cover;
	flex-shrink: 0;
	border: 1px solid var(--border, #e3e3ea);
}
.jv-brand-fav-default {
	display: block;
	background: var(--surface-2, #f0f0f4);
}
.jv-brand-foot {
	display: flex;
	align-items: center;
	gap: 12px;
	margin-top: 24px;
}
.jv-brand-err {
	color: var(--red, #e5484d);
	font-size: 12px;
}
.jv-brand-ok {
	color: var(--green, #30a46c);
	font-size: 12px;
}
</style>
