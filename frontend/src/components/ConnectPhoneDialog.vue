<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: 'Connect your phone', size: 'sm' }"
		@update:modelValue="(v) => emit('update:modelValue', v)"
	>
		<template #body-content>
			<p class="text-p-sm text-ink-gray-6">
				Open the <b>{{ agentName }}</b> app on your phone and scan this code to connect to
				this workspace — then sign in with your email and password.
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
						<img :src="qrSrc" alt="Pairing QR code" class="size-52" />
					</div>
					<div class="mt-3 text-center">
						<div class="text-base font-medium text-ink-gray-8">{{ payload.name }}</div>
						<div class="text-sm text-ink-gray-5">{{ payload.site }}</div>
					</div>
				</template>
			</div>

			<p class="mt-4 text-p-sm text-ink-gray-5">
				Don’t have the app yet? Install <b>{{ agentName }}</b> from your app store, then
				tap <b>Scan to connect</b> on the welcome screen.
			</p>
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
// ConnectPhoneDialog — shows a QR encoding this site's connection details
// (site URL, real site name, dev socket port) for the mobile app to scan during
// onboarding. Contains NO credential; the phone signs in with email+password
// after scanning. Backend: jarvis.mobile.auth.get_pairing_qr.
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
const payload = ref({ name: "", site: "" });

const qrSrc = computed(() => (svg.value ? `data:image/svg+xml;base64,${svg.value}` : ""));

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Could not generate the code.";
}

async function load() {
	loading.value = true;
	error.value = "";
	try {
		const res = await api.getPairingQr();
		svg.value = res?.svg || "";
		payload.value = res?.payload || { name: "", site: "" };
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
