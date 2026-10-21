<template>
	<ShareDialog
		v-if="target"
		:key="target.name"
		:modelValue="true"
		:title="__('Share “{0}”', [target.title || __('Chat')])"
		:hint="hint"
		:loadCandidates="shareCandidates"
		:loadShares="() => getChatShares(target.name)"
		:saveShares="(users) => setChatShares(target.name, users)"
		@update:modelValue="close"
		@saved="saved"
	/>
</template>
<script setup>
import { computed } from "vue";
import ShareDialog from "@/components/ShareDialog.vue";
import { chatShareTarget } from "@/composables/useChatSharing";
import { shareCandidates, getChatShares, setChatShares } from "@/api/chatSharing";
import { useShellStore } from "@/stores/shell";
import { __ } from "@/lib/i18n";
const target = computed(() => chatShareTarget.value);
const hint = __(
	"People you add can read this whole chat, including data from your ERP, connected apps and memories Jarvis keeps about you that they cannot normally see. Secrets are hidden only where Jarvis can recognise them. They cannot reply or act on it. Admins can see this chat's title and who it is shared with."
);
function close(value) {
	if (!value) chatShareTarget.value = null;
}
function saved() {
	useShellStore().loadConversations();
}
</script>
