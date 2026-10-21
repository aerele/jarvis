<template>
	<ShareDialog
		v-if="target"
		:key="target.name"
		:modelValue="true"
		:title="__('Share “{0}”', [target.title || __('Chat')])"
		:hint="hint"
		:loadCandidates="shareCandidates"
		:loadShares="() => getChatAccess(target.name)"
		:saveShares="(users, modes) => setChatRecipients(target.name, users, modes)"
		:accessModes="true"
		@update:modelValue="close"
		@saved="saved"
	/>
</template>
<script setup>
import { computed } from "vue";
import ShareDialog from "@/components/ShareDialog.vue";
import { chatShareTarget } from "@/composables/useChatSharing";
import { shareCandidates } from "@/api/chatSharing";
import { getChatAccess, setChatRecipients } from "@/api/chatCollaboration";
import { useShellStore } from "@/stores/shell";
import { __ } from "@/lib/i18n";
const target = computed(() => chatShareTarget.value);
const hint = __(
	"People you add can read this whole chat, including ERP data, connected apps and any personal information already in its history. Secrets are hidden only where Jarvis can recognise them. Admins can see this chat's title and who it is shared with."
);
function close(value) {
	if (!value) chatShareTarget.value = null;
}
function saved() {
	useShellStore().loadConversations();
}
</script>
