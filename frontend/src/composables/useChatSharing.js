import { ref } from "vue";
export const chatShareTarget = ref(null);
export function openChatShare(conversation, title) {
	if (conversation) chatShareTarget.value = { name: conversation, title: title || "" };
}
