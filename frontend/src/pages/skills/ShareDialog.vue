<template>
	<ShareDialog
		:modelValue="modelValue"
		:title="__('Share “{0}”', [skill.skill_name || skill.name])"
		:hint="__('They can use this skill in chat, but can’t edit or re-share it.')"
		:loadCandidates="candidates"
		:loadShares="loadShares"
		:saveShares="saveShares"
		@update:modelValue="(value) => emit('update:modelValue', value)"
		@saved="() => emit('saved')"
	/>
</template>
<script setup>
import ShareDialog from "@/components/ShareDialog.vue";
import { listShareableUsers, getSkillShares, shareCustomSkill } from "@/api";
import { __ } from "@/lib/i18n";
const props = defineProps({
	modelValue: { type: Boolean, default: false },
	skill: { type: Object, default: () => ({ name: "", skill_name: "" }) },
});
const emit = defineEmits(["update:modelValue", "saved"]);
const loadShares = () => getSkillShares(props.skill.name);
const saveShares = (users) => shareCustomSkill(props.skill.name, users);
async function candidates(query) {
	const rows = await listShareableUsers();
	const q = query.trim().toLowerCase();
	return (rows || []).filter(
		(u) =>
			!q || (u.full_name || "").toLowerCase().includes(q) || u.name.toLowerCase().includes(q)
	);
}
</script>
