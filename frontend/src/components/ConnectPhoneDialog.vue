<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: `Get ${agentName} on your phone`, size: 'sm' }"
		@update:modelValue="(v) => emit('update:modelValue', v)"
	>
		<template #body-content>
			<p class="text-p-sm text-ink-gray-6">
				Scan this code to open <b>{{ agentName }}</b> on your phone.
			</p>

			<div class="mt-4 flex flex-col items-center">
				<div
					v-if="loading"
					class="flex h-52 w-52 items-center justify-center text-sm text-ink-gray-5"
				>
					Generating code…
				</div>
				<div
					v-else-if="error"
					class="flex h-52 w-52 flex-col items-center justify-center gap-2 text-center text-sm text-ink-gray-5"
				>
					<FeatherIcon name="alert-triangle" class="size-5 text-ink-amber-3" />
					<span>{{ error }}</span>
					<Button label="Retry" variant="subtle" @click="load" />
				</div>
				<template v-else>
					<div class="rounded-xl border border-outline-gray-2 bg-white p-3">
						<img :src="qrSrc" alt="Mobile app QR code" class="size-52" />
					</div>
				</template>
			</div>
		</template>

		<template #actions>
			<Button
				label="Done"
				variant="solid"
				class="w-full"
				@click="emit('update:modelValue', false)"
			/>
		</template>
	</Dialog>
</template>

<script setup>
// ConnectPhoneDialog: shows a QR encoding the mobile PWA URL (/jarvis-mobile).
// Scanning it opens the PWA on the phone: Android offers to install it (the PWA's
// own beforeinstallprompt banner), iOS loads it in Safari for Add-to-Home-Screen.
// No credential in the QR, just the public URL; the user signs in after opening.
// Backend: jarvis.mobile.auth.get_pwa_qr.
import { ref, computed, watch } from "vue";
import { Dialog, Button, FeatherIcon } from "frappe-ui";
import * as api from "@/api";
import { agentName } from "@/branding";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue"]);

const loading = ref(false);
const error = ref("");
const svg = ref("");
const url = ref("");

const qrSrc = computed(() => (svg.value ? `data:image/svg+xml;base64,${svg.value}` : ""));

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Could not generate the code.";
}

async function load() {
	loading.value = true;
	error.value = "";
	try {
		const res = await api.getPwaQr();
		svg.value = res?.svg || "";
		url.value = res?.url || "";
		if (!svg.value) error.value = "Could not generate the code.";
	} catch (e) {
		error.value = errMsg(e);
	} finally {
		loading.value = false;
	}
}

// Fetch a fresh QR each time the dialog opens.
watch(
	() => props.modelValue,
	(open) => {
		if (open) load();
	}
);
</script>
